"""M04 AI 画像会话、回答、受控结构化抽取、确认、发布、历史与删除（Task 7+8，统一方案 §7）。

本模块是 M04 文字会话、抽取、草稿确认/发布/历史/删除边界的事实源：

- ``create_profile_session`` 只允许同一 ``user_id + subject`` 存在一个活动会话
  （已存在则回放/复用）；创建前校验 ``profile_text_extract`` 授权并快照当前
  revision 向量与授权信息。
- ``submit_profile_turn`` 先把原文 turn 落库（抽取失败不删原文），再以
  ``profile_extract`` 任务入队；同 ``client_turn_id`` 重复提交只回放原 turn，
  不创建第二个任务。
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
或 Worker）控制事务；唯一例外是 ``_mark_stale``：它必须在抛出
``PROFILE_SESSION_STALE`` 前自行提交 stale 状态变更（异常路径下 get_db 上下文
退出会回滚未提交事务，不提交则 stale 永不落库，同 user+subject 将无法重新创建
会话）。原回答与密钥永不进入日志或错误响应。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.ai_common import AiTaskStatus, AI_FIELD_ALLOWLIST, ProjectionKind
from app.schemas.ai_profile import (
    ProfileFieldConfirmationStatus,
    ProfileFieldPatchAction,
    ProfileQuestion,
    ProfileRevisionPage,
    ProfileRevisionRead,
    ProfileSessionStatus,
    ProfileSubject,
)
from app.services.ai.base import AITaskContext, StructuredExtractRequest
from app.services.ai.features import (
    ProjectionBuildError,
    build_feature_projection,
)
from app.services.ai.gateway import AIGateway
from app.services.ai.tasks import AiTaskRecord, TaskError, enqueue_task, fail_task
from app.services.derivation_outbox import run_cleanup_for_user
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
_PROFILE_QUESTION_BANK: dict[str, ProfileQuestion] = {
    "interest_tags": ProfileQuestion(
        id="interest_lifestyle_v1", text="最近让你投入的事情是什么？"
    ),
    "city_code": ProfileQuestion(
        id="city_residence_v1", text="你现在生活在哪座城市？"
    ),
    "marriage_status": ProfileQuestion(
        id="marriage_status_v1", text="你目前的婚姻状态是？"
    ),
    "education_level": ProfileQuestion(
        id="education_v1", text="你的最高学历是？"
    ),
    "height_cm": ProfileQuestion(id="height_v1", text="你的身高是多少？"),
    "income_band": ProfileQuestion(
        id="income_v1", text="你的收入大概在什么范围？"
    ),
    "occupation_group": ProfileQuestion(
        id="occupation_v1", text="你从事什么职业？"
    ),
    "lifestyle_tags": ProfileQuestion(
        id="lifestyle_v1", text="你平时的生活方式有什么特点？"
    ),
    "relationship_goal": ProfileQuestion(
        id="relationship_goal_v1", text="你对这段关系的期待是什么？"
    ),
    "age": ProfileQuestion(id="age_v1", text="你今年多大了？"),
}

_SESSION_COLUMNS = (
    "session_id, user_id, subject, input_mode, status, active_status, "
    "consent_version, policy_revision, current_question_id, "
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

# 供 app.workers.ai_worker.register_business_handlers 引用的公共常量。
PROJECTION_TASK_TYPE = _PROJECTION_TASK_TYPE
CLEANUP_TASK_TYPE = _CLEANUP_TASK_TYPE

_DRAFT_COLUMNS = (
    "draft_id, user_id, subject, session_id, status, expected_revision, "
    "consent_snapshot_json, policy_revision, prompt_version, schema_version, "
    "published_revision_id, expires_at, created_at, updated_at"
)
_DRAFT_FIELD_COLUMNS = (
    "draft_id, field_key, subject, value_json, display_value, source_type, "
    "source_turn_ids, confidence, visibility, consent_scope, schema_version, "
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
    def accepted(cls, turn: ProfileTurn, task: AiTaskRecord) -> "TurnSubmission":
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
    def replay(cls, turn: ProfileTurn) -> "TurnSubmission":
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
    value: Any = None
    display_value: str | None = None
    source_type: str | None = None
    source_turn_ids: tuple[str, ...] = ()
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
    """202 publish result; ``replayed=True`` means no second write happened."""

    task_id: str
    status: str
    replayed: bool
    revision: PublishedRevision | None

    @classmethod
    def accepted(cls, task: AiTaskRecord, revision: PublishedRevision) -> "TaskSubmission":
        return cls(
            task_id=task.task_id,
            status=task.status.value,
            replayed=False,
            revision=revision,
        )

    @classmethod
    def replay(cls, task: AiTaskRecord) -> "TaskSubmission":
        return cls(
            task_id=task.task_id,
            status=task.status.value,
            replayed=True,
            revision=None,
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


def assert_session_transition(source: ProfileSessionStatus, target: ProfileSessionStatus) -> None:
    """Raise ``ValueError`` unless the session state move is legal (§7.2)."""
    if target not in _SESSION_TRANSITIONS.get(source, set()):
        raise ValueError(
            f"illegal profile_session transition: {source.value} -> {target.value}"
        )


def next_profile_question(session: ProfileSession) -> ProfileQuestion | None:
    """Return the first missing-field question; never repeats confirmed fields.

    The question bank is ordered and fixed; the result is real coverage of the
    frozen allowlist, never a timer-based fake progress.
    """
    for field_key, question in _PROFILE_QUESTION_BANK.items():
        if field_key not in session.field_keys:
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
    """Return (non-deleted field keys, confirmed field keys) of a session draft."""
    result = await db.execute(
        text(
            "SELECT df.field_key, df.confirmation_status "
            "FROM ai_profile_draft_field df "
            "JOIN ai_profile_draft d ON d.draft_id = df.draft_id "
            "WHERE d.session_id = :session_id AND df.confirmation_status <> 'deleted'"
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


def _subject(value: Any) -> ProfileSubject:
    if isinstance(value, ProfileSubject):
        return value
    return ProfileSubject(str(value))


def _session_from_row(
    row: dict[str, Any],
    *,
    revision: RevisionVector,
    consent_snapshot: dict[str, Any],
    field_keys: frozenset[str],
    confirmed_keys: frozenset[str],
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
    return _session_from_row(
        row,
        revision=revision,
        consent_snapshot=consent_snapshot,
        field_keys=field_keys,
        confirmed_keys=confirmed_keys,
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
    consent = await _load_consent_grant(
        db, owner_user_id, PROFILE_CONSENT_SCOPE, str(row["consent_version"])
    )
    return _session_from_row(
        row,
        revision=revision,
        consent_snapshot=_consent_snapshot(consent) if consent else {},
        field_keys=field_keys,
        confirmed_keys=confirmed_keys,
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
    return _session_from_row(
        row,
        revision=revision,
        consent_snapshot=_consent_snapshot(consent),
        field_keys=field_keys,
        confirmed_keys=confirmed_keys,
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


async def _count_turns(db: AsyncSession, session_id: str) -> int:
    result = await db.execute(
        text("SELECT COUNT(*) FROM ai_profile_turn WHERE session_id = :session_id"),
        {"session_id": session_id},
    )
    return int(await _scalar(result) or 0)


async def _insert_turn(
    db: AsyncSession, session_id: str, user_id: int, client_turn_id: str, answer_text: str
) -> ProfileTurn:
    turn_id = uuid.uuid4().hex
    turn_no = (await _count_turns(db, session_id)) + 1
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
    for field in result.fields:
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
                " source_turn_ids, confidence, visibility, consent_scope, schema_version, "
                " prompt_version, content_hash, confirmation_status, created_at, updated_at) "
                "VALUES (:draft_id, :field_key, :subject, :value_json, :display_value, "
                " 'user_answer', :source_turn_ids, :confidence, :visibility, "
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
                "confidence": float(field.confidence),
                "visibility": "self",
                "consent_scope": consent_scope,
                "schema_version": getattr(field, "schema_version", None) or PROFILE_SCHEMA_VERSION,
                "prompt_version": PROFILE_PROMPT_VERSION,
                "content_hash": _content_hash(field.field_key, subject, value, source_turn_ids),
            },
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
        return None

    session = await load_owned_session(db, str(session_id), task.owner_user_id)
    turn = await find_turn_by_client_id(db, session.session_id, str(payload.get("client_turn_id") or ""))
    if turn is None or turn.turn_id != str(turn_id):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None

    context = AITaskContext(
        task_id=task.task_id,
        request_id=uuid.uuid4().hex,
        scene="profile_extract",
        provider="mock",
        model="mock-model-v1",
        prompt_version=PROFILE_PROMPT_VERSION,
        schema_version=PROFILE_SCHEMA_VERSION,
        input_revision=task.source_revision_json or {},
    )
    request = StructuredExtractRequest(
        subject=session.subject.value,
        turn_texts=(turn.answer_text,),
        consent_version=session.consent_version,
        policy_revision=session.policy_revision or PROFILE_POLICY_REVISION,
    )
    gateway = AIGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
    outcome = await gateway.structured_extract(context, request)
    if outcome.result is None:
        # 只改任务状态（fail_task/retry_wait），不产生草稿字段；返回 None 时
        # Worker 会再走一次 fail_task，但状态守卫使其成为无操作。
        await fail_task(
            db, task.task_id, worker_id,
            error_code=outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
            retryable=outcome.retryable,
        )
        return None

    draft_id = await _write_draft(db, session, turn, outcome.result)
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


def _validate_field_value(field_key: str, value: Any) -> None:
    """Domain check for ``replace`` (统一方案 §6.2 值域/长度/枚举).

    标签类字段要求非空字符串数组；其余 allowlist 字段要求非空标量。来源引用在
    replace 时保留（只改 value_json/content_hash，不动 source_turn_ids）。
    """
    if value is None:
        raise AIInputError(f"field {field_key} 的 value 不能为空")
    if field_key in _TAG_LIST_FIELDS:
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            raise AIInputError(f"field {field_key} 必须是「非空字符串数组」")
    elif not isinstance(value, (str, int, float)):
        raise AIInputError(f"field {field_key} 的 value 必须是标量（字符串或数字）")


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
        value=_maybe_json(row.get("value_json")),
        display_value=row.get("display_value"),
        source_type=str(row.get("source_type") or "user_answer"),
        source_turn_ids=(
            tuple(json.loads(source_turn_ids)) if source_turn_ids else ()
        ),
        confidence=float(row.get("confidence") or 0.0),
        visibility=row.get("visibility"),
        consent_scope=row.get("consent_scope"),
        schema_version=str(row.get("schema_version") or PROFILE_SCHEMA_VERSION),
        prompt_version=row.get("prompt_version"),
        content_hash=row.get("content_hash"),
        confirmation_status=str(row.get("confirmation_status") or "suggested"),
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
) -> ProfileDraft:
    """Apply per-field confirm/replace/reject/delete actions under optimistic lock.

    每个 action 都携带旧 revision（不匹配 → ``409 DRAFT_VERSION_CONFLICT``）；
    replace 重新过字段 Schema/来源约束（值域校验，来源引用保留）；delete 只把
    字段标记为 ``deleted``（不可见）；reject 标记 ``rejected``。成功后草稿
    ``expected_revision + 1``，返回新草稿。不 commit。
    """
    draft = await load_owned_draft_for_update(db, draft_id, owner_user_id)
    ensure_draft_editable(draft, "确认/修改")
    ensure_revision(draft.revision, expected_revision)
    known = {field.field_key for field in draft.fields}
    applied = 0
    for action in actions:
        if action.expected_revision != draft.revision:
            raise DraftVersionConflict()
        if action.field_key not in AI_FIELD_ALLOWLIST:
            raise AIInputError(f"field {action.field_key} 不在可编辑字段白名单内")
        if action.field_key not in known:
            raise AIInputError(f"field {action.field_key} 不存在于当前草稿")
        if action.action is ProfileFieldPatchAction.CONFIRM:
            await _update_draft_field_status(
                db, draft_id, action.field_key, ProfileFieldConfirmationStatus.CONFIRMED
            )
            applied += 1
        elif action.action is ProfileFieldPatchAction.REPLACE:
            _validate_field_value(action.field_key, action.value)
            existing = next(f for f in draft.fields if f.field_key == action.field_key)
            new_hash = _content_hash(
                action.field_key, draft.subject, action.value, existing.source_turn_ids
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
                    "value_json": json.dumps(action.value, ensure_ascii=False),
                    "display_value": _display_value(action.value),
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
    return await load_owned_draft(db, draft_id, owner_user_id)


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
    result = await db.execute(
        text(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_no "
            "FROM ai_profile_revision WHERE user_id = :user_id AND subject = :subject"
        ),
        {"user_id": owner_user_id, "subject": subject},
    )
    row = await _first_row(result)
    revision_no = int(row["next_no"]) if row else 1
    source_revision = await _load_revision_vector(db, owner_user_id)
    now = _now_utc()
    await db.execute(
        text(
            "INSERT INTO ai_profile_revision "
            "(user_id, subject, revision_no, draft_id, source_revision_json, "
            " policy_revision, published_by, published_at, created_at) "
            "VALUES (:user_id, :subject, :revision_no, :draft_id, :source_revision_json, "
            " :policy_revision, :published_by, :published_at, :created_at)"
        ),
        {
            "user_id": owner_user_id,
            "subject": subject,
            "revision_no": revision_no,
            "draft_id": draft.draft_id,
            "source_revision_json": json.dumps(source_revision.as_dict(), ensure_ascii=False),
            "policy_revision": draft.policy_revision or PROFILE_POLICY_REVISION,
            "published_by": owner_user_id,
            "published_at": now,
            "created_at": now,
        },
    )
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
    changed_keys: list[str] = []
    for field in fields:
        changed_keys.append(field.field_key)
        await db.execute(
            text(
                "INSERT INTO ai_profile_revision_field "
                "(revision_id, field_key, subject, value_json, display_value, confidence, "
                " source_type, source_turn_ids, content_hash, schema_version, prompt_version, "
                " created_at) "
                "VALUES (:revision_id, :field_key, :subject, :value_json, :display_value, "
                " :confidence, :source_type, :source_turn_ids, :content_hash, "
                " :schema_version, :prompt_version, :created_at)"
            ),
            {
                "revision_id": revision_id,
                "field_key": field.field_key,
                "subject": subject,
                "value_json": json.dumps(field.value, ensure_ascii=False),
                "display_value": field.display_value,
                "confidence": field.confidence,
                "source_type": field.source_type or "user_answer",
                "source_turn_ids": json.dumps(list(field.source_turn_ids), ensure_ascii=False),
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
) -> AiTaskRecord:
    """Enqueue the projection-build task that follows a confirmed publish.

    投影/搜索结果/兼容度快照的具体构建由 Task 9/10/11 的 handler 负责；本任务只
    记录受控摘要（revision/draft/subject/target），不含字段原文。不 commit。
    """
    target = "user_profile" if revision.subject == "personal" else "user_partner_preference"
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=_PROJECTION_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=RevisionVector(),
        consent=None,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                {
                    "revision_id": revision.revision_id,
                    "draft_id": revision.draft_id,
                    "subject": revision.subject,
                    "user_id": owner_user_id,
                    "projection_target": target,
                },
                ensure_ascii=False,
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
        return TaskSubmission.replay(
            _replay_or_conflict(existing, request_hash, "publish")
        )
    draft = await load_owned_draft_for_update(db, draft_id, owner_user_id)
    ensure_draft_editable(draft, "发布")
    ensure_revision(draft.revision, expected_revision)
    fields = confirmed_fields(draft)
    if not fields:
        raise AIInputError("at least one confirmed field is required")
    target = "user_profile" if draft.subject == "personal" else "user_partner_preference"
    revision = await insert_immutable_profile_revision(
        db, owner_user_id, draft, fields, target
    )
    revision_component = "profile" if draft.subject == "personal" else "preference"
    await increment_revision_and_enqueue(
        db,
        owner_user_id,
        RevisionKind(revision_component),
        revision.changed_field_keys,
        "ai_profile_published",
        priority=40,
    )
    task = await enqueue_cleanup_or_projection_task(
        db, owner_user_id, revision, idempotency_key, request_hash
    )
    await db.flush()
    return TaskSubmission.accepted(task, revision)


async def restore_profile_revision(
    db: AsyncSession, revision_id: int, owner_user_id: int
) -> ProfileDraft:
    """Create a new editable draft from an immutable revision snapshot.

    旧 revision 只读、不更新旧行；新草稿字段回填 ``suggested``（再由用户确认后
    发布）。新草稿 ``expected_revision=0``，可正常走 confirm → publish 流程。
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
            "source_type, source_turn_ids, content_hash, schema_version, prompt_version "
            "FROM ai_profile_revision_field WHERE revision_id = :revision_id"
        ),
        {"revision_id": revision_id},
    )
    field_rows = field_rows_result.mappings().all()
    draft_id = uuid.uuid4().hex
    now = _now_utc()
    consent = await _load_latest_consent(db, owner_user_id, PROFILE_CONSENT_SCOPE)
    consent_snapshot = _consent_snapshot(consent) if consent else {}
    await db.execute(
        text(
            "INSERT INTO ai_profile_draft "
            "(draft_id, user_id, subject, session_id, status, expected_revision, "
            " consent_snapshot_json, policy_revision, prompt_version, schema_version, "
            " expires_at, created_at, updated_at) "
            "VALUES (:draft_id, :user_id, :subject, NULL, 'draft', 0, "
            " :consent_snapshot_json, :policy_revision, :prompt_version, :schema_version, "
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
            "created_at": now,
        },
    )
    for field in field_rows:
        source_turn_ids = field.get("source_turn_ids")
        await db.execute(
            text(
                "INSERT INTO ai_profile_draft_field "
                "(draft_id, field_key, subject, value_json, display_value, source_type, "
                " source_turn_ids, confidence, visibility, consent_scope, schema_version, "
                " prompt_version, content_hash, confirmation_status, created_at, updated_at) "
                "VALUES (:draft_id, :field_key, :subject, :value_json, :display_value, "
                " :source_type, :source_turn_ids, :confidence, 'self', :consent_scope, "
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
    return _draft_from_row(draft_row, list(field_rows))


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
            "UPDATE ai_search_result SET stale = 1 WHERE target_user_id = :user_id"
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
    await db.execute(
        text(
            "UPDATE ai_consent_grant SET revoked_at = UTC_TIMESTAMP(), "
            "revoke_reason = 'ai_profile_deleted', updated_at = UTC_TIMESTAMP() "
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
                {
                    "subject": subject_value,
                    "scope": "profile",
                    "user_id": owner_user_id,
                    "event_type": event_type,
                },
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
        db, owner_user_id, kind, (field_key,), "ai_profile_field_deleted", priority=40
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
                {
                    "subject": subject_value,
                    "field_key": field_key,
                    "scope": "field",
                    "user_id": owner_user_id,
                },
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
    confirmed 字段），按主体映射投影种类：``personal`` → ``personal_searchable``
    + ``personal_compatibility``；``ideal_partner`` → ``ideal_partner_preference``。
    每种调用 ``build_feature_projection``（revision_vector=None 使投影以任务执行
    时的最新五维版本向量为准，保证投影 valid）；``ProjectionBuildError``（无已
    发布版本/无 allowlist 字段/无授权）按 Task 6 语义不可重试地失败为
    ``RESULT_STALE``，绝不落空投影。返回 ``(result_ref, revisions)``，revisions
    取任务入队时的 source_revision 使 ``complete_task`` 版本复核不误 supersede。
    """
    payload = task.payload_summary or {}
    user_id = payload.get("user_id")
    subject = payload.get("subject")
    if not user_id or not subject:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    subject_value = str(subject)
    if subject_value == ProfileSubject.PERSONAL.value:
        kinds = (
            ProjectionKind.PERSONAL_SEARCHABLE,
            ProjectionKind.PERSONAL_COMPATIBILITY,
        )
    elif subject_value == ProfileSubject.IDEAL_PARTNER.value:
        kinds = (ProjectionKind.IDEAL_PARTNER_PREFERENCE,)
    else:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    try:
        built: list[str] = []
        for kind in kinds:
            projection = await build_feature_projection(
                db, int(user_id), kind, revision_vector=None
            )
            built.append(f"{kind.value}:{projection.id if projection.id is not None else 'ok'}")
    except ProjectionBuildError:
        # 投影不可构建（发布后无该主体已确认字段/授权撤回等）：不可重试终态。
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
    user_id = payload.get("user_id")
    if scope in {"profile", "field"}:
        if not user_id:
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
        await run_cleanup_for_user(db, int(user_id), reason, source_revision)
        return f"cleanup:user:{user_id}", source_revision
    snapshot_id = payload.get("snapshot_id")
    if snapshot_id:
        await db.execute(
            text(
                "UPDATE ai_search_result SET stale = 1 WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": str(snapshot_id)},
        )
        return f"cleanup:snapshot:{snapshot_id}", RevisionVector()
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
