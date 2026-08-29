"""M04 AI 画像会话、回答、受控结构化抽取、确认、发布、历史与删除（Task 7+8，统一方案 §7）。

本模块是 M04 文字会话、抽取、草稿确认/发布/历史/删除边界的事实源：

- ``create_profile_session`` 只允许同一 ``user_id + subject`` 存在一个活动会话
  （已存在则回放/复用）；创建前校验 ``profile_text_extract`` 授权并快照当前
  revision 向量与授权信息。
- ``submit_profile_turn`` 先过 ``moderate_text`` 内容审核（reject 拒绝入库、
  replace 用脱敏文本；幂等回放不重复审核），再把（脱敏后）turn 落库（抽取失败
  不删原文），以 ``profile_extract`` 任务入队；同 ``client_turn_id`` 重复提交
  只回放原 turn，不创建第二个任务。
- ``extract_profile_turn`` 是 Worker 注册的 ``profile_extract`` handler：只调用
  ``AIGateway.structured_extract``，结果只写成 ``suggested`` 状态的草稿字段，
  绝不产生已发布字段或认证字段；schema-invalid/timeout 只改变任务状态。
- ``confirm_profile_draft`` 逐项 confirm/replace/reject/delete，每个 action 都
  携带旧 revision（不匹配抛 ``409 DRAFT_VERSION_CONFLICT``）；replace 重新过
  字段 Schema 与来源约束，delete 只标记字段不可见。
- ``publish_profile_draft`` 只把 ``confirmed`` 字段写入不可变
  ``ai_profile_revision`` + ``ai_profile_revision_field``，然后只递增对应主体
  revision（personal→profile、ideal_partner→preference，不得依据未定义的
  ``revision.kind`` 推断）并写一条 outbox 事件；同 key 同 payload 回放同一 task。
- ``restore_profile_revision`` 从旧快照创建新 draft（字段回填 ``suggested``），
  不更新旧行；旧 revision 只读。
- ``delete_ai_profile`` / ``delete_ai_profile_field`` 在同一事务内先写
  invalidated_at/不可读标记（草稿、活动会话、已发布投影引用、search result、
  compatibility snapshot）并递增 privacy/对应主体 revision、写 outbox 删除事件，
  再 enqueue cleanup task；同步响应前草稿与派生结果已不可读。异步物理清理由
  Task 9/10/11 的消费者实现（本任务只注册占位 handler）。

与 Task 6 的任务状态机一致，本模块函数**不**调用 ``commit()``——调用方（路由
或 Worker）控制事务；自提交例外有二：``_mark_stale`` 必须在抛出
``PROFILE_SESSION_STALE`` 前自行提交 stale 状态变更（异常路径下 get_db 上下文
退出会回滚未提交事务，不提交则 stale 永不落库，同 user+subject 将无法重新创建
会话）；``_fail_extract_session`` 在 extract handler 终态失败时自行提交会话
FAILED + fail_task（worker 对返回 None 的 handler 会回滚其事务，不提交则
FAILED 落不了库，会话将永远停留在 extracting）。原回答与密钥永不进入日志或
错误响应。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.ai_common import AI_FIELD_ALLOWLIST, AiTaskStatus, ProjectionKind
from app.schemas.ai_profile import (
    PROFILE_ENTRY_CATEGORIES,
    PROFILE_ENTRY_CONTENT_MAX_LENGTH,
    ProfileFieldConfirmationStatus,
    ProfileFieldPatchAction,
    ProfileQuestion,
    ProfileRevisionPage,
    ProfileRevisionRead,
    ProfileSessionStatus,
    ProfileSubject,
    normalize_profile_extracted_value,
)
from app.services.ai.base import (
    AITaskContext,
    ExtractedEntry,
    ExtractedField,
    NarrativeRequest,
    StructuredExtractRequest,
)
from app.services.ai.features import (
    ProjectionBuildError,
    _load_latest_revision,
    _subject_for_kind,
    build_feature_projection,
)
from app.services.ai.gateway import AIGateway
from app.services.ai.prompts.profile_narrative import serialize_fields_for_prompt
from app.services.ai.tasks import AiTaskRecord, TaskError, enqueue_task, fail_task
from app.services.content_filter import moderate_text
from app.services.derivation_outbox import purge_ai_resources, run_cleanup_for_user
from app.services.revisions import (
    RevisionKind,
    RevisionVector,
    increment_revision_and_enqueue,
)

logger = logging.getLogger(__name__)

# 冻结的 schema/prompt 版本（Task 1/5 冻结，Task 7 引用，不新增版本）。
PROFILE_SCHEMA_VERSION = "profile-extract-v1"
PROFILE_PROMPT_VERSION = "profile-extract-prompt-v1"
PROFILE_POLICY_REVISION = "ai-policy-2026-08-07-v1"
PROFILE_CONSENT_SCOPE = "profile_text_extract"

# 画像叙事层（narrative）版本常量——发布后生成人格画像解读成品。
NARRATIVE_SCHEMA_VERSION = "profile-narrative-v1"
NARRATIVE_PROMPT_VERSION = "profile-narrative-prompt-v4"
_NARRATIVE_TASK_TYPE = "profile_narrative"
# 叙事层重新生成（regenerate）每日上限：24h 窗口内 profile_narrative 任务数。
_NARRATIVE_REGENERATE_DAILY_LIMIT = 5

# 会话依赖的当前 revision 版本变化（profile/preference 任一）即视为 stale，
# 客户端必须重新创建会话（统一方案 §7.5 PROFILE_SESSION_STALE）。
_STALE_STATUSES = frozenset(
    {
        ProfileSessionStatus.CANCELLED,
        ProfileSessionStatus.PUBLISHED,
        ProfileSessionStatus.FAILED,
    }
)
_ACTIVE_FOR_TURNS = frozenset(
    {
        ProfileSessionStatus.DRAFT,
        ProfileSessionStatus.EXTRACTING,
        ProfileSessionStatus.AWAITING_CONFIRMATION,
        ProfileSessionStatus.PAUSED,
    }
)
_PAUSEABLE = frozenset(
    {
        ProfileSessionStatus.DRAFT,
        ProfileSessionStatus.EXTRACTING,
        ProfileSessionStatus.AWAITING_CONFIRMATION,
    }
)
_RESUMABLE = frozenset(
    {
        ProfileSessionStatus.PAUSED,
        ProfileSessionStatus.DRAFT,
        ProfileSessionStatus.EXTRACTING,
        ProfileSessionStatus.AWAITING_CONFIRMATION,
    }
)

# 会话状态合法迁移（统一方案 §7.2；执行计划 §3.1）。pause/resume 不改变已保存
# turn；发布/删除后历史只读。Task 8 负责 published 路径。
_SESSION_TRANSITIONS: dict[ProfileSessionStatus, set[ProfileSessionStatus]] = {
    ProfileSessionStatus.DRAFT: {
        ProfileSessionStatus.EXTRACTING,
        ProfileSessionStatus.PAUSED,
        ProfileSessionStatus.CANCELLED,
        ProfileSessionStatus.STALE,
    },
    ProfileSessionStatus.EXTRACTING: {
        ProfileSessionStatus.AWAITING_CONFIRMATION,
        ProfileSessionStatus.PAUSED,
        ProfileSessionStatus.CANCELLED,
        ProfileSessionStatus.STALE,
        ProfileSessionStatus.FAILED,
    },
    ProfileSessionStatus.AWAITING_CONFIRMATION: {
        ProfileSessionStatus.EXTRACTING,
        ProfileSessionStatus.PAUSED,
        ProfileSessionStatus.CANCELLED,
        ProfileSessionStatus.STALE,
    },
    ProfileSessionStatus.PAUSED: {
        ProfileSessionStatus.DRAFT,
        ProfileSessionStatus.AWAITING_CONFIRMATION,
        ProfileSessionStatus.CANCELLED,
        ProfileSessionStatus.STALE,
    },
    ProfileSessionStatus.PUBLISHED: set(),
    ProfileSessionStatus.FAILED: set(),
    ProfileSessionStatus.CANCELLED: set(),
    ProfileSessionStatus.STALE: set(),
}

# 缺失字段 → 追问问题字典（固定文案，不诱导敏感信息；§7.5 示例对齐）。
# Task6 Step2：每个问题补稳定 ``field_key``（属于 AI_FIELD_ALLOWLIST），前端据
# 此映射到 typed field 编辑器，不依赖问题文案或顺序。加法字段，保留 id/text。
_PROFILE_QUESTION_BANK: dict[str, ProfileQuestion] = {
    "interest_tags": ProfileQuestion(
        id="interest_lifestyle_v1",
        text="最近让你投入的事情是什么？",
        field_key="interest_tags",
    ),
    "city_code": ProfileQuestion(
        id="city_residence_v1",
        text="你现在生活在哪座城市？",
        field_key="city_code",
    ),
    "marriage_status": ProfileQuestion(
        id="marriage_status_v1",
        text="你目前的婚姻状态是？",
        field_key="marriage_status",
    ),
    "education_level": ProfileQuestion(
        id="education_v1",
        text="你的最高学历是？",
        field_key="education_level",
    ),
    "height_cm": ProfileQuestion(
        id="height_v1",
        text="你的身高是多少？",
        field_key="height_cm",
    ),
    "income_band": ProfileQuestion(
        id="income_v1",
        text="你的收入大概在什么范围？",
        field_key="income_band",
    ),
    "occupation_group": ProfileQuestion(
        id="occupation_v1",
        text="你从事什么职业？",
        field_key="occupation_group",
    ),
    "lifestyle_tags": ProfileQuestion(
        id="lifestyle_v1",
        text="你平时的生活方式有什么特点？",
        field_key="lifestyle_tags",
    ),
    "relationship_goal": ProfileQuestion(
        id="relationship_goal_v1",
        text="你对这段关系的期待是什么？",
        field_key="relationship_goal",
    ),
    "age": ProfileQuestion(
        id="age_v1",
        text="你今年多大了？",
        field_key="age",
    ),
}

_SESSION_COLUMNS = (
    "session_id, user_id, subject, input_mode, status, active_status, "
    "consent_version, policy_revision, current_question_id, skipped_field_keys, "
    "profile_revision, preference_revision, expires_at, ended_at, "
    "created_at, updated_at"
)
_TURN_COLUMNS = (
    "turn_id, session_id, client_turn_id, user_id, turn_no, role, "
    "answer_text, status, source_type, created_at"
)

# Task 8：发布投影任务与删除清理任务类型。profile_projection 的投影构建 handler
# 与 cleanup 的物理清理 handler 在本文件末尾注册（ai_worker 显式导入注册，final
# review C-2/C-3）。
_PROJECTION_TASK_TYPE = "profile_projection"
_CLEANUP_TASK_TYPE = "cleanup"
# restore 是同步操作但需要幂等记录，复用 ai_task 表做去重锚（缺陷 15）。
_RESTORE_TASK_TYPE = "profile_restore"

# 供 app.workers.ai_worker.register_business_handlers 引用的公共常量。
PROJECTION_TASK_TYPE = _PROJECTION_TASK_TYPE
CLEANUP_TASK_TYPE = _CLEANUP_TASK_TYPE
NARRATIVE_TASK_TYPE = _NARRATIVE_TASK_TYPE

_DRAFT_COLUMNS = (
    "draft_id, user_id, subject, session_id, status, expected_revision, "
    "consent_snapshot_json, policy_revision, prompt_version, schema_version, "
    "published_revision_id, last_operation_idempotency_key, "
    "last_operation_request_digest, last_operation_response_json, "
    "expires_at, created_at, updated_at"
)
_DRAFT_FIELD_COLUMNS = (
    "draft_id, field_key, subject, field_kind, category, content, "
    "replaces_field_key, value_json, display_value, source_type, "
    "source_turn_ids, source_span, confidence, visibility, consent_scope, schema_version, "
    "prompt_version, content_hash, confirmation_status, created_at, updated_at"
)
_TASK_COLUMNS = (
    "id, task_id, owner_user_id, task_type, scene, idempotency_key, request_digest, "
    "status, stage, attempt_count, max_attempts, next_run_at, lease_owner, lease_until, "
    "consent_snapshot_json, source_revision_json, payload_summary, error_code, error_message, "
    "result_ref, created_at, updated_at, started_at, finished_at"
)

# replace 动作要求 value 非空；标签类字段必须是「非空字符串数组」。
_TAG_LIST_FIELDS = frozenset({"interest_tags", "lifestyle_tags"})

# 删除传播的投影类型白名单（统一方案 §10.3 projection_kind 枚举）。
_PERSONAL_PROJECTION_KINDS = ("personal_searchable", "personal_compatibility")
_IDEAL_PARTNER_PROJECTION_KINDS = ("ideal_partner_preference",)


# ----------------------------------------------------------------------
# 稳定业务错误（执行计划 §3.2 错误码注册表）
# ----------------------------------------------------------------------


class AIInputError(Exception):
    """400 AI_INPUT_INVALID：类型、长度、枚举或范围非法，不重试。"""

    code = "AI_INPUT_INVALID"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = 400
        self.retryable = False
        self.retry_after_ms = 0


class ProfileSessionNotFound(Exception):
    """404 PROFILE_SESSION_NOT_FOUND：不存在或非本人；不泄露归属。"""

    code = "PROFILE_SESSION_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("画像会话不存在")
        self.message = "画像会话不存在"
        self.status_code = 404
        self.retryable = False
        self.retry_after_ms = 0


class ProfileSessionStale(Exception):
    """409 PROFILE_SESSION_STALE：资料/授权版本变化或过期，需重新创建会话。"""

    code = "PROFILE_SESSION_STALE"

    def __init__(self) -> None:
        super().__init__("画像会话依赖的资料或授权版本已变化，请重新创建会话")
        self.message = "画像会话依赖的资料或授权版本已变化，请重新创建会话"
        self.status_code = 409
        self.retryable = False
        self.retry_after_ms = 0


class AIConsentRequired(Exception):
    """403 AI_CONSENT_REQUIRED：scope 未授权或已撤回，不创建任务。"""

    code = "AI_CONSENT_REQUIRED"

    def __init__(self) -> None:
        super().__init__("尚未同意 AI 画像文字抽取授权")
        self.message = "尚未同意 AI 画像文字抽取授权"
        self.status_code = 403
        self.retryable = False
        self.retry_after_ms = 0


class DraftVersionConflict(Exception):
    """409 DRAFT_VERSION_CONFLICT：expected_revision 与当前草稿版本不符。"""

    code = "DRAFT_VERSION_CONFLICT"

    def __init__(self) -> None:
        super().__init__("草稿版本已变化，请刷新后重试")
        self.message = "草稿版本已变化，请刷新后重试"
        self.status_code = 409
        self.retryable = False
        self.retry_after_ms = 0


class ProfileDraftNotFound(Exception):
    """404 PROFILE_DRAFT_NOT_FOUND：草稿不存在或非本人；不泄露归属。"""

    code = "PROFILE_DRAFT_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("画像草稿不存在")
        self.message = "画像草稿不存在"
        self.status_code = 404
        self.retryable = False
        self.retry_after_ms = 0


class ProfileRevisionNotFound(Exception):
    """404 PROFILE_REVISION_NOT_FOUND：历史版本不存在或非本人；不泄露归属。"""

    code = "PROFILE_REVISION_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("画像历史版本不存在")
        self.message = "画像历史版本不存在"
        self.status_code = 404
        self.retryable = False
        self.retry_after_ms = 0


class DraftStatusConflict(Exception):
    """409 RESULT_STALE：草稿已进入只读终态（published/deleted/cancelled）。

    发布或字段修改前必须处于 ``draft`` 等可编辑状态；已删除草稿的授权已撤回，
    不得用原 expected_revision 重新发布（否则会静默撤销删除意图）。
    """

    code = "RESULT_STALE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = 409
        self.retryable = False
        self.retry_after_ms = 0


# ----------------------------------------------------------------------
# 领域对象
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileSession:
    """One ai_profile_session row plus reconstructed revision/consent context."""

    session_id: str
    owner_user_id: int
    subject: ProfileSubject
    status: ProfileSessionStatus
    input_mode: str
    consent_version: str
    policy_revision: str
    current_question: ProfileQuestion | None
    revision_vector: RevisionVector
    consent_snapshot: dict[str, Any]
    field_keys: frozenset[str]
    confirmed_keys: frozenset[str]
    profile_revision: int
    preference_revision: int
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime | None
    # Task6 Step2：当前会话的活动草稿 ID（无活动草稿为 None）。加法字段，
    # 由 ``_load_active_draft_id_for_session`` 在装配 session 时回填，路由层
    # 透传到 ``ProfileSessionRead.draft_id``。
    draft_id: str | None = None
    # 用户点「不想答」跳过的字段。不计入 confirmed，也不再作为下一问。
    skipped_keys: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProfileTurn:
    """One ai_profile_turn row (original answer is saved verbatim)."""

    turn_id: str
    session_id: str
    client_turn_id: str
    user_id: int
    turn_no: int
    answer_text: str
    status: str
    created_at: datetime | None


@dataclass(frozen=True)
class TurnSubmission:
    """202 turn+task result; ``replayed=True`` means no second task was created."""

    turn_id: str
    session_id: str
    client_turn_id: str
    turn_no: int
    answer_text: str
    created_at: datetime | None
    replayed: bool
    task_id: str | None
    task_status: str | None
    stage: str | None
    poll_after_ms: int
    expires_at: datetime | None

    @classmethod
    def accepted(cls, turn: ProfileTurn, task: AiTaskRecord) -> TurnSubmission:
        return cls(
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            client_turn_id=turn.client_turn_id,
            turn_no=turn.turn_no,
            answer_text=turn.answer_text,
            created_at=turn.created_at,
            replayed=False,
            task_id=task.task_id,
            task_status=task.status.value,
            stage=task.stage,
            poll_after_ms=1000,
            expires_at=task.lease_until,
        )

    @classmethod
    def replay(cls, turn: ProfileTurn) -> TurnSubmission:
        return cls(
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            client_turn_id=turn.client_turn_id,
            turn_no=turn.turn_no,
            answer_text=turn.answer_text,
            created_at=turn.created_at,
            replayed=True,
            task_id=None,
            task_status=None,
            stage=None,
            poll_after_ms=0,
            expires_at=None,
        )


@dataclass(frozen=True)
class CleanupTaskSubmission:
    """202 soft-delete result: session hidden synchronously, cleanup enqueued."""

    task_id: str
    status: AiTaskStatus
    cleanup_requested: bool = True


@dataclass(frozen=True)
class ProfileDraftField:
    """One ai_profile_draft_field row surfaced to the confirm/publish boundary.

    ``confirmation_status`` and ``subject`` are plain strings so the frozen
    Task 8 contract reads naturally (``field.confirmation_status == "confirmed"``);
    enums are applied only at the API schema boundary.
    """

    field_key: str
    subject: str
    # WP-P1：条目语义。structured 行恒为默认值，旧调用方零感知；entry 行
    # value 恒为 None，正文在 content，category 受 9 枚举约束。
    field_kind: str = "structured"
    category: str | None = None
    content: str | None = None
    replaces_field_key: str | None = None
    value: Any = None
    display_value: str | None = None
    source_type: str | None = None
    source_turn_ids: tuple[str, ...] = ()
    source_span: str | None = None
    confidence: float = 0.0
    visibility: str | None = None
    consent_scope: str | None = None
    schema_version: str = PROFILE_SCHEMA_VERSION
    prompt_version: str | None = None
    content_hash: str | None = None
    confirmation_status: str = "suggested"


@dataclass(frozen=True)
class ProfileDraft:
    """One editable ai_profile_draft row plus its field candidates."""

    draft_id: str
    owner_user_id: int
    subject: str
    status: str = "draft"
    revision: int = 0
    policy_revision: str = PROFILE_POLICY_REVISION
    schema_version: str = PROFILE_SCHEMA_VERSION
    consent_snapshot: dict[str, Any] = dc_field(default_factory=dict)
    last_operation_idempotency_key: str | None = None
    last_operation_request_digest: str | None = None
    operation_history: dict[str, Any] = dc_field(default_factory=dict)
    session_id: str | None = None
    fields: tuple[ProfileDraftField, ...] = ()
    expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class PublishedRevision:
    """The immutable ai_profile_revision row created by a confirmed-only publish."""

    revision_id: int
    subject: str
    revision_no: int
    draft_id: str
    changed_field_keys: tuple[str, ...]
    published_at: datetime


@dataclass(frozen=True)
class TaskSubmission:
    """202 publish result; ``replayed=True`` means no second write happened.

    ``narrative_task_id`` carries the async narrative generation task so the
    frontend can poll it directly instead of polling the business interface
    with a fixed short window.
    """

    task_id: str
    status: str
    replayed: bool
    revision: PublishedRevision | None
    narrative_task_id: str | None = None

    @classmethod
    def accepted(
        cls,
        task: AiTaskRecord,
        revision: PublishedRevision,
        narrative_task_id: str | None = None,
    ) -> TaskSubmission:
        return cls(
            task_id=task.task_id,
            status=task.status.value,
            replayed=False,
            revision=revision,
            narrative_task_id=narrative_task_id,
        )

    @classmethod
    def replay(
        cls, task: AiTaskRecord, narrative_task_id: str | None = None
    ) -> TaskSubmission:
        return cls(
            task_id=task.task_id,
            status=task.status.value,
            replayed=True,
            revision=None,
            narrative_task_id=narrative_task_id,
        )


@dataclass(frozen=True)
class CleanupTask:
    """202 delete result: drafts/results hidden synchronously, cleanup enqueued.

    ``status`` is the plain ``ai_task.status`` string (``"queued"`` on creation)
    so the frozen contract ``task.status == "queued"`` holds directly.
    """

    task_id: str
    status: str
    subject: str
    cleanup_requested: bool = True


# ----------------------------------------------------------------------
# 输入归一化与请求摘要
# ----------------------------------------------------------------------


def normalize_profile_answer(answer_text: str) -> str:
    """Trim and validate a text answer (1..2000 chars)."""
    normalized = answer_text.strip()
    if not 1 <= len(normalized) <= 2000:
        raise AIInputError("answer_text must contain 1..2000 characters")
    return normalized


def hash_request(session_id: str, client_turn_id: str, answer_text: str) -> str:
    """Stable request digest for task idempotency; never stores raw text."""
    payload = json.dumps(
        {
            "session_id": session_id,
            "client_turn_id": client_turn_id,
            "answer_text": answer_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def hash_narrative_request(revision_id: int, subject: str) -> str:
    """Stable digest for narrative task idempotency."""
    payload = json.dumps(
        {"revision_id": revision_id, "subject": subject, "type": "narrative"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def assert_session_transition(source: ProfileSessionStatus, target: ProfileSessionStatus) -> None:
    """Raise ``ValueError`` unless the session state move is legal (§7.2)."""
    if target not in _SESSION_TRANSITIONS.get(source, set()):
        raise ValueError(
            f"illegal profile_session transition: {source.value} -> {target.value}"
        )


def next_profile_question(session: ProfileSession) -> ProfileQuestion | None:
    """Return the first missing-field question; never repeats confirmed or skipped fields.

    The question bank is ordered and fixed; the result is real coverage of the
    frozen allowlist, never a timer-based fake progress. Skipped fields stay
    unanswered (progress unchanged) and are not asked again in this session.
    """
    skipped = session.skipped_keys
    for field_key, question in _PROFILE_QUESTION_BANK.items():
        if field_key not in session.field_keys and field_key not in skipped:
            return question
    return None


def progress_value(confirmed_keys: frozenset[str]) -> float:
    """Confirmed-field coverage over the frozen allowlist (0..1)."""
    if not AI_FIELD_ALLOWLIST:
        return 0.0
    return len(confirmed_keys) / len(AI_FIELD_ALLOWLIST)


# ----------------------------------------------------------------------
# 内部辅助：SQL 读取/写入（不 commit，由调用方控制事务）
# ----------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _maybe_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


async def _first_row(result: Any) -> dict[str, Any] | None:
    return result.mappings().first()


async def _scalar(result: Any) -> Any:
    try:
        return result.scalar()
    except AttributeError:
        rows = result.mappings().all()
        if rows:
            return next(iter(rows[0].values()))
        return None


async def _load_session_row(db: AsyncSession, session_id: str) -> dict[str, Any] | None:
    result = await db.execute(
        text(f"SELECT {_SESSION_COLUMNS} FROM ai_profile_session "
             "WHERE session_id = :session_id LIMIT 1"),
        {"session_id": session_id},
    )
    return await _first_row(result)


async def _find_active_session(
    db: AsyncSession, user_id: int, subject: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(f"SELECT {_SESSION_COLUMNS} FROM ai_profile_session "
             "WHERE user_id = :user_id AND subject = :subject "
             "AND active_status = 1 LIMIT 1"),
        {"user_id": user_id, "subject": subject},
    )
    return await _first_row(result)


async def _load_consent_grant(
    db: AsyncSession, user_id: int, scope: str, version: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT user_id, scope, version, policy_revision, granted_at "
            "FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND version = :version "
            "AND revoked_at IS NULL ORDER BY granted_at DESC LIMIT 1"
        ),
        {"user_id": user_id, "scope": scope, "version": version},
    )
    return await _first_row(result)


def _consent_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    granted_at = row.get("granted_at")
    return {
        "scope": row.get("scope") or PROFILE_CONSENT_SCOPE,
        "version": row.get("version") or "",
        "policy_revision": row.get("policy_revision") or PROFILE_POLICY_REVISION,
        "granted_at": granted_at.isoformat() if granted_at else None,
    }


async def _load_revision_vector(db: AsyncSession, user_id: int) -> RevisionVector:
    result = await db.execute(
        text(
            "SELECT profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision "
            "FROM user_revision_state WHERE user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    row = await _first_row(result)
    if row is None:
        return RevisionVector()
    return RevisionVector(
        profile=int(row["profile_revision"] or 0),
        preference=int(row["preference_revision"] or 0),
        privacy=int(row["privacy_revision"] or 0),
        relationship=int(row["relationship_revision"] or 0),
        policy=int(row["policy_revision"] or 0),
    )


async def _load_field_keys(
    db: AsyncSession, session_id: str
) -> tuple[frozenset[str], frozenset[str]]:
    """Return (non-deleted field keys, confirmed field keys) of a session draft.

    只统计 structured 字段：entry 条目不计入题目推进（field_keys）与进度/
    门槛（confirmed_keys）——条目是丰富度增强，不改变建构门槛边界（WP-P1
    Global Constraint；集成测试在 _load_field_keys 消费处防回归）。
    """
    result = await db.execute(
        text(
            "SELECT df.field_key, df.confirmation_status "
            "FROM ai_profile_draft_field df "
            "JOIN ai_profile_draft d ON d.draft_id = df.draft_id "
            "WHERE d.session_id = :session_id "
            "AND df.confirmation_status <> 'deleted' "
            "AND df.field_kind = 'structured'"
        ),
        {"session_id": session_id},
    )
    field_keys: set[str] = set()
    confirmed_keys: set[str] = set()
    for row in result.mappings().all():
        field_keys.add(str(row["field_key"]))
        if str(row["confirmation_status"]) == ProfileFieldConfirmationStatus.CONFIRMED.value:
            confirmed_keys.add(str(row["field_key"]))
    return frozenset(field_keys), frozenset(confirmed_keys)


async def _load_active_draft_id_for_session(
    db: AsyncSession, session_id: str
) -> str | None:
    """Return the active draft id for a session, or ``None`` if none exists.

    Task6 Step2：会话读取路径回填 ``draft_id``。只挑选非终态（非
    published/deleted/cancelled）的最新草稿；published 之后的历史草稿只读，
    不作为「可继续编辑的 draft_id」暴露给前端。加法查询，不改变现有写入。
    """
    result = await db.execute(
        text(
            "SELECT draft_id FROM ai_profile_draft "
            "WHERE session_id = :session_id AND status IN ('draft', 'extracting', "
            "'awaiting_confirmation', 'paused') "
            "ORDER BY updated_at DESC LIMIT 1"
        ),
        {"session_id": session_id},
    )
    row = await _first_row(result)
    return str(row["draft_id"]) if row else None


def _subject(value: Any) -> ProfileSubject:
    if isinstance(value, ProfileSubject):
        return value
    return ProfileSubject(str(value))


def _parse_skipped_keys(value: Any) -> frozenset[str]:
    """Parse session.skipped_field_keys JSON into an allowlisted frozenset."""
    parsed = _maybe_json(value)
    if parsed is None:
        return frozenset()
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return frozenset()
    return frozenset(
        str(item) for item in parsed if str(item) in AI_FIELD_ALLOWLIST
    )


def _session_from_row(
    row: dict[str, Any],
    *,
    revision: RevisionVector,
    consent_snapshot: dict[str, Any],
    field_keys: frozenset[str],
    confirmed_keys: frozenset[str],
    draft_id: str | None = None,
) -> ProfileSession:
    session = ProfileSession(
        session_id=str(row["session_id"]),
        owner_user_id=int(row["user_id"]),
        subject=_subject(row["subject"]),
        status=ProfileSessionStatus(str(row["status"])),
        input_mode=str(row.get("input_mode") or "text"),
        consent_version=str(row["consent_version"]),
        policy_revision=str(row["policy_revision"]),
        current_question=None,
        revision_vector=revision,
        consent_snapshot=consent_snapshot,
        field_keys=field_keys,
        confirmed_keys=confirmed_keys,
        profile_revision=int(row.get("profile_revision") or 0),
        preference_revision=int(row.get("preference_revision") or 0),
        expires_at=row.get("expires_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        draft_id=draft_id,
        skipped_keys=_parse_skipped_keys(row.get("skipped_field_keys")),
    )
    object.__setattr__(session, "current_question", next_profile_question(session))
    return session


def _turn_from_row(row: dict[str, Any]) -> ProfileTurn:
    return ProfileTurn(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        client_turn_id=str(row["client_turn_id"]),
        user_id=int(row["user_id"]),
        turn_no=int(row["turn_no"] or 0),
        answer_text=str(row["answer_text"] or ""),
        status=str(row.get("status") or "saved"),
        created_at=row.get("created_at"),
    )


def _stored_revision(row: dict[str, Any]) -> RevisionVector:
    return RevisionVector(
        profile=int(row.get("profile_revision") or 0),
        preference=int(row.get("preference_revision") or 0),
    )


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if isinstance(expires_at, datetime):
        return expires_at.replace(tzinfo=None) < _now_utc()
    return False


async def _mark_stale(db: AsyncSession, session_id: str) -> None:
    """Mark a session stale and commit immediately.

    stale 标记是一个独立、原子的状态变更，且**总是**在抛出
    ``PROFILE_SESSION_STALE`` 之前调用。调用方（路由）的 ``commit()`` 只在成功
    分支执行，异常路径退出 ``get_db`` 上下文时会回滚未提交事务——若不在此处
    提交，过期/版本变化的会话会永远保持 ``active_status=1``，同 user+subject
    将无法重新创建会话。所有调用点都在本事务尚无其它未提交写入时执行，因此
    此处的 ``commit()`` 不会把别的写入提前固化。
    """
    await db.execute(
        text(
            "UPDATE ai_profile_session SET status = 'stale', active_status = 0, "
            "ended_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() "
            "WHERE session_id = :session_id"
        ),
        {"session_id": session_id},
    )
    await db.commit()


async def _fail_extract_session(db: AsyncSession, session_id: str) -> None:
    """Mark a session failed after a terminal extraction failure and commit.

    与 ``_mark_stale`` 同款自提交例外（第二个）：extract handler 终态失败时，
    会话状态必须立即固化——worker 对返回 None 的 handler 会回滚其事务
    （``_process`` 的 ``finalize_handler(False)``），若不在此处 commit，FAILED
    写入会被丢弃，会话将永远停留在 extracting。``WHERE status='extracting'``
    保证幂等：会话已因新 turn 回到 extracting 时不误伤，已 failed 时重复调用
    为 no-op。同事务内的 ``fail_task(retryable=False)`` 一并被固化，使 worker
    的重记撞状态守卫成为 no-op，避免不可重试失败被硬编码 ``retryable=True``
    推入重试循环。``active_status=0`` + ``ended_at`` 与 ``_mark_stale`` 的终态
    语义一致：失败会话释放 active 唯一槽，用户下次 ensureSession 建新会话。
    """
    await db.execute(
        text(
            "UPDATE ai_profile_session SET status = 'failed', "
            "active_status = 0, ended_at = UTC_TIMESTAMP(), "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE session_id = :session_id AND status = 'extracting'"
        ),
        {"session_id": session_id},
    )
    await db.commit()


async def _reuse_active_session(
    db: AsyncSession,
    row: dict[str, Any],
    *,
    revision: RevisionVector,
    consent_snapshot: dict[str, Any],
) -> ProfileSession:
    """Replay an existing active session row (create idempotency, §7.5).

    复用路径同样校验过期：已过期但仍 active 的会话按 ``PROFILE_SESSION_STALE``
    处理（与 ``load_owned_active_session`` 的语义一致），客户端重新创建。
    """
    if _is_expired(row.get("expires_at")):
        await _mark_stale(db, str(row["session_id"]))
        raise ProfileSessionStale()
    field_keys, confirmed_keys = await _load_field_keys(db, str(row["session_id"]))
    draft_id = await _load_active_draft_id_for_session(db, str(row["session_id"]))
    return _session_from_row(
        row,
        revision=revision,
        consent_snapshot=consent_snapshot,
        field_keys=field_keys,
        confirmed_keys=confirmed_keys,
        draft_id=draft_id,
    )


async def _update_session_status(
    db: AsyncSession, session_id: str, status: ProfileSessionStatus
) -> None:
    await db.execute(
        text(
            "UPDATE ai_profile_session SET status = :status, "
            "updated_at = UTC_TIMESTAMP() WHERE session_id = :session_id"
        ),
        {"status": status.value, "session_id": session_id},
    )


# ----------------------------------------------------------------------
# 会话与回答
# ----------------------------------------------------------------------


async def create_profile_session(
    db: AsyncSession,
    owner_user_id: int,
    subject: ProfileSubject,
    consent_version: str,
    idempotency_key: str,
) -> ProfileSession:
    """Create or reuse the single active session for ``user_id + subject``.

    校验 ``profile_text_extract`` 授权；已存在活动会话时回放/复用（同
    user+subject 只保留一个活动 session）。写 ai_profile_session（session_id、
    subject、status=draft、授权与版本快照、expires_at）。不 commit。
    """
    subject_value = subject.value if isinstance(subject, ProfileSubject) else str(subject)
    if subject_value not in {ProfileSubject.PERSONAL.value, ProfileSubject.IDEAL_PARTNER.value}:
        raise AIInputError("subject must be personal or ideal_partner")
    consent = await _load_consent_grant(
        db, owner_user_id, PROFILE_CONSENT_SCOPE, consent_version
    )
    if consent is None:
        raise AIConsentRequired()
    revision = await _load_revision_vector(db, owner_user_id)
    consent_snapshot = _consent_snapshot(consent)
    existing = await _find_active_session(db, owner_user_id, subject_value)
    if existing is not None:
        return await _reuse_active_session(
            db, existing, revision=revision, consent_snapshot=consent_snapshot
        )

    session_id = uuid.uuid4().hex
    expires_at = _now_utc() + timedelta(days=settings.ai_profile_session_expire_days)
    policy_revision = consent_snapshot.get("policy_revision") or PROFILE_POLICY_REVISION
    try:
        await db.execute(
            text(
                "INSERT INTO ai_profile_session "
                "(session_id, user_id, subject, input_mode, status, active_status, "
                " consent_version, policy_revision, current_question_id, "
                " profile_revision, preference_revision, expires_at, created_at, updated_at) "
                "VALUES (:session_id, :user_id, :subject, 'text', 'draft', 1, "
                " :consent_version, :policy_revision, NULL, "
                " :profile_revision, :preference_revision, :expires_at, "
                " UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "session_id": session_id,
                "user_id": owner_user_id,
                "subject": subject_value,
                "consent_version": consent_version,
                "policy_revision": policy_revision,
                "profile_revision": revision.profile,
                "preference_revision": revision.preference,
                "expires_at": expires_at,
            },
        )
    except IntegrityError:
        # 并发首次创建同 user+subject：唯一键 uk_ai_profile_session_active
        # 冲突（两个请求都通过了前置检查）。仿 enqueue_task 的
        # IntegrityError→回读回放模式：回滚本请求事务后回读既有活动会话复用，
        # 不产生第二个 session；回读仍无 → 原样上抛。rollback 只作用于本请求
        # 事务，不会误回滚赢家已提交的会话。
        await db.rollback()
        existing = await _find_active_session(db, owner_user_id, subject_value)
        if existing is None:
            raise
        return await _reuse_active_session(
            db, existing, revision=revision, consent_snapshot=consent_snapshot
        )
    row = {
        "session_id": session_id,
        "user_id": owner_user_id,
        "subject": subject_value,
        "input_mode": "text",
        "status": ProfileSessionStatus.DRAFT.value,
        "active_status": 1,
        "consent_version": consent_version,
        "policy_revision": policy_revision,
        "current_question_id": None,
        "profile_revision": revision.profile,
        "preference_revision": revision.preference,
        "expires_at": expires_at,
        "ended_at": None,
        "created_at": _now_utc(),
        "updated_at": _now_utc(),
    }
    return _session_from_row(
        row,
        revision=revision,
        consent_snapshot=consent_snapshot,
        field_keys=frozenset(),
        confirmed_keys=frozenset(),
    )


async def load_owned_session(
    db: AsyncSession, session_id: str, owner_user_id: int
) -> ProfileSession:
    """Load a session by ownership only (GET/pause/resume/delete paths)."""
    row = await _load_session_row(db, session_id)
    if row is None or int(row["user_id"]) != owner_user_id:
        raise ProfileSessionNotFound()
    revision = await _load_revision_vector(db, owner_user_id)
    field_keys, confirmed_keys = await _load_field_keys(db, session_id)
    draft_id = await _load_active_draft_id_for_session(db, session_id)
    consent = await _load_consent_grant(
        db, owner_user_id, PROFILE_CONSENT_SCOPE, str(row["consent_version"])
    )
    return _session_from_row(
        row,
        revision=revision,
        consent_snapshot=_consent_snapshot(consent) if consent else {},
        field_keys=field_keys,
        confirmed_keys=confirmed_keys,
        draft_id=draft_id,
    )


async def load_owned_active_session(
    db: AsyncSession, session_id: str, owner_user_id: int
) -> ProfileSession:
    """Load an owned session that is still usable for turn submission.

    不存在/非本人/已结束统一 404；资料或授权版本变化、过期统一 409 stale。
    """
    row = await _load_session_row(db, session_id)
    if row is None or int(row["user_id"]) != owner_user_id:
        raise ProfileSessionNotFound()
    if int(row.get("active_status") or 0) != 1:
        raise ProfileSessionNotFound()
    status = ProfileSessionStatus(str(row["status"]))
    if status is ProfileSessionStatus.STALE:
        raise ProfileSessionStale()
    if status in _STALE_STATUSES:
        raise ProfileSessionNotFound()
    if status not in _ACTIVE_FOR_TURNS:
        raise ProfileSessionNotFound()

    revision = await _load_revision_vector(db, owner_user_id)
    # 资料/偏好版本变化 → 会话 stale，需重新创建。
    stored = _stored_revision(row)
    if stored != RevisionVector(profile=revision.profile, preference=revision.preference):
        await _mark_stale(db, session_id)
        raise ProfileSessionStale()
    if _is_expired(row.get("expires_at")):
        await _mark_stale(db, session_id)
        raise ProfileSessionStale()

    consent = await _load_consent_grant(
        db, owner_user_id, PROFILE_CONSENT_SCOPE, str(row["consent_version"])
    )
    if consent is None:
        raise AIConsentRequired()
    field_keys, confirmed_keys = await _load_field_keys(db, session_id)
    draft_id = await _load_active_draft_id_for_session(db, session_id)
    return _session_from_row(
        row,
        revision=revision,
        consent_snapshot=_consent_snapshot(consent),
        field_keys=field_keys,
        confirmed_keys=confirmed_keys,
        draft_id=draft_id,
    )


async def find_turn_by_client_id(
    db: AsyncSession, session_id: str, client_turn_id: str
) -> ProfileTurn | None:
    result = await db.execute(
        text(
            f"SELECT {_TURN_COLUMNS} FROM ai_profile_turn "
            "WHERE session_id = :session_id AND client_turn_id = :client_turn_id "
            "LIMIT 1"
        ),
        {"session_id": session_id, "client_turn_id": client_turn_id},
    )
    row = await _first_row(result)
    return _turn_from_row(row) if row else None


async def _insert_turn(
    db: AsyncSession, session_id: str, user_id: int, client_turn_id: str, answer_text: str
) -> ProfileTurn:
    turn_id = uuid.uuid4().hex
    # turn_no 经 ``COUNT(*)+1`` 计算，同一会话两个并发 turn（不同 client_turn_id）
    # 可得到相同 turn_no。``client_turn_id`` 唯一键无法保护 turn_no 维度。此处对
    # ``ai_profile_session`` 行加 ``SELECT ... FOR UPDATE`` 锁，序列化同一会话的
    # turn 插入：第二个并发请求在锁释放后才能读取 COUNT，此时前一个 turn 已可见，
    # turn_no 一定递增。同时把 COUNT 换成 ``COALESCE(MAX(turn_no), 0)+1``，避免
    # 已删除/软删除 turn 对计数的影响。
    await db.execute(
        text(
            "SELECT session_id FROM ai_profile_session "
            "WHERE session_id = :session_id FOR UPDATE"
        ),
        {"session_id": session_id},
    )
    result = await db.execute(
        text(
            "SELECT COALESCE(MAX(turn_no), 0) + 1 AS next_no "
            "FROM ai_profile_turn WHERE session_id = :session_id"
        ),
        {"session_id": session_id},
    )
    turn_no = int(await _scalar(result) or 1)
    await db.execute(
        text(
            "INSERT INTO ai_profile_turn "
            "(turn_id, session_id, client_turn_id, user_id, turn_no, role, "
            " answer_text, status, source_type, created_at) "
            "VALUES (:turn_id, :session_id, :client_turn_id, :user_id, :turn_no, "
            " 'user', :answer_text, 'saved', 'user_answer', UTC_TIMESTAMP())"
        ),
        {
            "turn_id": turn_id,
            "session_id": session_id,
            "client_turn_id": client_turn_id,
            "user_id": user_id,
            "turn_no": turn_no,
            "answer_text": answer_text,
        },
    )
    return ProfileTurn(
        turn_id=turn_id,
        session_id=session_id,
        client_turn_id=client_turn_id,
        user_id=user_id,
        turn_no=turn_no,
        answer_text=answer_text,
        status="saved",
        created_at=_now_utc(),
    )


async def submit_profile_turn(
    db: AsyncSession,
    session_id: str,
    owner_user_id: int,
    client_turn_id: str,
    answer_text: str,
    idempotency_key: str,
) -> TurnSubmission:
    """Persist the original answer first, then enqueue a ``profile_extract`` task.

    同 ``client_turn_id`` 重复提交回放原 turn 且不再创建第二个 task；原文先落库，
    抽取失败不删原文。不 commit。
    """
    normalized = normalize_profile_answer(answer_text)
    session = await load_owned_active_session(db, session_id, owner_user_id)
    existing = await find_turn_by_client_id(db, session_id, client_turn_id)
    if existing is not None:
        return TurnSubmission.replay(existing)

    # 前置内容审核（Task 9）:违规文本不落库、不进 LLM prompt（与 community
    # 模块一致）。置于幂等回放分支之后——已落库 turn 的重放不因词库事后收紧
    # 被拒,回放语义优先;审核在首次落库前完成,replace 用审核后的脱敏文本
    # 替代原文,后续 hash_request/抽取 prompt 使用的均为脱敏文本。
    # ``manual_review`` 不拦截（与 community 一致,走人工审核队列）。
    moderation = await moderate_text(db, normalized, field="画像回答")
    if moderation.action == "reject":
        raise AIInputError("回答内容包含违规信息,请修改后重试")
    if moderation.action == "replace" and moderation.display_content:
        normalized = moderation.display_content

    try:
        turn = await _insert_turn(db, session_id, owner_user_id, client_turn_id, normalized)
    except IntegrityError:
        # 并发同 client_turn_id：唯一键 uk_ai_profile_turn_session_client 冲突
        # （check-then-insert 的非原子窗口）。回滚后回读原 turn 回放，不创建
        # 第二个 task。rollback 只作用于本请求事务——赢家的 turn 已在其自身
        # 事务中落库，绝不会被本请求的回滚误删；enqueue_task 内部的 rollback
        # 也不会到达这里（冲突发生在 turn 层，早于 task 入队）。
        await db.rollback()
        existing = await find_turn_by_client_id(db, session_id, client_turn_id)
        if existing is None:
            raise
        return TurnSubmission.replay(existing)

    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type="profile_extract",
        idempotency_key=idempotency_key,
        request_hash=hash_request(session_id, client_turn_id, normalized),
        revisions=session.revision_vector,
        consent=session.consent_snapshot,
    )
    # 受控摘要：只记录定位 session/turn 的引用，不含原文。
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "source_revision_json = :source_revision_json, "
            "consent_snapshot_json = :consent_snapshot_json, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                {
                    "session_id": session_id,
                    "turn_id": turn.turn_id,
                    "client_turn_id": client_turn_id,
                    "subject": session.subject.value,
                },
                ensure_ascii=False,
            ),
            "source_revision_json": json.dumps(
                session.revision_vector.as_dict(), ensure_ascii=False
            ),
            "consent_snapshot_json": json.dumps(
                session.consent_snapshot, ensure_ascii=False
            ),
            "task_id": task.task_id,
        },
    )
    if session.status is ProfileSessionStatus.DRAFT or (
        session.status is ProfileSessionStatus.AWAITING_CONFIRMATION
    ):
        assert_session_transition(session.status, ProfileSessionStatus.EXTRACTING)
        await _update_session_status(db, session_id, ProfileSessionStatus.EXTRACTING)
    await db.flush()
    return TurnSubmission.accepted(turn=turn, task=task)


# ----------------------------------------------------------------------
# 抽取（Worker handler）与草稿写入
# ----------------------------------------------------------------------


def _content_hash(field_key: str, subject: str, value: Any, source_turn_ids: tuple[str, ...]) -> str:
    payload = json.dumps(
        {
            "field_key": field_key,
            "subject": subject,
            "value": value,
            "source_turn_ids": list(source_turn_ids),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _incoming_extract_fields(result: Any) -> tuple[Any, ...]:
    return tuple(getattr(result, "fields", ()) or ())


def _incoming_extract_entries(result: Any) -> tuple[Any, ...]:
    return tuple(getattr(result, "entries", ()) or ())


def validate_entry_content(content: Any) -> str:
    """entry 正文校验：1..200 字非空白文本，超限/类型错误 AI_INPUT_INVALID。

    服务层校验与 DB VARCHAR(200) 构成双保险；strip 后落库，保证
    entry_digest 与读取端拿到的是归一化正文。
    """
    if not isinstance(content, str):
        raise AIInputError("entry content 必须是文本")
    normalized = content.strip()
    if not 1 <= len(normalized) <= PROFILE_ENTRY_CONTENT_MAX_LENGTH:
        raise AIInputError(
            f"entry content 长度须为 1..{PROFILE_ENTRY_CONTENT_MAX_LENGTH} 字"
        )
    return normalized


def _fallback_tag_field(session: ProfileSession, turn: ProfileTurn) -> ExtractedField | None:
    """口语化回答抽不出字段时，把本轮所问的标签字段写成 suggested。"""
    question = session.current_question
    if question is None or question.field_key not in _TAG_LIST_FIELDS:
        return None
    answer = (turn.answer_text or "").strip()
    if not answer:
        return None
    return ExtractedField(
        field_key=question.field_key,
        subject=session.subject,
        value=(answer,),
        source_quote=answer,
        source_span=answer,
        confidence=0.4,
        needs_confirmation=True,
        confirmation_status=ProfileFieldConfirmationStatus.SUGGESTED.value,
        schema_version=PROFILE_SCHEMA_VERSION,
        prompt_version=PROFILE_PROMPT_VERSION,
        policy_revision=session.policy_revision or PROFILE_POLICY_REVISION,
    )


async def _copy_unreplaced_draft_fields(
    db: AsyncSession,
    *,
    source_draft_id: str,
    target_draft_id: str,
    replaced_keys: set[str],
) -> None:
    rows = await _load_draft_field_rows(db, source_draft_id)
    for row in rows:
        field_key = str(row["field_key"])
        if field_key in replaced_keys:
            continue
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft_field "
                "(draft_id, field_key, subject, field_kind, category, content, "
                " replaces_field_key, value_json, display_value, source_type, "
                " source_turn_ids, source_span, confidence, visibility, consent_scope, schema_version, "
                " prompt_version, content_hash, confirmation_status, created_at, updated_at) "
                "VALUES (:draft_id, :field_key, :subject, :field_kind, :category, :content, "
                " :replaces_field_key, :value_json, :display_value, "
                " :source_type, :source_turn_ids, :source_span, :confidence, :visibility, "
                " :consent_scope, :schema_version, :prompt_version, :content_hash, "
                " :confirmation_status, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "draft_id": target_draft_id,
                "field_key": field_key,
                "subject": row.get("subject"),
                "field_kind": row.get("field_kind") or "structured",
                "category": row.get("category"),
                "content": row.get("content"),
                "replaces_field_key": row.get("replaces_field_key"),
                "value_json": (
                    row.get("value_json")
                    if isinstance(row.get("value_json"), str)
                    else json.dumps(row.get("value_json") or row.get("value"), ensure_ascii=False)
                ),
                "display_value": row.get("display_value"),
                "source_type": row.get("source_type") or "user_answer",
                "source_turn_ids": (
                    row.get("source_turn_ids")
                    if isinstance(row.get("source_turn_ids"), str)
                    else json.dumps(list(row.get("source_turn_ids") or []), ensure_ascii=False)
                ),
                "source_span": row.get("source_span"),
                "confidence": float(row.get("confidence") or 0.0),
                "visibility": row.get("visibility") or "self",
                "consent_scope": row.get("consent_scope") or PROFILE_CONSENT_SCOPE,
                "schema_version": row.get("schema_version") or PROFILE_SCHEMA_VERSION,
                "prompt_version": row.get("prompt_version") or PROFILE_PROMPT_VERSION,
                "content_hash": row.get("content_hash"),
                "confirmation_status": row.get("confirmation_status") or "suggested",
            },
        )


async def _write_draft(
    db: AsyncSession,
    session: ProfileSession,
    turn: ProfileTurn,
    result: Any,
) -> str:
    """Persist one suggested draft with full source evidence; returns draft_id."""
    draft_id = uuid.uuid4().hex
    consent_snapshot = session.consent_snapshot or {}
    await db.execute(
        text(
            "INSERT INTO ai_profile_draft "
            "(draft_id, user_id, subject, session_id, status, expected_revision, "
            " consent_snapshot_json, policy_revision, prompt_version, schema_version, "
            " expires_at, created_at, updated_at) "
            "VALUES (:draft_id, :user_id, :subject, :session_id, 'draft', 0, "
            " :consent_snapshot_json, :policy_revision, :prompt_version, :schema_version, "
            " NULL, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ),
        {
            "draft_id": draft_id,
            "user_id": session.owner_user_id,
            "subject": session.subject.value,
            "session_id": session.session_id,
            "consent_snapshot_json": json.dumps(consent_snapshot, ensure_ascii=False),
            "policy_revision": session.policy_revision or PROFILE_POLICY_REVISION,
            "prompt_version": PROFILE_PROMPT_VERSION,
            "schema_version": PROFILE_SCHEMA_VERSION,
        },
    )
    source_turn_ids = (turn.turn_id,)
    consent_scope = consent_snapshot.get("scope") or PROFILE_CONSENT_SCOPE
    written_keys: set[str] = set()
    for field in _incoming_extract_fields(result):
        value = getattr(field, "value", None)
        # 主体隔离在字段标签层强制：写草稿字段一律以会话 subject 为准，忽略
        # provider 返回的 subject（mock provider 恒返回 personal，若信任它，
        # ideal_partner 会话的草稿字段会被错标成 personal）。不一致时记录但
        # 不改变会话 subject。
        subject = session.subject.value
        provider_subject = getattr(field, "subject", None)
        if provider_subject and provider_subject != subject:
            logger.warning(
                "ai_draft_field_subject_overridden session_id=%s field_key=%s "
                "provider_subject=%s forced_subject=%s",
                session.session_id,
                field.field_key,
                provider_subject,
                subject,
            )
        # 认证字段不在 allowlist，Schema/网关已拒绝；此处再做一道兜底。
        if field.field_key not in AI_FIELD_ALLOWLIST:
            continue
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft_field "
                "(draft_id, field_key, subject, value_json, display_value, source_type, "
                " source_turn_ids, source_span, confidence, visibility, consent_scope, schema_version, "
                " prompt_version, content_hash, confirmation_status, created_at, updated_at) "
                "VALUES (:draft_id, :field_key, :subject, :value_json, :display_value, "
                " 'user_answer', :source_turn_ids, :source_span, :confidence, :visibility, "
                " :consent_scope, :schema_version, :prompt_version, :content_hash, "
                " 'suggested', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "draft_id": draft_id,
                "field_key": field.field_key,
                "subject": subject,
                "value_json": json.dumps(value, ensure_ascii=False),
                "display_value": _display_value(value),
                "source_turn_ids": json.dumps(list(source_turn_ids), ensure_ascii=False),
                "source_span": getattr(field, "source_span", None)
                or getattr(field, "source_quote", None),
                "confidence": float(field.confidence),
                "visibility": "self",
                "consent_scope": consent_scope,
                "schema_version": getattr(field, "schema_version", None) or PROFILE_SCHEMA_VERSION,
                "prompt_version": PROFILE_PROMPT_VERSION,
                "content_hash": _content_hash(field.field_key, subject, value, source_turn_ids),
            },
        )
        written_keys.add(field.field_key)
    # WP-P1：条目候选写入。entry 的 field_key 在写入层生成（provider 不产出），
    # 形如 entry_{category}_{8hex}，满足 (draft_id, field_key) 唯一键；
    # value_json 恒 NULL，正文在 content，门槛/进度不消费（_load_field_keys 过滤）。
    for entry in _incoming_extract_entries(result):
        subject = session.subject.value
        content = validate_entry_content(getattr(entry, "content", None))
        category = getattr(entry, "category", "")
        if category not in PROFILE_ENTRY_CATEGORIES:
            raise AIInputError("entry category 非法")
        entry_key = f"entry_{category}_{uuid.uuid4().hex[:8]}"
        entry_turn_ids = (turn.turn_id,)
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft_field "
                "(draft_id, field_key, subject, field_kind, category, content, "
                " value_json, display_value, source_type, "
                " source_turn_ids, source_span, confidence, visibility, consent_scope, "
                " schema_version, prompt_version, content_hash, confirmation_status, "
                " created_at, updated_at) "
                "VALUES (:draft_id, :field_key, :subject, 'entry', :category, :content, "
                " NULL, :display_value, 'user_answer', :source_turn_ids, :source_span, "
                " :confidence, 'self', :consent_scope, :schema_version, :prompt_version, "
                " :content_hash, 'suggested', UTC_TIMESTAMP(), UTC_TIMESTAMP())"
            ),
            {
                "draft_id": draft_id,
                "field_key": entry_key,
                "subject": subject,
                "category": category,
                "content": content,
                "display_value": content,
                "source_turn_ids": json.dumps(list(entry_turn_ids), ensure_ascii=False),
                "source_span": getattr(entry, "source_span", None)
                or getattr(entry, "source_quote", None),
                "confidence": float(entry.confidence),
                "consent_scope": consent_scope,
                "schema_version": getattr(entry, "schema_version", None)
                or PROFILE_SCHEMA_VERSION,
                "prompt_version": PROFILE_PROMPT_VERSION,
                "content_hash": _content_hash(entry_key, subject, content, entry_turn_ids),
            },
        )
    previous_draft_id = session.draft_id
    if previous_draft_id:
        await _copy_unreplaced_draft_fields(
            db,
            source_draft_id=previous_draft_id,
            target_draft_id=draft_id,
            replaced_keys=written_keys,
        )
    return draft_id


def _display_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


async def extract_profile_turn(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """Worker handler for ``task_type == "profile_extract"``.

    只调用 ``AIGateway.structured_extract``；结果只写 ``suggested`` 草稿字段，
    不产生已发布字段。失败（schema-invalid/timeout/…）只改变任务状态并返回
    ``None``；成功时推进会话状态 ``extracting -> awaiting_confirmation`` 并返回
    ``(result_ref, revisions)`` 交给 Worker 的 ``complete_task`` 版本复核。
    """
    payload = task.payload_summary or {}
    session_id = payload.get("session_id")
    turn_id = payload.get("turn_id")
    if not session_id or not turn_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_FEATURE_DISABLED", retryable=False,
        )
        # payload 残缺是终态失败：session_id 可定位时同样自提交置 FAILED，
        # 否则该会话将永远停在 extracting（payload 完全没有 session_id 时
        # 无会话可标记，跳过）。
        if session_id:
            await _fail_extract_session(db, str(session_id))
        return None

    session = await load_owned_session(db, str(session_id), task.owner_user_id)
    turn = await find_turn_by_client_id(db, session.session_id, str(payload.get("client_turn_id") or ""))
    if turn is None or turn.turn_id != str(turn_id):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        # turn 定位失败同样是终态失败：自提交置 FAILED，避免会话卡在 extracting。
        await _fail_extract_session(db, session.session_id)
        return None

    context = AITaskContext(
        task_id=task.task_id,
        request_id=uuid.uuid4().hex,
        scene="profile_extract",
        provider=settings.ai_provider_name,
        model=settings.ai_model_name,
        prompt_version=PROFILE_PROMPT_VERSION,
        schema_version=PROFILE_SCHEMA_VERSION,
        input_revision=task.source_revision_json or {},
    )
    request = StructuredExtractRequest(
        subject=session.subject.value,
        turn_texts=(turn.answer_text,),
        consent_version=session.consent_version,
        policy_revision=session.policy_revision or PROFILE_POLICY_REVISION,
        target_field_key=(
            session.current_question.field_key if session.current_question else None
        ),
    )
    gateway = AIGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
    outcome = await gateway.structured_extract(context, request)
    if outcome.result is None:
        # 只改任务状态（fail_task/retry_wait），不产生草稿字段。
        await fail_task(
            db, task.task_id, worker_id,
            error_code=outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
            retryable=outcome.retryable,
        )
        # 不可重试的终态失败：自提交把会话从 extracting 推进到 failed，并连同
        # 上面的 fail_task(retryable=False) 一起固化——worker 对返回 None 的
        # handler 会回滚其事务，不在此处 commit 则 FAILED 永远落不了库，前端
        # 会一直显示"提取中"；fail_task 不固化则 worker 的重记（硬编码
        # retryable=True）会把它推进重试循环。可重试的失败由 worker 退避后
        # 重试，会话状态不动。extracting 守卫由 helper 的 WHERE 条件承担。
        if not outcome.retryable:
            await _fail_extract_session(db, session.session_id)
        return None

    expected_subject = session.subject
    expected_policy_revision = session.policy_revision or PROFILE_POLICY_REVISION
    try:
        fields = tuple(outcome.result.fields)
        if outcome.result.schema_version != PROFILE_SCHEMA_VERSION:
            raise ValueError("provider result schema version does not match")
        for field in fields:
            # Revalidate at the worker boundary as well as in Pydantic.  A
            # provider adapter can return a model created with ``model_construct``
            # or another bypass, so the draft writer must never silently filter
            # an unknown/authentication field.
            if not isinstance(field.subject, ProfileSubject):
                raise TypeError("provider subject is not typed")
            if field.subject is not expected_subject:
                raise ValueError("provider subject does not match session")
            if field.schema_version != PROFILE_SCHEMA_VERSION:
                raise ValueError("provider schema version does not match")
            if field.prompt_version != PROFILE_PROMPT_VERSION:
                raise ValueError("provider prompt version does not match")
            if field.policy_revision != expected_policy_revision:
                raise ValueError("provider policy revision does not match")
            field.value = normalize_profile_extracted_value(
                field.subject, field.field_key, field.value
            )
            if isinstance(field.confidence, bool):
                raise TypeError("provider confidence must be numeric")
            confidence = float(field.confidence)
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise ValueError("provider confidence is outside the allowed range")
            source_span = getattr(field, "source_span", None)
            source_quote = getattr(field, "source_quote", None)
            if source_span is not None and not isinstance(source_span, str):
                raise ValueError("provider source span must be text")
            if source_quote is not None and not isinstance(source_quote, str):
                raise ValueError("provider source quote must be text")
            if source_span is not None and source_quote is not None and source_span != source_quote:
                raise ValueError("provider source evidence does not agree")
            if field.confirmation_status != ProfileFieldConfirmationStatus.SUGGESTED.value:
                raise ValueError("provider confirmation status is not suggested")
            if not field.needs_confirmation:
                raise ValueError("provider field does not require confirmation")
        # entry 与 structured 字段同一套边界复核纪律：provider 适配器可能用
        # model_construct 绕过校验，写入层绝不静默放行非法条目。
        entries = tuple(outcome.result.entries)
        for entry in entries:
            if not isinstance(entry.subject, ProfileSubject):
                raise TypeError("provider subject is not typed")
            if entry.subject is not expected_subject:
                raise ValueError("provider subject does not match session")
            if entry.schema_version != PROFILE_SCHEMA_VERSION:
                raise ValueError("provider schema version does not match")
            if entry.prompt_version != PROFILE_PROMPT_VERSION:
                raise ValueError("provider prompt version does not match")
            if entry.policy_revision != expected_policy_revision:
                raise ValueError("provider policy revision does not match")
            if entry.category not in PROFILE_ENTRY_CATEGORIES:
                raise ValueError("provider entry category is not in the allowlist")
            validate_entry_content(entry.content)
            if isinstance(entry.confidence, bool):
                raise TypeError("provider confidence must be numeric")
            confidence = float(entry.confidence)
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise ValueError("provider confidence is outside the allowed range")
            entry_span = getattr(entry, "source_span", None)
            entry_quote = getattr(entry, "source_quote", None)
            if entry_span is not None and not isinstance(entry_span, str):
                raise ValueError("provider source span must be text")
            if entry_quote is not None and not isinstance(entry_quote, str):
                raise ValueError("provider source quote must be text")
            if (
                entry_span is not None
                and entry_quote is not None
                and entry_span != entry_quote
            ):
                raise ValueError("provider source evidence does not agree")
            if entry.confirmation_status != ProfileFieldConfirmationStatus.SUGGESTED.value:
                raise ValueError("provider entry confirmation status is not suggested")
            if not entry.needs_confirmation:
                raise ValueError("provider entry does not require confirmation")
        if not fields and not entries:
            fallback = _fallback_tag_field(session, turn)
            if fallback is None:
                raise ValueError("provider returned no extractable fields")
            fields = (fallback,)
            extract_result = outcome.result.model_copy(update={"fields": fields})
        else:
            extract_result = outcome.result
    except (AttributeError, TypeError, ValueError):
        # Provider provenance must describe this exact session.  Do not rewrite
        # a foreign subject or accept fabricated version evidence into a draft.
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        # 伪造来源/版本证据是终态失败：自提交置 FAILED 并固化 fail_task。
        await _fail_extract_session(db, session.session_id)
        return None

    draft_id = await _write_draft(db, session, turn, extract_result)
    if session.status is ProfileSessionStatus.EXTRACTING:
        assert_session_transition(
            session.status, ProfileSessionStatus.AWAITING_CONFIRMATION
        )
        await _update_session_status(
            db, session.session_id, ProfileSessionStatus.AWAITING_CONFIRMATION
        )
    return f"profile-draft:{draft_id}", session.revision_vector


# ----------------------------------------------------------------------
# 暂停 / 恢复 / 软删除
# ----------------------------------------------------------------------


async def skip_profile_question(
    db: AsyncSession,
    session_id: str,
    owner_user_id: int,
    field_key: str,
    idempotency_key: str,
) -> ProfileSession:
    """Skip the current interview question without confirming a field.

    跳过不写草稿、不计入 confirmed 覆盖度；同一会话内该字段不再追问。
    重复跳过当前已跳过字段时幂等返回当前会话。不 commit。
    """
    del idempotency_key
    session = await load_owned_active_session(db, session_id, owner_user_id)
    if session.current_question is None:
        raise AIInputError("当前没有可跳过的问题")
    if field_key not in AI_FIELD_ALLOWLIST:
        raise AIInputError("field_key 不在允许的画像字段内")
    if session.current_question.field_key != field_key:
        raise AIInputError("只能跳过当前问题")
    if field_key in session.skipped_keys:
        return session
    skipped = sorted(session.skipped_keys | {field_key})
    await db.execute(
        text(
            "UPDATE ai_profile_session SET skipped_field_keys = :skipped_field_keys, "
            "updated_at = UTC_TIMESTAMP() WHERE session_id = :session_id"
        ),
        {
            "session_id": session_id,
            "skipped_field_keys": json.dumps(skipped, ensure_ascii=False),
        },
    )
    return await load_owned_session(db, session_id, owner_user_id)


async def pause_profile_session(
    db: AsyncSession, session_id: str, owner_user_id: int
) -> ProfileSession:
    """Pause a session only from draft/extracting/awaiting_confirmation.

    重复暂停返回当前状态；stale 会话 409，已结束会话按 404 处理。
    """
    session = await load_owned_session(db, session_id, owner_user_id)
    if session.status is ProfileSessionStatus.STALE:
        raise ProfileSessionStale()
    if session.status in _STALE_STATUSES:
        raise ProfileSessionNotFound()
    if _is_expired(session.expires_at):
        await _mark_stale(db, session_id)
        raise ProfileSessionStale()
    if session.status is ProfileSessionStatus.PAUSED:
        return session
    if session.status not in _PAUSEABLE:
        raise ProfileSessionNotFound()
    assert_session_transition(session.status, ProfileSessionStatus.PAUSED)
    await _update_session_status(db, session_id, ProfileSessionStatus.PAUSED)
    return await load_owned_session(db, session_id, owner_user_id)


async def resume_profile_session(
    db: AsyncSession, session_id: str, owner_user_id: int
) -> ProfileSession:
    """Resume a paused session; stale or expired sessions are 409.

    非 stale/cancelled 均可恢复；恢复不改变已保存 turn。暂停前的真实状态无法在
    冻结的 session 表上持久化，因此恢复到 draft（有草稿字段则 awaiting_confirmation）。
    """
    session = await load_owned_session(db, session_id, owner_user_id)
    if session.status is ProfileSessionStatus.STALE:
        raise ProfileSessionStale()
    if session.status is ProfileSessionStatus.CANCELLED:
        raise ProfileSessionNotFound()
    if _is_expired(session.expires_at):
        await _mark_stale(db, session_id)
        raise ProfileSessionStale()
    if session.status is ProfileSessionStatus.PAUSED:
        target = (
            ProfileSessionStatus.AWAITING_CONFIRMATION
            if session.field_keys
            else ProfileSessionStatus.DRAFT
        )
        assert_session_transition(session.status, target)
        await _update_session_status(db, session_id, target)
    return await load_owned_session(db, session_id, owner_user_id)


async def delete_profile_session(
    db: AsyncSession,
    session_id: str,
    owner_user_id: int,
    idempotency_key: str,
) -> CleanupTaskSubmission:
    """Soft-delete a session synchronously and enqueue a ``cleanup`` task.

    软删除幂等：会话先隐藏（active_status=0），重复删除回放同一 cleanup task
    （同 key）。已发布 revision 不隐式删除（Task 8 处理清理与删除传播）。
    """
    session = await load_owned_session(db, session_id, owner_user_id)
    await db.execute(
        text(
            "UPDATE ai_profile_session SET status = 'cancelled', active_status = 0, "
            "ended_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() "
            "WHERE session_id = :session_id"
        ),
        {"session_id": session_id},
    )
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type="cleanup",
        idempotency_key=idempotency_key,
        request_hash=hash_request(session_id, "delete", ""),
        revisions=session.revision_vector,
        consent=None,
    )
    return CleanupTaskSubmission(
        task_id=task.task_id, status=task.status
    )


# ----------------------------------------------------------------------
# Task 8：草稿读取、字段确认、confirmed-only 发布、历史与删除传播
# ----------------------------------------------------------------------


def confirmed_fields(draft: ProfileDraft) -> tuple[ProfileDraftField, ...]:
    """Return only the ``confirmed`` fields of a draft.

    This is the single filter that guarantees unconfirmed fields never enter a
    published revision or any downstream projection (§7.4「发布只接受 confirmed
    字段」).
    """
    return tuple(
        field for field in draft.fields
        if field.confirmation_status == ProfileFieldConfirmationStatus.CONFIRMED.value
    )


def ensure_revision(current: int, expected: int) -> None:
    """Raise ``DraftVersionConflict`` unless the optimistic lock matches."""
    if int(current) != int(expected):
        raise DraftVersionConflict()


# 可编辑/可发布草稿状态白名单。deleted/published/cancelled 等终态草稿只读
# （文档 §7「已发布/已删除草稿只读」），不允许再 PATCH 或 publish。
_DRAFT_EDITABLE_STATUSES = frozenset({"draft", "awaiting_confirmation"})


def min_confirmed_fields_to_publish() -> int:
    """发布门槛：至少确认字段数，来自 settings.ai_profile_min_fields（默认 7）。

    良配对齐：默认 7/10 ≈ 67%——无需完成全部题目，进度 67% 左右即可提前
    建构画像。进度提示（can_early_publish）与发布硬门槛共用此值，避免两套
    数字漂移。
    """
    return settings.ai_profile_min_fields


def ensure_draft_editable(draft: ProfileDraft, operation: str) -> None:
    """Reject confirm/publish on terminal read-only drafts (docs §7).

    ``operation`` is a short Chinese label like "确认" / "发布" used only in the
    safe error message.  Guard runs before ``ensure_revision`` so a deleted
    draft is always rejected with ``409 RESULT_STALE`` regardless of the
    client-supplied expected_revision (delete does not bump it, so a stale
    client could otherwise republish with the original revision).
    """
    if draft.status not in _DRAFT_EDITABLE_STATUSES:
        raise DraftStatusConflict(
            f"草稿状态 {draft.status} 不可{operation}（已发布/已删除草稿只读）"
        )


def hash_publish_request(draft_id: str, expected_revision: int) -> str:
    """Stable digest of a publish request for idempotent task replay."""
    payload = json.dumps(
        {"draft_id": draft_id, "expected_revision": int(expected_revision)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def hash_profile_delete(subject: str, field_key: str | None = None) -> str:
    """Stable digest of a delete request for idempotent task replay."""
    payload = json.dumps(
        {"subject": subject, "field_key": field_key},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def hash_restore_request(revision_id: int, owner_user_id: int) -> str:
    """Stable digest of a restore request for idempotent replay (缺陷 15)."""
    payload = json.dumps(
        {"revision_id": int(revision_id), "owner_user_id": int(owner_user_id)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def hash_profile_patch_request(
    draft_id: str, expected_revision: int, actions: list[ProfileFieldPatchAction]
) -> str:
    payload = json.dumps(
        {
            "draft_id": draft_id,
            "expected_revision": int(expected_revision),
            "actions": [action.model_dump(mode="json") for action in actions],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cleanup_purge_deadline() -> str:
    return (_now_utc() + timedelta(minutes=15)).isoformat()


def _cleanup_payload(
    *,
    scope: str,
    resource_id: str,
    version: RevisionVector,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "resource_id": resource_id,
        "version": version.as_dict(),
        "purge_deadline": _cleanup_purge_deadline(),
    }


def _parse_cleanup_resource_id(resource_id: str) -> dict[str, str]:
    parts = str(resource_id).split(":")
    if not parts:
        return {}
    kind = parts[0]
    if kind == "profile" and len(parts) >= 3:
        return {"kind": kind, "user_id": parts[1], "subject": parts[2]}
    if kind == "field" and len(parts) >= 4:
        return {
            "kind": kind,
            "user_id": parts[1],
            "subject": parts[2],
            "field_key": parts[3],
        }
    if kind == "snapshot" and len(parts) >= 3:
        return {"kind": kind, "user_id": parts[1], "snapshot_id": parts[2]}
    if kind == "consent" and len(parts) >= 3:
        return {"kind": kind, "user_id": parts[1], "consent_scope": parts[2]}
    return {"kind": kind}


async def _find_write_task(
    db: AsyncSession, owner_user_id: int, task_type: str, idempotency_key: str
) -> AiTaskRecord | None:
    """Look up an already enqueued write task before replaying it idempotently."""
    result = await db.execute(
        text(
            f"SELECT {_TASK_COLUMNS} FROM ai_task "
            "WHERE owner_user_id = :owner_user_id AND task_type = :task_type "
            "AND idempotency_key = :idempotency_key LIMIT 1"
        ),
        {
            "owner_user_id": owner_user_id,
            "task_type": task_type,
            "idempotency_key": idempotency_key,
        },
    )
    row = await _first_row(result)
    return AiTaskRecord.from_row(row) if row else None


def _replay_or_conflict(
    existing: AiTaskRecord, request_hash: str, message: str
) -> AiTaskRecord:
    """Return the existing task when the digest matches, else 409 conflict."""
    if existing.request_digest != request_hash:
        raise TaskError(
            code="TASK_IDEMPOTENCY_CONFLICT",
            message="Idempotency-Key 已用于不同请求内容",
            status_code=409,
        )
    return existing


def _validate_and_normalize_replace_value(
    subject: ProfileSubject, field_key: str, value: Any
) -> Any:
    """Validate and normalize a ``replace`` value using the same frozen
    subject contract as extraction (统一方案 §6.2 值域/长度/枚举).

    标签类字段要求非空字符串数组；其余 allowlist 字段要求非空标量。
    枚举/区间/类型约束与抽取边界 ``normalize_profile_extracted_value`` 完全
    一致——用户手改路径不得绕过机器生成路径的校验，否则脏值可直达不可变
    revision 与搜索投影。来源引用在 replace 时保留（只改 value_json/
    content_hash，不动 source_turn_ids）。
    """
    try:
        return normalize_profile_extracted_value(subject, field_key, value)
    except ValueError as exc:
        raise AIInputError(str(exc)) from exc


async def _load_draft_row(
    db: AsyncSession, draft_id: str, *, for_update: bool = False
) -> dict[str, Any] | None:
    lock = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_DRAFT_COLUMNS} FROM ai_profile_draft "
            f"WHERE draft_id = :draft_id LIMIT 1{lock}"
        ),
        {"draft_id": draft_id},
    )
    return await _first_row(result)


async def _load_draft_field_rows(
    db: AsyncSession, draft_id: str
) -> list[dict[str, Any]]:
    result = await db.execute(
        text(
            f"SELECT {_DRAFT_FIELD_COLUMNS} FROM ai_profile_draft_field "
            "WHERE draft_id = :draft_id ORDER BY created_at ASC"
        ),
        {"draft_id": draft_id},
    )
    return result.mappings().all()


def _draft_field_from_row(row: dict[str, Any]) -> ProfileDraftField:
    source_turn_ids = row.get("source_turn_ids")
    return ProfileDraftField(
        field_key=str(row["field_key"]),
        subject=str(row["subject"]),
        field_kind=str(row.get("field_kind") or "structured"),
        category=row.get("category"),
        content=row.get("content"),
        replaces_field_key=row.get("replaces_field_key"),
        value=_maybe_json(row.get("value_json")),
        display_value=row.get("display_value"),
        source_type=str(row.get("source_type") or "user_answer"),
        source_turn_ids=(
            tuple(json.loads(source_turn_ids)) if source_turn_ids else ()
        ),
        source_span=row.get("source_span"),
        confidence=float(row.get("confidence") or 0.0),
        visibility=row.get("visibility"),
        consent_scope=row.get("consent_scope"),
        schema_version=str(row.get("schema_version") or PROFILE_SCHEMA_VERSION),
        prompt_version=row.get("prompt_version"),
        content_hash=row.get("content_hash"),
        confirmation_status=str(row.get("confirmation_status") or "suggested"),
    )


def _operation_history(row: dict[str, Any]) -> dict[str, Any]:
    raw = _maybe_json(row.get("last_operation_response_json"))
    if isinstance(raw, dict) and isinstance(raw.get("operations"), dict):
        return raw
    return {"operations": {}}


def _draft_response_payload(draft: ProfileDraft) -> dict[str, Any]:
    return {
        "draft_id": draft.draft_id,
        "owner_user_id": draft.owner_user_id,
        "subject": draft.subject,
        "status": draft.status,
        "revision": draft.revision,
        "policy_revision": draft.policy_revision,
        "schema_version": draft.schema_version,
        "consent_snapshot": draft.consent_snapshot,
        "session_id": draft.session_id,
        "fields": [
            {
                "field_key": item.field_key,
                "subject": item.subject,
                "field_kind": item.field_kind,
                "category": item.category,
                "content": item.content,
                "replaces_field_key": item.replaces_field_key,
                "value": item.value,
                "display_value": item.display_value,
                "source_type": item.source_type,
                "source_turn_ids": list(item.source_turn_ids),
                "source_span": item.source_span,
                "confidence": item.confidence,
                "visibility": item.visibility,
                "consent_scope": item.consent_scope,
                "schema_version": item.schema_version,
                "prompt_version": item.prompt_version,
                "content_hash": item.content_hash,
                "confirmation_status": item.confirmation_status,
            }
            for item in draft.fields
        ],
    }


def _draft_from_response_payload(
    payload: dict[str, Any], fallback: ProfileDraft
) -> ProfileDraft:
    fields = tuple(
        ProfileDraftField(
            field_key=str(item["field_key"]),
            subject=str(item.get("subject") or fallback.subject),
            value=item.get("value"),
            display_value=item.get("display_value"),
            source_type=item.get("source_type"),
            source_turn_ids=tuple(item.get("source_turn_ids") or ()),
            source_span=item.get("source_span"),
            confidence=float(item.get("confidence") or 0.0),
            visibility=item.get("visibility"),
            consent_scope=item.get("consent_scope"),
            schema_version=str(item.get("schema_version") or PROFILE_SCHEMA_VERSION),
            prompt_version=item.get("prompt_version"),
            content_hash=item.get("content_hash"),
            confirmation_status=str(item.get("confirmation_status") or "suggested"),
        )
        for item in payload.get("fields") or ()
        if isinstance(item, dict) and item.get("field_key")
    )
    if not fields:
        return fallback
    return ProfileDraft(
        draft_id=str(payload.get("draft_id") or fallback.draft_id),
        owner_user_id=int(payload.get("owner_user_id") or fallback.owner_user_id),
        subject=str(payload.get("subject") or fallback.subject),
        status=str(payload.get("status") or fallback.status),
        revision=int(payload.get("revision") or 0),
        policy_revision=str(payload.get("policy_revision") or fallback.policy_revision),
        schema_version=str(payload.get("schema_version") or fallback.schema_version),
        consent_snapshot=(
            payload.get("consent_snapshot")
            if isinstance(payload.get("consent_snapshot"), dict)
            else fallback.consent_snapshot
        ),
        operation_history=fallback.operation_history,
        session_id=payload.get("session_id") or fallback.session_id,
        fields=fields,
        expires_at=fallback.expires_at,
        created_at=fallback.created_at,
        updated_at=fallback.updated_at,
    )


def _draft_from_row(
    row: dict[str, Any], fields: list[dict[str, Any]]
) -> ProfileDraft:
    return ProfileDraft(
        draft_id=str(row["draft_id"]),
        owner_user_id=int(row["user_id"]),
        subject=str(row["subject"]),
        status=str(row.get("status") or "draft"),
        revision=int(row.get("expected_revision") or 0),
        policy_revision=str(row.get("policy_revision") or PROFILE_POLICY_REVISION),
        schema_version=str(row.get("schema_version") or PROFILE_SCHEMA_VERSION),
        consent_snapshot=(
            _maybe_json(row.get("consent_snapshot_json"))
            if isinstance(_maybe_json(row.get("consent_snapshot_json")), dict)
            else {}
        ),
        last_operation_idempotency_key=row.get("last_operation_idempotency_key"),
        last_operation_request_digest=row.get("last_operation_request_digest"),
        operation_history=_operation_history(row),
        session_id=str(row["session_id"]) if row.get("session_id") else None,
        fields=tuple(_draft_field_from_row(f) for f in fields),
        expires_at=row.get("expires_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


async def load_owned_draft(
    db: AsyncSession, draft_id: str, owner_user_id: int
) -> ProfileDraft:
    """Read a draft by ownership only (GET path); missing/foreign is a uniform 404."""
    row = await _load_draft_row(db, draft_id)
    if row is None or int(row["user_id"]) != owner_user_id:
        raise ProfileDraftNotFound()
    fields = await _load_draft_field_rows(db, draft_id)
    return _draft_from_row(row, fields)


async def load_owned_draft_for_update(
    db: AsyncSession, draft_id: str, owner_user_id: int
) -> ProfileDraft:
    """Lock a draft row for a PATCH/publish transaction under its ownership."""
    row = await _load_draft_row(db, draft_id, for_update=True)
    if row is None or int(row["user_id"]) != owner_user_id:
        raise ProfileDraftNotFound()
    fields = await _load_draft_field_rows(db, draft_id)
    return _draft_from_row(row, fields)


async def confirm_profile_draft(
    db: AsyncSession,
    draft_id: str,
    owner_user_id: int,
    actions: list[ProfileFieldPatchAction],
    expected_revision: int,
    idempotency_key: str = "",
) -> ProfileDraft:
    """Apply per-field confirm/replace/reject/delete actions under optimistic lock.

    每个 action 都携带旧 revision（不匹配 → ``409 DRAFT_VERSION_CONFLICT``）；
    replace 重新过字段 Schema/来源约束（值域校验，来源引用保留）；delete 只把
    字段标记为 ``deleted``（不可见）；reject 标记 ``rejected``。成功后草稿
    ``expected_revision + 1``，返回新草稿。不 commit。
    """
    draft = await load_owned_draft_for_update(db, draft_id, owner_user_id)
    request_hash = hash_profile_patch_request(draft_id, expected_revision, actions)
    history_entry = (
        draft.operation_history.get("operations", {}).get(idempotency_key)
        if idempotency_key
        else None
    )
    if isinstance(history_entry, dict):
        if str(history_entry.get("request_digest") or "") != request_hash:
            raise TaskError(
                code="TASK_IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key conflict",
                status_code=409,
            )
        response_payload = history_entry.get("response")
        if isinstance(response_payload, dict):
            return _draft_from_response_payload(response_payload, draft)
    if idempotency_key and draft.last_operation_idempotency_key == idempotency_key:
        if draft.last_operation_request_digest != request_hash:
            raise TaskError(
                code="TASK_IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key conflict",
                status_code=409,
            )
        return draft
    ensure_draft_editable(draft, "确认/修改")
    ensure_revision(draft.revision, expected_revision)
    known = {field.field_key for field in draft.fields}
    applied = 0
    for action in actions:
        if action.expected_revision != draft.revision:
            raise DraftVersionConflict()
        existing_field = next(
            (f for f in draft.fields if f.field_key == action.field_key), None
        )
        is_entry = existing_field is not None and existing_field.field_kind == "entry"
        # entry 的 field_key 由服务端生成（entry_{category}_{hex}），不在
        # structured allowlist；其可编辑性只取决于该键是否存在于当前草稿。
        if is_entry:
            pass
        elif action.field_key not in AI_FIELD_ALLOWLIST:
            raise AIInputError(f"field {action.field_key} 不在可编辑字段白名单内")
        elif action.field_key not in known:
            raise AIInputError(f"field {action.field_key} 不存在于当前草稿")
        if action.action is ProfileFieldPatchAction.CONFIRM:
            await _update_draft_field_status(
                db, draft_id, action.field_key, ProfileFieldConfirmationStatus.CONFIRMED
            )
            applied += 1
        elif action.action is ProfileFieldPatchAction.REPLACE:
            if is_entry:
                # entry 编辑 = 改 content 并重算 content_hash（WP-P1）；分类
                # 不可改——改分类语义等于删除+新增，走显式 delete/add 流程。
                new_content = validate_entry_content(action.value)
                new_hash = _content_hash(
                    action.field_key,
                    draft.subject,
                    new_content,
                    existing_field.source_turn_ids,
                )
                await db.execute(
                    text(
                        "UPDATE ai_profile_draft_field "
                        "SET content = :content, display_value = :display_value, "
                        "content_hash = :content_hash, "
                        "confirmation_status = 'confirmed', updated_at = UTC_TIMESTAMP() "
                        "WHERE draft_id = :draft_id AND field_key = :field_key"
                    ),
                    {
                        "content": new_content,
                        "display_value": new_content,
                        "content_hash": new_hash,
                        "draft_id": draft_id,
                        "field_key": action.field_key,
                    },
                )
                applied += 1
                continue
            normalized_value = _validate_and_normalize_replace_value(
                draft.subject, action.field_key, action.value
            )
            existing = next(f for f in draft.fields if f.field_key == action.field_key)
            new_hash = _content_hash(
                action.field_key, draft.subject, normalized_value, existing.source_turn_ids
            )
            await db.execute(
                text(
                    "UPDATE ai_profile_draft_field "
                    "SET value_json = :value_json, display_value = :display_value, "
                    "content_hash = :content_hash, "
                    "confirmation_status = 'confirmed', updated_at = UTC_TIMESTAMP() "
                    "WHERE draft_id = :draft_id AND field_key = :field_key"
                ),
                {
                    "value_json": json.dumps(normalized_value, ensure_ascii=False),
                    "display_value": _display_value(normalized_value),
                    "content_hash": new_hash,
                    "draft_id": draft_id,
                    "field_key": action.field_key,
                },
            )
            applied += 1
        elif action.action is ProfileFieldPatchAction.REJECT:
            await _update_draft_field_status(
                db, draft_id, action.field_key, ProfileFieldConfirmationStatus.REJECTED
            )
            applied += 1
        elif action.action is ProfileFieldPatchAction.DELETE:
            await _update_draft_field_status(
                db, draft_id, action.field_key, ProfileFieldConfirmationStatus.DELETED
            )
            applied += 1
        else:
            raise AIInputError(f"action {action.action} 非法")
    if applied:
        await db.execute(
            text(
                "UPDATE ai_profile_draft SET expected_revision = :revision, "
                "updated_at = UTC_TIMESTAMP() WHERE draft_id = :draft_id"
            ),
            {"revision": draft.revision + 1, "draft_id": draft_id},
        )
    updated = await load_owned_draft(db, draft_id, owner_user_id)
    if idempotency_key:
        history = dict(updated.operation_history or {"operations": {}})
        operations = dict(history.get("operations") or {})
        operations[idempotency_key] = {
            "request_digest": request_hash,
            "response": _draft_response_payload(updated),
        }
        if len(operations) > 64:
            operations = dict(list(operations.items())[-64:])
        history["operations"] = operations
        await db.execute(
            text(
                "UPDATE ai_profile_draft SET last_operation_idempotency_key = :key, "
                "last_operation_request_digest = :request_digest, "
                "last_operation_response_json = :response_json, "
                "updated_at = UTC_TIMESTAMP() WHERE draft_id = :draft_id"
            ),
            {
                "draft_id": draft_id,
                "key": idempotency_key,
                "request_digest": request_hash,
                "response_json": json.dumps(history, ensure_ascii=False),
            },
        )
    return updated


async def _update_draft_field_status(
    db: AsyncSession,
    draft_id: str,
    field_key: str,
    status: ProfileFieldConfirmationStatus,
) -> None:
    await db.execute(
        text(
            "UPDATE ai_profile_draft_field "
            "SET confirmation_status = :status, updated_at = UTC_TIMESTAMP() "
            "WHERE draft_id = :draft_id AND field_key = :field_key"
        ),
        {"status": status.value, "draft_id": draft_id, "field_key": field_key},
    )


async def insert_immutable_profile_revision(
    db: AsyncSession,
    owner_user_id: int,
    draft: ProfileDraft,
    fields: tuple[ProfileDraftField, ...],
    target: str,
) -> PublishedRevision:
    """Write ai_profile_revision + ai_profile_revision_field (confirmed fields only).

    只写 ``confirmed`` 字段；content_hash/source_revision 必填。发布后草稿标记为
    ``published`` 并关联 ``published_revision_id``，所属会话推进到 ``published``
    （发布后历史只读）。不 commit。
    """
    subject = draft.subject
    source_revision = await _load_revision_vector(db, owner_user_id)
    now = _now_utc()
    # revision_no 经 ``SELECT MAX(revision_no)+1`` 计算，两个并发发布事务可读到
    # 同一值。唯一键 ``uk_ai_profile_revision (user_id, subject, revision_no)``
    # 会拒绝第二个 INSERT；此处捕获 ``IntegrityError``，用 SAVEPOINT 只回滚本次
    # INSERT 后重读并重试（最多 3 次），与 ``enqueue_task`` 的嵌套事务并发处理
    # 模式一致。SAVEPOINT 保证调用方事务（草稿锁、reserved_task 等）不被误回滚。
    revision_id = 0
    revision_no = 1
    for attempt in range(3):
        result = await db.execute(
            text(
                "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_no "
                "FROM ai_profile_revision WHERE user_id = :user_id AND subject = :subject"
            ),
            {"user_id": owner_user_id, "subject": subject},
        )
        row = await _first_row(result)
        revision_no = int(row["next_no"]) if row else 1
        try:
            if hasattr(db, "begin_nested"):
                async with db.begin_nested():
                    await db.execute(
                        text(
                            "INSERT INTO ai_profile_revision "
                            "(user_id, subject, revision_no, draft_id, source_revision_json, "
                            " policy_revision, published_by, published_at, created_at) "
                            "VALUES (:user_id, :subject, :revision_no, :draft_id, "
                            " :source_revision_json, :policy_revision, :published_by, "
                            " :published_at, :created_at)"
                        ),
                        {
                            "user_id": owner_user_id,
                            "subject": subject,
                            "revision_no": revision_no,
                            "draft_id": draft.draft_id,
                            "source_revision_json": json.dumps(
                                source_revision.as_dict(), ensure_ascii=False
                            ),
                            "policy_revision": draft.policy_revision or PROFILE_POLICY_REVISION,
                            "published_by": owner_user_id,
                            "published_at": now,
                            "created_at": now,
                        },
                    )
            else:
                await db.execute(
                    text(
                        "INSERT INTO ai_profile_revision "
                        "(user_id, subject, revision_no, draft_id, source_revision_json, "
                        " policy_revision, published_by, published_at, created_at) "
                        "VALUES (:user_id, :subject, :revision_no, :draft_id, "
                        " :source_revision_json, :policy_revision, :published_by, "
                        " :published_at, :created_at)"
                    ),
                    {
                        "user_id": owner_user_id,
                        "subject": subject,
                        "revision_no": revision_no,
                        "draft_id": draft.draft_id,
                        "source_revision_json": json.dumps(
                            source_revision.as_dict(), ensure_ascii=False
                        ),
                        "policy_revision": draft.policy_revision or PROFILE_POLICY_REVISION,
                        "published_by": owner_user_id,
                        "published_at": now,
                        "created_at": now,
                    },
                )
        except IntegrityError:
            if attempt == 2:
                raise
            continue
        result = await db.execute(
            text(
                "SELECT id FROM ai_profile_revision "
                "WHERE user_id = :user_id AND subject = :subject AND revision_no = :revision_no "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"user_id": owner_user_id, "subject": subject, "revision_no": revision_no},
        )
        row = await _first_row(result)
        revision_id = int(row["id"]) if row else 0
        break
    changed_keys: list[str] = []
    for field in fields:
        if field.confirmation_status != ProfileFieldConfirmationStatus.CONFIRMED.value:
            raise AIInputError("only confirmed fields can be published")
        is_entry = field.field_kind == "entry"
        if is_entry:
            # entry 的 field_key 是服务端生成的 entry_{category}_{hex}，不在
            # structured allowlist；合法性由抽取/编辑边界的分类与 200 字校验
            # 承担，发布层只复核主体一致与正文存在（WP-P1）。
            if field.subject != subject or field.category not in PROFILE_ENTRY_CATEGORIES:
                raise AIInputError(
                    "published entry is outside the draft subject/category contract"
                )
            if not field.content:
                raise AIInputError("published entry is missing content")
        elif field.subject != subject or field.field_key not in AI_FIELD_ALLOWLIST:
            raise AIInputError("published field is outside the draft subject allowlist")
        changed_keys.append(field.field_key)
        await db.execute(
            text(
                "INSERT INTO ai_profile_revision_field "
                "(revision_id, field_key, subject, field_kind, category, content, "
                " replaces_field_key, value_json, display_value, confidence, "
                " source_type, source_turn_ids, source_span, content_hash, schema_version, prompt_version, "
                " created_at) "
                "VALUES (:revision_id, :field_key, :subject, :field_kind, :category, :content, "
                " :replaces_field_key, :value_json, :display_value, "
                " :confidence, :source_type, :source_turn_ids, :source_span, :content_hash, "
                " :schema_version, :prompt_version, :created_at)"
            ),
            {
                "revision_id": revision_id,
                "field_key": field.field_key,
                "subject": subject,
                "field_kind": field.field_kind,
                "category": field.category,
                "content": field.content,
                "replaces_field_key": field.replaces_field_key,
                # entry 的 value_json 恒为 NULL（正文在 content），structured
                # 行保持既有 JSON 序列化不变。
                "value_json": (
                    None if is_entry else json.dumps(field.value, ensure_ascii=False)
                ),
                "display_value": field.display_value,
                "confidence": field.confidence,
                "source_type": field.source_type or "user_answer",
                "source_turn_ids": json.dumps(list(field.source_turn_ids), ensure_ascii=False),
                "source_span": field.source_span,
                "content_hash": field.content_hash
                or _content_hash(field.field_key, subject, field.value, field.source_turn_ids),
                "schema_version": field.schema_version or PROFILE_SCHEMA_VERSION,
                "prompt_version": field.prompt_version,
                "created_at": now,
            },
        )
    await db.execute(
        text(
            "UPDATE ai_profile_draft SET status = 'published', "
            "published_revision_id = :revision_id, "
            "expected_revision = expected_revision + 1, updated_at = UTC_TIMESTAMP() "
            "WHERE draft_id = :draft_id"
        ),
        {"revision_id": revision_id, "draft_id": draft.draft_id},
    )
    if draft.session_id:
        await db.execute(
            text(
                "UPDATE ai_profile_session SET status = 'published', active_status = 0, "
                "ended_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() "
                "WHERE session_id = :session_id AND status IN "
                "('draft','extracting','awaiting_confirmation','paused')"
            ),
            {"session_id": draft.session_id},
        )
    return PublishedRevision(
        revision_id=revision_id,
        subject=subject,
        revision_no=revision_no,
        draft_id=draft.draft_id,
        changed_field_keys=tuple(changed_keys),
        published_at=now,
    )


async def enqueue_cleanup_or_projection_task(
    db: AsyncSession,
    owner_user_id: int,
    revision: PublishedRevision,
    idempotency_key: str,
    request_hash: str,
    source_revision: RevisionVector,
    consent_snapshot: dict[str, Any],
    reserved_task: AiTaskRecord | None = None,
) -> AiTaskRecord:
    """Enqueue the projection-build task that follows a confirmed publish.

    投影/搜索结果/兼容度快照的具体构建由 Task 9/10/11 的 handler 负责；本任务只
    记录受控摘要（revision/draft/subject/target），不含字段原文。不 commit。
    """
    target = "user_profile" if revision.subject == "personal" else "user_partner_preference"
    task = reserved_task or await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=_PROJECTION_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=source_revision,
        consent=consent_snapshot or None,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "source_revision_json = :source_revision_json, "
            "consent_snapshot_json = :consent_snapshot_json, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                {
                    "published_revision_id": revision.revision_id,
                    "revision_id": revision.revision_id,
                    "draft_id": revision.draft_id,
                    "subject": revision.subject,
                    "user_id": owner_user_id,
                    "projection_target": target,
                    "source_revision": source_revision.as_dict(),
                    "consent_snapshot": consent_snapshot,
                },
                ensure_ascii=False,
            ),
            "source_revision_json": json.dumps(
                source_revision.as_dict(), ensure_ascii=False
            ),
            "consent_snapshot_json": json.dumps(
                consent_snapshot, ensure_ascii=False
            ),
            "task_id": task.task_id,
        },
    )
    return task


async def publish_profile_draft(
    db: AsyncSession,
    draft_id: str,
    owner_user_id: int,
    expected_revision: int,
    idempotency_key: str,
) -> TaskSubmission:
    """Publish a draft: write only confirmed fields to an immutable revision.

    Idempotent by ``Idempotency-Key``: a same-key same-payload retry returns the
    first task without re-writing a revision or re-bumping the revision vector.
    Only the subject's own revision component is incremented — personal →
    ``profile_revision``, ideal_partner → ``preference_revision`` (never inferred
    from an undefined ``revision.kind``).  The projection task is enqueued in the
    same transaction, then ``db.flush()``.  Does not commit.
    """
    request_hash = hash_publish_request(draft_id, expected_revision)
    existing = await _find_write_task(
        db, owner_user_id, _PROJECTION_TASK_TYPE, idempotency_key
    )
    if existing is not None:
        narrative_existing = await _find_write_task(
            db, owner_user_id, _NARRATIVE_TASK_TYPE, idempotency_key + "-narrative"
        )
        return TaskSubmission.replay(
            _replay_or_conflict(existing, request_hash, "publish"),
            narrative_task_id=narrative_existing.task_id if narrative_existing else None,
        )
    draft = await load_owned_draft_for_update(db, draft_id, owner_user_id)
    request_hash = hash_publish_request(draft_id, expected_revision)
    # The draft row lock serializes concurrent publishes of the same draft.
    # Re-check the idempotency row after acquiring it: a retry that entered
    # before the winning transaction committed must replay, not fail on the
    # now-published draft or create a second immutable revision.
    existing = await _find_write_task(
        db, owner_user_id, _PROJECTION_TASK_TYPE, idempotency_key
    )
    if existing is not None:
        narrative_existing = await _find_write_task(
            db, owner_user_id, _NARRATIVE_TASK_TYPE, idempotency_key + "-narrative"
        )
        return TaskSubmission.replay(
            _replay_or_conflict(existing, request_hash, "publish"),
            narrative_task_id=narrative_existing.task_id if narrative_existing else None,
        )
    ensure_draft_editable(draft, "发布")
    ensure_revision(draft.revision, expected_revision)
    fields = confirmed_fields(draft)
    min_fields = min_confirmed_fields_to_publish()
    # WP-P1 锁定语义：发布门槛只数 structured 确认字段，entry 条目不计入
    # （条目是丰富度增强，不改变建构门槛边界；测试在消费处防回归）。
    structured_confirmed_count = sum(
        1 for field in fields if field.field_kind == "structured"
    )
    if structured_confirmed_count < min_fields:
        raise AIInputError(
            f"at least {min_fields} confirmed fields are required"
        )
    current_revision = await _load_revision_vector(db, owner_user_id)
    # Reserve the unique task key before mutating the immutable revision. The
    # unique owner/type/key constraint plus the draft lock makes the whole
    # publish idempotent even when two requests race before either response.
    reserved_task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=_PROJECTION_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=current_revision,
        consent=draft.consent_snapshot or None,
    )
    if reserved_task.request_digest != request_hash:
        raise TaskError(
            code="TASK_IDEMPOTENCY_CONFLICT",
            message="Idempotency-Key 已用于不同请求内容",
            status_code=409,
        )
    target = "user_profile" if draft.subject == "personal" else "user_partner_preference"
    revision = await insert_immutable_profile_revision(
        db, owner_user_id, draft, fields, target
    )
    revision_component = "profile" if draft.subject == "personal" else "preference"
    published_vector = await increment_revision_and_enqueue(
        db,
        owner_user_id,
        RevisionKind(revision_component),
        revision.changed_field_keys,
        "ai_profile_published",
        priority=40,
        payload_extra={
            "published_revision_id": revision.revision_id,
            "subject": draft.subject,
            "consent_snapshot": draft.consent_snapshot,
        },
    )
    # The immutable revision is not externally visible until this transaction
    # commits; record the post-publish five-dimensional snapshot before then.
    await db.execute(
        text(
            "UPDATE ai_profile_revision SET source_revision_json = :source_revision_json "
            "WHERE id = :revision_id"
        ),
        {
            "source_revision_json": json.dumps(
                published_vector.as_dict(), ensure_ascii=False
            ),
            "revision_id": revision.revision_id,
        },
    )
    task = await enqueue_cleanup_or_projection_task(
        db,
        owner_user_id,
        revision,
        idempotency_key,
        request_hash,
        source_revision=published_vector,
        consent_snapshot=draft.consent_snapshot,
        reserved_task=reserved_task,
    )
    # 入队画像叙事层（narrative）任务——与投影任务并列，发布后异步生成
    # 人格画像解读成品（persona_title/insight/dimensions/ideal_weights）。
    # 失败不影响发布本身，只改变 narrative 任务状态。任务创建复用
    # ``_enqueue_narrative_task``（regenerate 与 publish 共用同一逻辑）。
    narrative_task = await _enqueue_narrative_task(
        db,
        owner_user_id,
        str(revision.subject),
        idempotency_key,
        revision_id=revision.revision_id,
        source_revision=published_vector,
        consent_snapshot=draft.consent_snapshot,
    )
    return TaskSubmission.accepted(task, revision, narrative_task.task_id)


async def _enqueue_narrative_task(
    db: AsyncSession,
    owner_user_id: int,
    subject: str,
    idempotency_key: str,
    *,
    revision_id: int | None = None,
    source_revision: RevisionVector | None = None,
    consent_snapshot: dict[str, Any] | None = None,
) -> AiTaskRecord:
    """入队一条 ``profile_narrative`` 任务并回填受控摘要（唯一创建入口）。

    publish 与 regenerate 共用本助手，避免两份任务创建逻辑漂移：

    - publish 传入刚落库的 ``revision_id`` / 发布后版本向量 / 授权快照，
      行为与抽取前逐字节一致；
    - regenerate 缺省时按库内当前状态解析：该 subject 最近一次发布的
      revision、用户全量 revision 向量、最新有效 ``profile_text_extract``
      授权（无授权抛 ``AIConsentRequired``，无历史 revision 抛
      ``AIInputError``）。

    不 commit，由调用方控制事务。
    """
    resolved_revision_id = revision_id
    if resolved_revision_id is None:
        row = await _first_row(
            await db.execute(
                text(
                    "SELECT id FROM ai_profile_revision "
                    "WHERE user_id = :user_id AND subject = :subject "
                    "ORDER BY revision_no DESC, id DESC LIMIT 1"
                ),
                {"user_id": owner_user_id, "subject": subject},
            )
        )
        if row is None:
            raise AIInputError("尚未生成过画像叙事层，无法重新生成")
        resolved_revision_id = int(row["id"])
    resolved_vector = source_revision
    if resolved_vector is None:
        resolved_vector = await _load_revision_vector(db, owner_user_id)
    resolved_consent = consent_snapshot
    if resolved_consent is None:
        consent = await _load_latest_consent(db, owner_user_id, PROFILE_CONSENT_SCOPE)
        if consent is None:
            raise AIConsentRequired()
        resolved_consent = _consent_snapshot(consent)
    narrative_request_hash = hash_narrative_request(resolved_revision_id, subject)
    narrative_task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=_NARRATIVE_TASK_TYPE,
        idempotency_key=idempotency_key + "-narrative",
        request_hash=narrative_request_hash,
        revisions=resolved_vector,
        consent=resolved_consent,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "source_revision_json = :source_revision_json, "
            "consent_snapshot_json = :consent_snapshot_json, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                {
                    "published_revision_id": resolved_revision_id,
                    "subject": subject,
                    "user_id": owner_user_id,
                    "source_revision": resolved_vector.as_dict(),
                    "consent_snapshot": resolved_consent,
                },
                ensure_ascii=False,
            ),
            "source_revision_json": json.dumps(
                resolved_vector.as_dict(), ensure_ascii=False
            ),
            "consent_snapshot_json": json.dumps(
                resolved_consent, ensure_ascii=False
            ),
            "task_id": narrative_task.task_id,
        },
    )
    await db.flush()
    return narrative_task


async def restore_profile_revision(
    db: AsyncSession,
    revision_id: int,
    owner_user_id: int,
    idempotency_key: str = "",
) -> ProfileDraft:
    """Create a new editable draft from an immutable revision snapshot.

    旧 revision 只读、不更新旧行；新草稿字段回填 ``suggested``（再由用户确认后
    发布）。新草稿 ``expected_revision=0``，可正常走 confirm → publish 流程。

    consent 校验与 ``create_profile_session`` 一致：恢复前必须有有效
    ``profile_text_extract`` 授权（revoked_at IS NULL），否则抛
    ``AIConsentRequired``，不允许用已撤销授权的快照静默创建草稿。
    """
    result = await db.execute(
        text(
            "SELECT id, user_id, subject, revision_no, draft_id, policy_revision, "
            "published_at, created_at FROM ai_profile_revision "
            "WHERE id = :revision_id LIMIT 1"
        ),
        {"revision_id": revision_id},
    )
    row = await _first_row(result)
    if row is None or int(row["user_id"]) != owner_user_id:
        raise ProfileRevisionNotFound()
    subject = str(row["subject"])
    field_rows_result = await db.execute(
        text(
            "SELECT field_key, subject, value_json, display_value, confidence, "
            "source_type, source_turn_ids, source_span, content_hash, schema_version, prompt_version "
            "FROM ai_profile_revision_field WHERE revision_id = :revision_id"
        ),
        {"revision_id": revision_id},
    )
    field_rows = field_rows_result.mappings().all()
    now = _now_utc()
    consent = await _load_latest_consent(db, owner_user_id, PROFILE_CONSENT_SCOPE)
    if consent is None:
        # consent 已撤销或不存在时禁止恢复（与 ``create_profile_session`` 一致），
        # 不允许用已撤销授权的快照静默创建草稿（缺陷 14）。
        raise AIConsentRequired()
    consent_snapshot = _consent_snapshot(consent)
    # 幂等回放（缺陷 15）：同 ``Idempotency-Key`` 重复 restore 返回已创建的草稿，
    # 不创建第二个草稿。参考 ``confirm_profile_draft`` 的幂等处理模式：在
    # ``ai_task`` 表按 ``task_type='profile_restore'`` + key 查找既有终态任务，
    # 命中且 request_digest 一致时回放其关联草稿；digest 不一致则 409 冲突。
    request_digest = hash_restore_request(revision_id, owner_user_id)
    if idempotency_key:
        existing_task = await _find_write_task(
            db, owner_user_id, _RESTORE_TASK_TYPE, idempotency_key
        )
        if existing_task is not None:
            if existing_task.request_digest != request_digest:
                raise TaskError(
                    code="TASK_IDEMPOTENCY_CONFLICT",
                    message="Idempotency-Key 已用于不同请求内容",
                    status_code=409,
                )
            # 回放：从任务 payload_summary 取回已创建的 draft_id 并回读草稿。
            payload = existing_task.payload_summary or {}
            replayed_draft_id = payload.get("draft_id") if isinstance(payload, dict) else None
            if replayed_draft_id:
                try:
                    replayed = await load_owned_draft(
                        db, str(replayed_draft_id), owner_user_id
                    )
                    return replayed
                except ProfileDraftNotFound:
                    # 草稿已被删除等：回放失败，走重建路径。
                    pass
    draft_id = uuid.uuid4().hex
    await db.execute(
        text(
            "INSERT INTO ai_profile_draft "
            "(draft_id, user_id, subject, session_id, status, expected_revision, "
            " consent_snapshot_json, policy_revision, prompt_version, schema_version, "
            " last_operation_idempotency_key, last_operation_request_digest, "
            " expires_at, created_at, updated_at) "
            "VALUES (:draft_id, :user_id, :subject, NULL, 'draft', 0, "
            " :consent_snapshot_json, :policy_revision, :prompt_version, :schema_version, "
            " :idempotency_key, :request_digest, "
            " NULL, :created_at, :created_at)"
        ),
        {
            "draft_id": draft_id,
            "user_id": owner_user_id,
            "subject": subject,
            "consent_snapshot_json": json.dumps(consent_snapshot, ensure_ascii=False),
            "policy_revision": str(row["policy_revision"] or PROFILE_POLICY_REVISION),
            "prompt_version": PROFILE_PROMPT_VERSION,
            "schema_version": PROFILE_SCHEMA_VERSION,
            "idempotency_key": idempotency_key or None,
            "request_digest": request_digest if idempotency_key else None,
            "created_at": now,
        },
    )
    for field in field_rows:
        source_turn_ids = field.get("source_turn_ids")
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft_field "
                "(draft_id, field_key, subject, value_json, display_value, source_type, "
                " source_turn_ids, source_span, confidence, visibility, consent_scope, schema_version, "
                " prompt_version, content_hash, confirmation_status, created_at, updated_at) "
                "VALUES (:draft_id, :field_key, :subject, :value_json, :display_value, "
                " :source_type, :source_turn_ids, :source_span, :confidence, 'self', :consent_scope, "
                " :schema_version, :prompt_version, :content_hash, 'suggested', "
                " :created_at, :created_at)"
            ),
            {
                "draft_id": draft_id,
                "field_key": str(field["field_key"]),
                "subject": subject,
                "value_json": json.dumps(
                    _maybe_json(field.get("value_json")), ensure_ascii=False
                ),
                "display_value": field.get("display_value"),
                "source_type": str(field.get("source_type") or "user_answer"),
                "source_turn_ids": json.dumps(
                    list(json.loads(source_turn_ids)) if source_turn_ids else [],
                    ensure_ascii=False,
                ),
                "source_span": field.get("source_span"),
                "confidence": float(field.get("confidence") or 0.0),
                "consent_scope": consent_snapshot.get("scope"),
                "schema_version": str(
                    field.get("schema_version") or PROFILE_SCHEMA_VERSION
                ),
                "prompt_version": field.get("prompt_version"),
                "content_hash": str(field.get("content_hash") or ""),
                "created_at": now,
            },
        )
    draft_row = {
        "draft_id": draft_id,
        "user_id": owner_user_id,
        "subject": subject,
        "session_id": None,
        "status": "draft",
        "expected_revision": 0,
        "policy_revision": str(row["policy_revision"] or PROFILE_POLICY_REVISION),
        "schema_version": PROFILE_SCHEMA_VERSION,
        "expires_at": None,
        "created_at": now,
        "updated_at": now,
    }
    draft = _draft_from_row(draft_row, list(field_rows))
    # 记录幂等锚（缺陷 15）：restore 是同步操作，但需要一个 ``profile_restore``
    # 任务行承载 idempotency_key + request_digest + draft_id，供重复请求回放。
    # 不入队执行（无 handler）；enqueue 后立即标记 succeeded 终态，worker 不会
    # 拾取（profile_restore 无注册 handler）。
    if idempotency_key:
        current_revision = await _load_revision_vector(db, owner_user_id)
        try:
            restore_task = await enqueue_task(
                db=db,
                owner_user_id=owner_user_id,
                task_type=_RESTORE_TASK_TYPE,
                idempotency_key=idempotency_key,
                request_hash=request_digest,
                revisions=current_revision,
                consent=consent_snapshot or None,
            )
            # 同步操作直接置终态（绕过 queued->leased->running->succeeded 状态机：
            # 此任务无 handler、worker 不会拾取，终态仅为幂等去重锚）。
            if restore_task.status != AiTaskStatus.SUCCEEDED:
                await db.execute(
                    text(
                        "UPDATE ai_task SET status = 'succeeded', "
                        "result_ref = :result_ref, finished_at = UTC_TIMESTAMP(), "
                        "payload_summary = :payload_summary, "
                        "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
                    ),
                    {
                        "result_ref": f"profile-draft:{draft_id}",
                        "payload_summary": json.dumps(
                            {
                                "revision_id": revision_id,
                                "draft_id": draft_id,
                                "subject": subject,
                            },
                            ensure_ascii=False,
                        ),
                        "task_id": restore_task.task_id,
                    },
                )
            else:
                # 并发同 key 回放：enqueue_task 已返回既有终态任务，补记 payload。
                await db.execute(
                    text(
                        "UPDATE ai_task SET payload_summary = :payload_summary, "
                        "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
                    ),
                    {
                        "payload_summary": json.dumps(
                            {
                                "revision_id": revision_id,
                                "draft_id": draft_id,
                                "subject": subject,
                            },
                            ensure_ascii=False,
                        ),
                        "task_id": restore_task.task_id,
                    },
                )
        except IntegrityError:
            # 并发同 key：另一请求已抢先创建 restore 草稿。回读回放。
            await db.rollback()
            existing_task = await _find_write_task(
                db, owner_user_id, _RESTORE_TASK_TYPE, idempotency_key
            )
            if existing_task is not None:
                payload = existing_task.payload_summary or {}
                replayed_draft_id = (
                    payload.get("draft_id") if isinstance(payload, dict) else None
                )
                if replayed_draft_id:
                    try:
                        return await load_owned_draft(
                            db, str(replayed_draft_id), owner_user_id
                        )
                    except ProfileDraftNotFound:
                        pass
            raise
    return draft


async def _load_latest_consent(
    db: AsyncSession, user_id: int, scope: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            "SELECT user_id, scope, version, policy_revision, granted_at "
            "FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL "
            "ORDER BY granted_at DESC LIMIT 1"
        ),
        {"user_id": user_id, "scope": scope},
    )
    return await _first_row(result)


async def delete_ai_profile(
    db: AsyncSession,
    owner_user_id: int,
    subject: ProfileSubject,
    idempotency_key: str,
) -> CleanupTask:
    """Delete the whole AI profile / revoke consent for one subject.

    同一事务内：先同步写不可读标记（草稿 → deleted、活动会话 → cancelled、已发布
    投影引用 → invalidated、search result → stale、compatibility snapshot →
    blocked、撤回 ``profile_text_extract`` 授权），再递增 ``privacy_revision``
    并写 outbox 删除事件（personal → ``ai_profile_deleted``，ideal_partner →
    ``ai_preference_deleted``），最后 enqueue cleanup task（status=``queued``）。
    同步响应前草稿与派生结果已不可读；物理清理由 Task 9/10/11 消费者执行。
    重复删除（同 key）回放同一 cleanup task。不 commit。
    """
    subject_value = _subject_value(subject)
    request_hash = hash_profile_delete(subject_value, None)
    existing = await _find_write_task(
        db, owner_user_id, _CLEANUP_TASK_TYPE, idempotency_key
    )
    if existing is not None:
        return CleanupTask(
            task_id=_replay_or_conflict(existing, request_hash, "delete").task_id,
            status=existing.status.value,
            subject=subject_value,
        )
    await db.execute(
        text(
            "UPDATE ai_profile_draft SET status = 'deleted', "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE user_id = :user_id AND subject = :subject AND status <> 'deleted'"
        ),
        {"user_id": owner_user_id, "subject": subject_value},
    )
    await db.execute(
        text(
            "UPDATE ai_profile_session SET status = 'cancelled', active_status = 0, "
            "ended_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() "
            "WHERE user_id = :user_id AND subject = :subject AND active_status = 1"
        ),
        {"user_id": owner_user_id, "subject": subject_value},
    )
    projection_kinds = (
        _PERSONAL_PROJECTION_KINDS
        if subject_value == ProfileSubject.PERSONAL.value
        else _IDEAL_PARTNER_PROJECTION_KINDS
    )
    placeholders = ", ".join(f":k{i}" for i in range(len(projection_kinds)))
    await db.execute(
        text(
            "UPDATE ai_feature_projection SET status = 'invalidated', "
            "invalidated_at = UTC_TIMESTAMP(), "
            "purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY), "
            f"updated_at = UTC_TIMESTAMP() "
            f"WHERE subject_user_id = :user_id AND projection_kind IN ({placeholders}) "
            "AND status = 'active'"
        ),
        {
            "user_id": owner_user_id,
            **{f"k{i}": kind for i, kind in enumerate(projection_kinds)},
        },
    )
    await db.execute(
        text(
            # M-4 carry-over：不仅失效 target 方向结果，还要失效该用户作为
            # viewer（snapshot 发起人）的结果行，通过 JOIN ai_search_snapshot
            # 匹配 viewer 方向。参考 consents.py 的同款 JOIN 模式。
            "UPDATE ai_search_result r "
            "JOIN ai_search_snapshot s ON s.snapshot_id = r.snapshot_id "
            "SET r.stale = 1, r.updated_at = UTC_TIMESTAMP() "
            "WHERE r.target_user_id = :user_id OR s.user_id = :user_id"
        ),
        {"user_id": owner_user_id},
    )
    # 代际 fence 标记（Plan Task 5 Step 4）：把该用户的搜索快照/草稿标记为
    # 旧代资源，purge 只删除带标记的行；删除后重建的新草稿/快照不受影响。
    await db.execute(
        text(
            "UPDATE ai_search_snapshot SET status = 'invalidated', "
            "invalidated_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() "
            "WHERE user_id = :user_id AND invalidated_at IS NULL"
        ),
        {"user_id": owner_user_id},
    )
    await db.execute(
        text(
            "UPDATE ai_search_draft SET status = 'invalidated', "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE user_id = :user_id "
            "AND status NOT IN ('invalidated', 'expired', 'failed')"
        ),
        {"user_id": owner_user_id},
    )
    await db.execute(
        text(
            "UPDATE ai_compatibility_snapshot SET status = 'blocked', "
            "invalidated_at = UTC_TIMESTAMP(), "
            "purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY) "
            "WHERE (viewer_user_id = :user_id OR target_user_id = :user_id) "
            "AND status <> 'blocked'"
        ),
        {"user_id": owner_user_id},
    )
    # 撤销 ``profile_text_extract`` 授权前先 ``SELECT ... FOR UPDATE`` 锁行，
    # 序列化并发撤销路径（与 ``revoke_consent`` 的墓碑计算逻辑一致）：两个并发
    # ``delete_ai_profile`` 只能有一个把 grant 标记 revoked，另一个在锁释放后
    # 看到 revoked_at 已非空，UPDATE 影响 0 行，幂等。墓碑计算依赖 granted_at，
    # 锁行保证墓碑值稳定。
    await db.execute(
        text(
            "SELECT user_id FROM ai_consent_grant "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL "
            "FOR UPDATE"
        ),
        {"user_id": owner_user_id, "scope": PROFILE_CONSENT_SCOPE},
    )
    await db.execute(
        text(
            "UPDATE ai_consent_grant SET revoked_at = UTC_TIMESTAMP(), "
            "revoke_reason = 'ai_profile_deleted', "
            "user_id = NULL, "
            "user_tombstone = SHA2(CONCAT('consent:', :user_id, ':', :scope, ':', granted_at), 256), "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL"
        ),
        {"user_id": owner_user_id, "scope": PROFILE_CONSENT_SCOPE},
    )
    event_type = (
        "ai_profile_deleted"
        if subject_value == ProfileSubject.PERSONAL.value
        else "ai_preference_deleted"
    )
    await increment_revision_and_enqueue(
        db,
        owner_user_id,
        RevisionKind.PRIVACY,
        (event_type,),
        event_type,
        priority=10,
        payload_extra={"subject": subject_value},
    )
    current_revision = await _load_revision_vector(db, owner_user_id)
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=_CLEANUP_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=current_revision,
        consent=None,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                _cleanup_payload(
                    scope="profile",
                    resource_id=f"profile:{owner_user_id}:{subject_value}",
                    version=current_revision,
                ),
                ensure_ascii=False,
            ),
            "task_id": task.task_id,
        },
    )
    await db.flush()
    return CleanupTask(
        task_id=task.task_id, status=task.status.value, subject=subject_value
    )


async def delete_ai_profile_field(
    db: AsyncSession,
    owner_user_id: int,
    subject: ProfileSubject,
    field_key: str,
    idempotency_key: str,
) -> CleanupTask:
    """Field-level deletion: hide synchronously, then clean up asynchronously.

    同一事务内：把该字段在本主体所有草稿中标记 ``deleted``（不可见），递增对应
    主体 revision（personal → profile、ideal_partner → preference）并写 outbox
    事件，最后 enqueue cleanup task。重复删除（同 key）回放同一 task。不 commit。

    注意（缺陷 16）：字段级删除是派生投影的失效信号，而非版本回写。已发布的
    ``ai_profile_revision`` 本身不可变——本函数不修改任何 revision 行。字段被标
    ``deleted`` 后，投影重建（``profile_projection`` handler）会读取最新已发布
    版本的 confirmed 字段重新生成投影，被删除的字段在下次重建时自然缺席。
    """
    subject_value = _subject_value(subject)
    if field_key not in AI_FIELD_ALLOWLIST:
        raise AIInputError(f"field {field_key} 不在可编辑字段白名单内")
    request_hash = hash_profile_delete(subject_value, field_key)
    existing = await _find_write_task(
        db, owner_user_id, _CLEANUP_TASK_TYPE, idempotency_key
    )
    if existing is not None:
        return CleanupTask(
            task_id=_replay_or_conflict(existing, request_hash, "delete").task_id,
            status=existing.status.value,
            subject=subject_value,
        )
    await db.execute(
        text(
            "UPDATE ai_profile_draft_field SET confirmation_status = 'deleted', "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE draft_id IN (SELECT draft_id FROM ai_profile_draft "
            " WHERE user_id = :user_id AND subject = :subject) "
            "AND field_key = :field_key AND confirmation_status <> 'deleted'"
        ),
        {"user_id": owner_user_id, "subject": subject_value, "field_key": field_key},
    )
    kind = (
        RevisionKind.PROFILE
        if subject_value == ProfileSubject.PERSONAL.value
        else RevisionKind.PREFERENCE
    )
    await increment_revision_and_enqueue(
        db,
        owner_user_id,
        kind,
        (field_key,),
        "ai_profile_field_deleted",
        priority=40,
        payload_extra={
            "subject": subject_value,
            "field_key": field_key,
        },
    )
    current_revision = await _load_revision_vector(db, owner_user_id)
    projection_kinds = (
        _PERSONAL_PROJECTION_KINDS
        if subject_value == ProfileSubject.PERSONAL.value
        else _IDEAL_PARTNER_PROJECTION_KINDS
    )
    placeholders = ", ".join(f":field_kind{i}" for i in range(len(projection_kinds)))
    await db.execute(
        text(
            "UPDATE ai_feature_projection SET status = 'invalidated', "
            "invalidated_at = UTC_TIMESTAMP(), "
            "invalidated_reason = 'ai_profile_field_deleted', "
            "purge_after = DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY), "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE subject_user_id = :user_id "
            f"AND projection_kind IN ({placeholders}) AND status = 'active'"
        ),
        {
            "user_id": owner_user_id,
            **{f"field_kind{i}": kind for i, kind in enumerate(projection_kinds)},
        },
    )
    await db.execute(
        text(
            # M-4 carry-over：不仅失效 target 方向结果，还要失效该用户作为
            # viewer（snapshot 发起人）的结果行，通过 JOIN ai_search_snapshot
            # 匹配 viewer 方向。参考 consents.py 的同款 JOIN 模式。
            "UPDATE ai_search_result r "
            "JOIN ai_search_snapshot s ON s.snapshot_id = r.snapshot_id "
            "SET r.stale = 1, r.updated_at = UTC_TIMESTAMP() "
            "WHERE r.target_user_id = :user_id OR s.user_id = :user_id"
        ),
        {"user_id": owner_user_id},
    )
    await db.execute(
        text(
            "UPDATE ai_compatibility_snapshot SET status = 'stale', "
            "invalidated_at = UTC_TIMESTAMP() "
            "WHERE (viewer_user_id = :user_id OR target_user_id = :user_id) "
            "AND status NOT IN ('stale', 'blocked')"
        ),
        {"user_id": owner_user_id},
    )
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=_CLEANUP_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=current_revision,
        consent=None,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                _cleanup_payload(
                    scope="field",
                    resource_id=f"field:{owner_user_id}:{subject_value}:{field_key}",
                    version=current_revision,
                ),
                ensure_ascii=False,
            ),
            "task_id": task.task_id,
        },
    )
    await db.flush()
    return CleanupTask(
        task_id=task.task_id, status=task.status.value, subject=subject_value
    )


def _subject_value(subject: Any) -> str:
    if isinstance(subject, ProfileSubject):
        return subject.value
    return str(subject)


# ----------------------------------------------------------------------
# Worker handler 注册（final review C-2/C-3 交接收尾）
# ----------------------------------------------------------------------


async def profile_projection_handler(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """``profile_projection`` Worker handler：发布后重建特征投影。

    读取已发布 ``ai_profile_revision`` 的已确认字段（Task 8 构造保证只有
    confirmed 字段），每次发布重建全部三种投影：``personal`` 主体对应
    ``personal_searchable`` + ``personal_compatibility``，``ideal_partner``
    主体对应 ``ideal_partner_preference``。任何一次发布都会推进五维版本向量，
    若只重建本次发布主体的投影，其他主体的投影会停留在旧向量上，按 §5.5
    全维比对被判过期，搜索随之静默为空，因此三种都要重建。每种调用
    ``build_feature_projection``（revision_vector=None 使投影以任务执行时的
    最新五维版本向量为准，保证投影 valid）：本次发布主体的 kind 钉住
    ``published_revision_id``，其他主体的 kind 取该主体最新已发布 revision，
    尚无已发布 revision 的主体跳过。``ProjectionBuildError``（无 allowlist
    字段/无授权等）按 Task 6 语义不可重试地失败为 ``RESULT_STALE``，绝不落
    空投影。返回 ``(result_ref, revisions)``，revisions 取任务入队时的
    source_revision 使 ``complete_task`` 版本复核不误 supersede。
    """
    payload = task.payload_summary or {}
    user_id = payload.get("user_id")
    subject = payload.get("subject")
    published_revision_id = payload.get("published_revision_id")
    source_revision = task.source_revision_json or payload.get("source_revision") or {}
    consent_snapshot = task.consent_snapshot_json or payload.get("consent_snapshot") or {}
    required_revision_keys = {
        "profile", "preference", "privacy", "relationship", "policy"
    }
    if (
        not user_id
        or not subject
        or not published_revision_id
        or not source_revision
        or set(source_revision) != required_revision_keys
        or not consent_snapshot
    ):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    subject_value = str(subject)
    if subject_value not in {
        ProfileSubject.PERSONAL.value,
        ProfileSubject.IDEAL_PARTNER.value,
    }:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    user_id_int = int(user_id)
    built: list[str] = []
    try:
        for kind in (
            ProjectionKind.PERSONAL_SEARCHABLE,
            ProjectionKind.PERSONAL_COMPATIBILITY,
            ProjectionKind.IDEAL_PARTNER_PREFERENCE,
        ):
            pinned_revision_id: int | None = None
            if _subject_for_kind(kind) == subject_value:
                pinned_revision_id = int(published_revision_id)
            elif await _load_latest_revision(
                db, user_id_int, _subject_for_kind(kind)
            ) is None:
                # 该主体尚无已发布 revision；本次发布不负责从空主体建投影。
                continue
            projection = await build_feature_projection(
                db,
                user_id_int,
                kind,
                revision_vector=None,
                published_revision_id=pinned_revision_id,
                consent_snapshot=consent_snapshot,
            )
            built.append(
                f"{kind.value}:{projection.id if projection.id is not None else 'ok'}"
            )
    except ProjectionBuildError:
        # 投影不可构建（无该主体已确认字段/授权撤回等）：不可重试终态。
        await fail_task(
            db, task.task_id, worker_id,
            error_code="RESULT_STALE", retryable=False,
        )
        return None
    revisions = (
        RevisionVector(**task.source_revision_json)
        if task.source_revision_json
        else RevisionVector()
    )
    return f"profile-projection:{subject_value}:{','.join(built)}", revisions


async def _load_revision_fields(
    db: AsyncSession, revision_id: int
) -> list[dict[str, Any]]:
    """读取一个已发布 revision 的全部字段行（field_key + display_value）。"""
    result = await db.execute(
        text(
            "SELECT field_key, value_json, display_value "
            "FROM ai_profile_revision_field WHERE revision_id = :revision_id"
        ),
        {"revision_id": revision_id},
    )
    rows = result.mappings().all()
    return [dict(r) for r in rows] if rows else []


async def _load_previous_revision_id(
    db: AsyncSession, user_id: int, subject: str, current_revision_id: int
) -> int | None:
    """读取同 user+subject 的上一次发布 revision id。

    用 ``revision_no < 当前版本号`` 而非 ``id <`` 定位：revision_no 是业务
    语义上的版本序号，id 只保证插入顺序，二者不必然一致（并发重试时
    revision_no 回滚但 id 可能更大）。用子查询合并成一次 round trip：
    先取当前行的 revision_no，再找比它小的最大 revision_no 对应行。
    """
    result = await db.execute(
        text(
            "SELECT p.id FROM ai_profile_revision p "
            "WHERE p.user_id = :user_id AND p.subject = :subject "
            "AND p.revision_no < ("
            "  SELECT c.revision_no FROM ai_profile_revision c "
            "  WHERE c.id = :current_revision_id "
            "  AND c.user_id = :user_id AND c.subject = :subject"
            ") "
            "ORDER BY p.revision_no DESC LIMIT 1"
        ),
        {
            "user_id": user_id,
            "subject": subject,
            "current_revision_id": current_revision_id,
        },
    )
    row = result.mappings().first()
    return int(row["id"]) if row else None


async def _load_history_summaries(
    db: AsyncSession,
    user_id: int,
    subject: str,
    current_revision_id: int,
    limit: int = 3,
) -> tuple[dict[str, Any], ...]:
    """读取近 N 次历史 revision 的字段摘要（用于 history_observations）。

    排除当前 revision（``id != current_revision_id``）：history_observations
    描述的是"过去的你"，当前版本的解读在 dimensions/insight 里，不应重复出现。

    用子查询先取近 N 个 revision id，再 JOIN 字段表一次性取回所有字段，
    避免对每个 revision 逐一查询（N+1 → 1 次 round trip）。
    """
    result = await db.execute(
        text(
            "SELECT r.id AS rev_id, r.revision_no AS rev_no, "
            "f.field_key, f.value_json, f.display_value "
            "FROM ("
            "  SELECT id, revision_no FROM ai_profile_revision "
            "  WHERE user_id = :user_id AND subject = :subject "
            "  AND id != :current_revision_id "
            "  ORDER BY revision_no DESC LIMIT :limit"
            ") r "
            "JOIN ai_profile_revision_field f ON f.revision_id = r.id "
            "ORDER BY r.revision_no DESC, f.field_key"
        ),
        {
            "user_id": user_id,
            "subject": subject,
            "current_revision_id": current_revision_id,
            "limit": limit,
        },
    )
    rows = result.mappings().all()
    if not rows:
        return ()
    grouped: dict[int, dict[str, Any]] = {}
    for row in rows:
        rev_id = int(row["rev_id"])
        if rev_id not in grouped:
            grouped[rev_id] = {
                "revision_id": rev_id,
                "revision_no": int(row["rev_no"]),
                "fields": [],
            }
        grouped[rev_id]["fields"].append(
            {
                "field_key": row["field_key"],
                "value_json": row["value_json"],
                "display_value": row["display_value"],
            }
        )
    return tuple(grouped.values())


async def generate_profile_narrative_handler(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """``profile_narrative`` Worker handler：发布后生成画像叙事层成品。

    读取本次发布的已确认字段 + 上一次发布快照 + 历史摘要，调
    ``AIGateway.generate_narrative`` 生成人格画像解读（persona_title /
    insight / dimensions / ideal_weights / recent_change），写入
    ``ai_profile_summary.summary_text``。叙事层是展示性内容，不参与
    匹配/搜索/兼容度计算。失败只改任务状态，不影响发布本身。
    """
    payload = task.payload_summary or {}
    user_id = payload.get("user_id")
    subject = payload.get("subject")
    published_revision_id = payload.get("published_revision_id")
    source_revision = task.source_revision_json or payload.get("source_revision") or {}
    consent_snapshot = task.consent_snapshot_json or payload.get("consent_snapshot") or {}
    if (
        not user_id
        or not subject
        or not published_revision_id
        or not source_revision
        or not consent_snapshot
    ):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    subject_value = str(subject)
    if subject_value not in {
        ProfileSubject.PERSONAL.value,
        ProfileSubject.IDEAL_PARTNER.value,
    }:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    user_id_int = int(user_id)
    revision_id_int = int(published_revision_id)

    # 1. 读取本次发布的字段
    await db.execute(
        text(
            "UPDATE ai_task SET stage = 'reading_fields', updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id"
        ),
        {"task_id": task.task_id},
    )
    current_field_rows = await _load_revision_fields(db, revision_id_int)
    if not current_field_rows:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    current_fields = serialize_fields_for_prompt(current_field_rows)

    # 2. 读取上一次发布的字段（做 diff，生成 recent_change）
    previous_revision_id = await _load_previous_revision_id(
        db, user_id_int, subject_value, revision_id_int
    )
    previous_fields: tuple[dict[str, Any], ...] = ()
    if previous_revision_id is not None:
        prev_rows = await _load_revision_fields(db, previous_revision_id)
        previous_fields = serialize_fields_for_prompt(prev_rows)

    # 3. 读取历史摘要（用于 history_observations，排除当前 revision）
    history_summaries = await _load_history_summaries(
        db, user_id_int, subject_value, revision_id_int, limit=3
    )

    # 4. 调 Gateway 生成叙事层
    await db.execute(
        text(
            "UPDATE ai_task SET stage = 'calling_llm', updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id"
        ),
        {"task_id": task.task_id},
    )
    context = AITaskContext(
        task_id=task.task_id,
        request_id=uuid.uuid4().hex,
        scene="profile_narrative",
        provider=settings.ai_provider_name,
        model=settings.ai_model_name,
        prompt_version=NARRATIVE_PROMPT_VERSION,
        schema_version=NARRATIVE_SCHEMA_VERSION,
        input_revision=source_revision,
        policy_revision=consent_snapshot.get("policy_revision"),
    )
    request = NarrativeRequest(
        subject=subject_value,
        current_fields=current_fields,
        previous_fields=previous_fields,
        history_summaries=history_summaries,
        consent_version=str(consent_snapshot.get("consent_version") or ""),
        policy_revision=str(consent_snapshot.get("policy_revision") or PROFILE_POLICY_REVISION),
    )
    gateway = AIGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
    outcome = await gateway.generate_narrative(context, request)
    if outcome.result is None:
        await fail_task(
            db, task.task_id, worker_id,
            error_code=outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
            retryable=outcome.retryable,
        )
        return None

    result = outcome.result

    # 5. Worker 边界复核（与 extract_profile_turn 一致的三道校验末道）
    try:
        if result.schema_version != NARRATIVE_SCHEMA_VERSION:
            raise ValueError("narrative schema version does not match")
        if result.prompt_version != NARRATIVE_PROMPT_VERSION:
            raise ValueError("narrative prompt version does not match")
        if not result.persona_title or not result.insight:
            raise ValueError("narrative missing required persona_title or insight")
        if len(result.persona_tags) > 8:
            raise ValueError("too many persona_tags")
        if not result.dimensions:
            raise ValueError("narrative must have at least one dimension")
        # subject 一致性：personal 画像不应返回 ideal_weights
        if subject_value == ProfileSubject.PERSONAL.value and result.ideal_weights:
            raise ValueError("personal narrative must not have ideal_weights")
    except (AttributeError, TypeError, ValueError):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    # 6. 写入 ai_profile_summary
    await db.execute(
        text(
            "UPDATE ai_task SET stage = 'writing_summary', updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id"
        ),
        {"task_id": task.task_id},
    )
    summary_json = result.model_dump_json(ensure_ascii=False)
    content_hash = hashlib.sha256(summary_json.encode("utf-8")).hexdigest()
    # 叙事层生成后先进入待确认态，用户确认（POST narrative/confirm）后才为
    # confirmed；读取端透传状态由前端驱动确认 UI（良配对齐 WP-P3）。
    await db.execute(
        text(
            "INSERT INTO ai_profile_summary "
            "(session_id, draft_id, revision_id, user_id, subject, "
            " summary_text, status, content_hash, created_at, updated_at) "
            "VALUES (NULL, NULL, :revision_id, :user_id, :subject, "
            " :summary_text, 'pending_confirmation', :content_hash, "
            " UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ),
        {
            "revision_id": revision_id_int,
            "user_id": user_id_int,
            "subject": subject_value,
            "summary_text": summary_json,
            "content_hash": content_hash,
        },
    )

    revisions = (
        RevisionVector(**source_revision)
        if source_revision
        else RevisionVector()
    )
    return f"profile-narrative:{subject_value}:{revision_id_int}", revisions


async def load_published_narrative(
    db: AsyncSession, user_id: int, subject: str
) -> dict[str, Any] | None:
    """读取用户最新发布的画像叙事层成品。

    返回 ``{status, data}`` 或 ``None``（未发布或任务未完成）。
    路由层据此构造 ``ProfileNarrativeRead``。解析 ``summary_text`` JSON
    时如果格式异常，返回 ``status='pending'`` 而非抛异常——叙事层是展示性
    内容，脏数据不应导致 500。

    状态机（良配对齐 WP-P3）：新行先生成 ``pending_confirmation``，用户
    确认后转 ``confirmed``；``published`` 仅为历史行兼容值，过滤逻辑不变
    （始终取最新一条，状态透传由前端驱动确认 UI）。
    """
    result = await db.execute(
        text(
            "SELECT summary_text, status "
            "FROM ai_profile_summary "
            "WHERE user_id = :user_id AND subject = :subject "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"user_id": user_id, "subject": subject},
    )
    row = result.mappings().first()
    if row is None or not row.get("summary_text"):
        return None
    raw_text = str(row["summary_text"])
    try:
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            return {"status": "pending", "data": None}
    except (ValueError, TypeError):
        return {"status": "pending", "data": None}
    return {"status": str(row.get("status") or "published"), "data": data}


async def confirm_profile_narrative(
    db: AsyncSession, user_id: int, subject: str
) -> bool:
    """将用户某 subject 最新一条叙事层标记为 confirmed（方案 WP-P3）。

    只命中最新一行（MySQL 不允许 UPDATE 直接子查询同表，用派生表包装）。
    不 commit，由调用方控制事务。无行时返回 False（路由层 404）。
    """
    result = await db.execute(
        text(
            "UPDATE ai_profile_summary SET status = 'confirmed', "
            " updated_at = UTC_TIMESTAMP() "
            "WHERE id = (SELECT id FROM ("
            "  SELECT id FROM ai_profile_summary "
            "  WHERE user_id = :user_id AND subject = :subject "
            "  ORDER BY created_at DESC LIMIT 1"
            ") AS latest)"
        ),
        {"user_id": user_id, "subject": subject},
    )
    return bool(result.rowcount)


async def request_narrative_regenerate(
    db: AsyncSession, user_id: int, subject: str, idempotency_key: str
) -> AiTaskRecord:
    """重新生成画像叙事层（方案 WP-P3）：幂等回放先行，再限频，后复用任务创建入口。

    幂等：同 Idempotency-Key 的重试回放已入队任务而非重新计数/入队——含
    满限后的重试（终审 Important：限频不得挡住回放）；digest 不一致 409。
    限频：24h 窗口（UTC_TIMESTAMP，与库内 created_at 写入口径一致）内
    ``profile_narrative`` 任务满 5 条拒绝（``AIInputError``）。放行前提是
    该 subject 至少发布过一次 revision；该前置查询的结果经 ``revision_id``
    传入 ``_enqueue_narrative_task`` 复用，避免重复查询同一最新 revision。
    不 commit，由调用方控制事务。
    """
    revision_result = await db.execute(
        text(
            "SELECT id FROM ai_profile_revision "
            "WHERE user_id = :user_id AND subject = :subject "
            "ORDER BY revision_no DESC, id DESC LIMIT 1"
        ),
        {"user_id": user_id, "subject": subject},
    )
    revision_row = await _first_row(revision_result)
    if revision_row is None:
        raise AIInputError("尚未生成过画像叙事层，无法重新生成")
    revision_id = int(revision_row["id"])
    request_hash = hash_narrative_request(revision_id, subject)
    existing = await _find_write_task(
        db, user_id, _NARRATIVE_TASK_TYPE, idempotency_key + "-narrative"
    )
    if existing is not None:
        return _replay_or_conflict(existing, request_hash, "regenerate")
    count_result = await db.execute(
        text(
            "SELECT COUNT(*) AS n FROM ai_task "
            "WHERE owner_user_id = :user_id AND task_type = :task_type "
            "AND created_at > UTC_TIMESTAMP() - INTERVAL 24 HOUR"
        ),
        {"user_id": user_id, "task_type": _NARRATIVE_TASK_TYPE},
    )
    count_row = await _first_row(count_result)
    if (
        count_row is not None
        and int(count_row.get("n") or 0) >= _NARRATIVE_REGENERATE_DAILY_LIMIT
    ):
        raise AIInputError("今日叙事重新生成次数已达上限")
    return await _enqueue_narrative_task(
        db, user_id, subject, idempotency_key, revision_id=revision_id
    )


async def cleanup_handler(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """``cleanup`` Worker handler：删除/撤回的异步物理清理。

    同步「不可读先行」半边已由删除事务完成（草稿/session/投影/search result/
    compat 快照标不可见）；本 handler 负责异步派生传播：全量/字段删除把该用户
    全部 active 投影按事件版本向量标 invalidated，并尽量把派生结果表
    （ai_search_result / ai_compatibility_snapshot）标 stale（表存在时）。
    ``search`` scope（快照删除）只把该快照的结果行标 stale。失败按 Task 6 语义
    转可重试失败；完成后返回 ``(result_ref, revisions)`` 由 ``complete_task``
    版本复核。
    """
    payload = task.payload_summary or {}
    scope = str(payload.get("scope") or "")
    resource = _parse_cleanup_resource_id(str(payload.get("resource_id") or ""))
    user_id = resource.get("user_id")
    if scope in {"profile", "field"}:
        subject_value = str(resource.get("subject") or "").strip()
        if not user_id or subject_value not in {"personal", "ideal_partner"}:
            await fail_task(
                db, task.task_id, worker_id,
                error_code="AI_INPUT_INVALID", retryable=False,
            )
            return None
        # 缺陷 44：user_id 来自 resource_id 解析，可能非整数；转换失败时不可重试。
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            await fail_task(
                db, task.task_id, worker_id,
                error_code="AI_INPUT_INVALID", retryable=False,
            )
            return None
        if scope == "field":
            reason = "ai_profile_field_deleted"
        else:
            reason = str(payload.get("event_type") or "ai_profile_deleted")
        source_revision = (
            RevisionVector(**task.source_revision_json)
            if task.source_revision_json
            else RevisionVector()
        )
        await run_cleanup_for_user(
            db,
            user_id_int,
            reason,
            source_revision,
            subject=subject_value,
        )
        if scope == "profile":
            await purge_ai_resources(
                db,
                user_id_int,
                scope="profile",
                subject=subject_value,
            )
        return f"cleanup:user:{user_id}", source_revision
    snapshot_id = resource.get("snapshot_id")
    if snapshot_id:
        await purge_ai_resources(
            db,
            int(user_id) if user_id else 0,
            scope="search",
            resource_id=str(snapshot_id),
        )
        return f"cleanup:snapshot:{snapshot_id}", RevisionVector()
    if scope == "consent":
        consent_scope = str(resource.get("consent_scope") or "unknown")
        if user_id:
            consent_cleanup_scope = {
                "profile_text_extract": "consent_profile",
                "search_parse": "consent_search",
                "compatibility_shadow": "consent_compatibility",
            }.get(consent_scope)
            if consent_cleanup_scope is not None:
                await purge_ai_resources(
                    db,
                    int(user_id),
                    scope=consent_cleanup_scope,
                )
        return f"cleanup:consent:{user_id or 'unknown'}:{consent_scope}", (
            RevisionVector(**task.source_revision_json)
            if task.source_revision_json
            else RevisionVector()
        )
    await fail_task(
        db, task.task_id, worker_id,
        error_code="AI_INPUT_INVALID", retryable=False,
    )
    return None


async def list_profile_revisions(
    db: AsyncSession,
    owner_user_id: int,
    cursor: str | None = None,
    limit: int = 20,
) -> ProfileRevisionPage:
    """Cursor-paginated, self-owned, read-only immutable revision history.

    只返回本人历史；cursor 是 opaque base64（编码最后一个 revision id），
    分页按 revision id 倒序。
    """
    page_size = min(max(int(limit), 1), 100)
    cursor_id: int | None = None
    if cursor:
        try:
            cursor_id = int(base64.b64decode(cursor).decode())
        except (ValueError, TypeError):
            cursor_id = None
    result = await db.execute(
        text(
            "SELECT r.id, r.subject, r.revision_no, r.policy_revision, "
            "r.published_at, COUNT(f.revision_id) AS field_count "
            "FROM ai_profile_revision r "
            "LEFT JOIN ai_profile_revision_field f ON f.revision_id = r.id "
            "WHERE r.user_id = :user_id "
            "AND (:cursor_id IS NULL OR r.id < :cursor_id) "
            "GROUP BY r.id, r.subject, r.revision_no, r.policy_revision, r.published_at "
            "ORDER BY r.id DESC LIMIT :limit"
        ),
        {"user_id": owner_user_id, "cursor_id": cursor_id, "limit": page_size + 1},
    )
    rows = result.mappings().all()
    has_more = len(rows) > page_size
    items_rows = rows[:page_size]
    next_cursor: str | None = None
    if has_more and items_rows:
        next_cursor = base64.b64encode(
            str(items_rows[-1]["id"]).encode()
        ).decode()
    total_result = await db.execute(
        text("SELECT COUNT(*) AS total FROM ai_profile_revision WHERE user_id = :user_id"),
        {"user_id": owner_user_id},
    )
    total_row = await _first_row(total_result)
    total = int(total_row["total"]) if total_row else 0
    items = [
        ProfileRevisionRead(
            revision_id=int(row["id"]),
            subject=ProfileSubject(str(row["subject"])),
            revision_no=int(row["revision_no"]),
            policy_revision=str(row["policy_revision"]),
            field_count=int(row["field_count"] or 0),
            published_at=row["published_at"],
        )
        for row in items_rows
    ]
    return ProfileRevisionPage(
        items=items,
        next_cursor=next_cursor,
        total=total,
        total_is_estimate=False,
        has_more=has_more,
    )
