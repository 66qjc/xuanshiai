"""M03 AI 搜索服务（Task 10，统一方案 §8/§10.3/§11.2，执行计划 §3.1/§3.2）。

本模块是 M03 搜索的事实源：

- ``compile_search_conditions`` 把确认后的 AST 条件静态编译为现有
  ``DiscoveryFilters`` 与 ``soft_terms``/``unknown``/``conflicts``，永远不产生
  SQL 字符串、表名、列名或排序表达式；模型输出只能成为参数化筛选。
- ``create_search_draft`` 写 ``parsing`` 草稿并入队 ``search_parse`` 任务；
  每用户每分钟解析次数受 ``ai_search_parse_rate_per_minute`` 限流。
- ``parse_search_draft``（Worker handler）调用 ``AIGateway.parse_search_query``，
  把 allowlist 条件与未知原文逐行写入 ``ai_search_condition``，草稿转
  ``awaiting_confirmation``；未知原文作为 off-allowlist 伪条件保存，重解析不会
  恢复用户已删除的条件。
- ``confirm_search_draft`` 要求所有未删除 hard 条件已 ``confirmed`` 且无区间
  冲突，才在同一事务创建带 ``snapshot_hash``/``policy_revision``/
  ``consent_snapshot``/五维 revision vector 的 ``ai_search_snapshot`` 并入队
  ``search_execute`` 任务；编译失败不创建候选任务。
- ``materialize_search_snapshot``（Worker handler）复用
  ``CandidateQueryService`` 的 predicate/count/cursor，每次读取重新过
  ``CandidateVisibilityService`` 门禁（被拉黑/撤回对象排除），只把当前可见卡片
  引用与证据写入 ``ai_search_result``；软字段缺失记为 ``unknown``，不作为硬
  失败。
- 结果读取路径完全以 MySQL 为事实源（不依赖 Redis），因此 Redis 断开时天然
  从 MySQL 恢复。

与 Task 6/7/8 一致，本模块函数**不**调用 ``commit()``——调用方（路由或 Worker）
控制事务。S-06 语义召回 adapter 的 Phase 4 启动条件只记录在
``docs/api/AI搜索.md``，本任务不实现语义召回主链路。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import redis_client
from app.schemas.ai_common import CursorMeta
from app.schemas.ai_search import (
    SearchCondition,
    SearchConditionRead,
    SearchConditionUserAction,
    SearchDraftRead,
    SearchDraftStatus,
    SearchResultItemRead,
    SearchResultPageRead,
    SearchSuggestionRead,
)
from app.schemas.discovery import DiscoveryFilters
from app.services.ai.base import AITaskContext, SearchParseRequest
from app.services.ai.gateway import AIGateway
from app.services.ai.profile import CleanupTask, DraftVersionConflict
from app.services.ai.tasks import (
    AiTaskRecord,
    TaskError,
    enqueue_task,
    fail_task,
)
from app.services.candidate_query import (
    SORT_VERSION,
    CandidateQueryService,
    CandidateQuerySnapshot,
    InvalidCandidateCursor,
    build_query_fingerprint,
)
from app.services.candidate_visibility import (
    CandidateVisibilityService,
    ViewerContext,
    VisibilityScene,
)
from app.services.discovery import CARD_FROM, CARD_SELECT
from app.services.revisions import RevisionVector

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 冻结常量（统一方案 §8/§10.3）
# ----------------------------------------------------------------------

SEARCH_SCHEMA_VERSION = "search-condition-v1"
SEARCH_PROMPT_VERSION = "search-parse-prompt-v1"
SEARCH_POLICY_REVISION = "ai-policy-2026-08-07-v1"
SEARCH_CONSENT_SCOPE = "search_parse"
SEARCH_PARSE_TASK_TYPE = "search_parse"
SEARCH_EXECUTE_TASK_TYPE = "search_execute"
SEARCH_CLEANUP_TASK_TYPE = "cleanup"
# 结果证据 TTL：与统一方案 §8.3 示例的 result_expires_at（10 分钟）一致。
SEARCH_RESULT_TTL_MINUTES = 10
SEARCH_PAGE_SIZE_DEFAULT = 20
SEARCH_MATERIALIZATION_LIMIT = 200
# Task8 Step2：cursor 版本升级到 v2，编码 (generation, rank_position, target_user_id)
# 三元组。旧 v1 cursor 只编码 (snapshot_id, rank_position)，在 generation 切换后失效，
# 解码时若 generation 不匹配当前 active generation 则抛 InvalidCandidateCursor，
# 让前端重新拉第一页。
_MATERIALIZED_CURSOR_VERSION_V1 = "ai-search-result-v1"
_MATERIALIZED_CURSOR_VERSION = "ai-search-result-v2"
# Task8 Step2：snapshot 级 active generation 追踪。用 ai_search_snapshot.status
# 之外的轻量字段记录当前 active generation；为最小加法，在 snapshot 行的
# ``result_total`` 之外新增一个 generation 列。但为避免再加 DDL，这里采用
# snapshot 行的 ``degraded`` 字段高位复用——不，那会破坏语义。
# 最小加法方案：在 ``ai_search_snapshot`` 表不加列，而是在 ``ai_search_result`` 表
# 用 generation 列 + ``MAX(generation) WHERE stale=0`` 派生 active generation。
# 这样 snapshot 表无 DDL，generation 完全由 result 行派生。
_SEARCH_RESULT_DEFAULT_GENERATION = 1

# Task 1 冻结的 10 个 allowlist 字段 → operator/kind 静态映射（逐字，统一方案 §8.1）。
FIELD_RULES: dict[str, dict[str, Any]] = {
    "age": {"operators": {"between", "gte", "lte"}, "kind": "hard"},
    "city_code": {"operators": {"eq", "in"}, "kind": "hard"},
    "marriage_status": {"operators": {"eq", "in"}, "kind": "hard"},
    "education_level": {"operators": {"gte"}, "kind": "hard"},
    "height_cm": {"operators": {"between", "gte", "lte"}, "kind": "hard"},
    "income_band": {"operators": {"between", "gte", "lte"}, "kind": "hard"},
    "occupation_group": {"operators": {"eq"}, "kind": "soft"},
    "interest_tags": {"operators": {"contains"}, "kind": "soft"},
    "lifestyle_tags": {"operators": {"contains"}, "kind": "soft"},
    "relationship_goal": {"operators": {"eq"}, "kind": "soft"},
}

# soft 标签字段（contains → 现有字面 DiscoverySearch.tag 语义，逐条编译）。
_TAG_SOFT_FIELDS = frozenset({"interest_tags", "lifestyle_tags"})


# ----------------------------------------------------------------------
# 稳定业务错误（执行计划 §3.2 错误码注册表）
# ----------------------------------------------------------------------


class SearchPolicyDenied(ValueError):
    """422 AI_POLICY_DENIED：越权字段、敏感推断或模型自创字段。"""

    code = "AI_POLICY_DENIED"
    status_code = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SearchInputInvalid(ValueError):
    """400 AI_INPUT_INVALID：类型、长度、枚举或 operator 非法。"""

    code = "AI_INPUT_INVALID"
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SearchQuotaExceeded(Exception):
    """429 AI_QUOTA_EXCEEDED：每用户每分钟解析额度耗尽。"""

    code = "AI_QUOTA_EXCEEDED"
    status_code = 429
    retryable = True

    def __init__(self) -> None:
        super().__init__("AI 搜索解析频率过高，请稍后重试")
        self.message = "AI 搜索解析频率过高，请稍后重试"


class SearchConsentRequired(Exception):
    """403 AI_CONSENT_REQUIRED：search_parse 授权缺失或已撤回。"""

    code = "AI_CONSENT_REQUIRED"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("尚未同意 AI 搜索解析授权")
        self.message = "尚未同意 AI 搜索解析授权"


class SearchDraftNotFound(Exception):
    """404 SEARCH_DRAFT_NOT_FOUND：草稿不存在或非本人；不泄露归属。"""

    code = "SEARCH_DRAFT_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("搜索草稿不存在")
        self.message = "搜索草稿不存在"


class SearchSnapshotNotFound(Exception):
    """404 SEARCH_SNAPSHOT_NOT_FOUND：快照不存在、非本人或已删除。"""

    code = "SEARCH_SNAPSHOT_NOT_FOUND"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("搜索快照不存在")
        self.message = "搜索快照不存在"


class SearchDraftNotConfirmed(Exception):
    """409 RESULT_STALE：草稿未确认/未就绪，不能创建候选查询任务。"""

    code = "RESULT_STALE"
    status_code = 409

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SearchResultStale(Exception):
    """409 RESULT_STALE：结果已过期，需重新确认生成新快照。"""

    code = "RESULT_STALE"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("搜索结果已过期，请重新发起搜索")
        self.message = "搜索结果已过期，请重新发起搜索"


# ----------------------------------------------------------------------
# 领域对象
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledSearch:
    """服务器侧编译结果：只含现有 DiscoveryFilters 与受控条件。

    永不携带 SQL 字符串、表名、列名、排序表达式或模型生成的字段；
    ``sql_expression`` 恒为 ``None``（参数化 SQL 由 CandidateQueryService 负责）。
    """

    filters: DiscoveryFilters
    # Multi-value hard predicates (currently city_code/marriage_status ``in``).
    # Singleton ``in`` values remain in ``filters`` for backwards-compatible
    # discovery predicates; true multi-value values are emitted as typed data,
    # never as SQL text.
    hard_memberships: tuple[tuple[str, tuple[Any, ...]], ...] = ()
    soft_terms: tuple[tuple[str, Any], ...] = ()
    unknown: tuple[SearchCondition, ...] = ()
    conflicts: tuple[str, ...] = ()
    sql_expression: None = None


@dataclass(frozen=True)
class SearchDraftParse:
    """202 draft+parse task 结果（对应 ``SearchDraftParseRead``）。"""

    draft_id: str
    status: str
    task_id: str
    condition_schema_version: str = SEARCH_SCHEMA_VERSION
    expires_at: datetime | None = None


@dataclass(frozen=True)
class SearchSnapshot:
    """202 confirm 结果：不可变快照 + 已入队的 search_execute 任务。"""

    snapshot_id: str
    task_id: str
    status: str
    condition_schema_version: str = SEARCH_SCHEMA_VERSION
    expires_at: datetime | None = None
    degraded: bool = False
    replayed: bool = False


@dataclass(frozen=True)
class SearchEvidence:
    """一个候选的结果证据：满足数、证据引用与 source revision。"""

    matched_condition_count: int
    matched_conditions: list[str]
    unknown_conditions: list[str]
    reason_codes: list[str]
    profile_revision: int
    projection_id: int | None = None
    source_hash: str | None = None
    consent_snapshot: dict[str, Any] | None = None
    source_revision: dict[str, int] | None = None
    card: dict[str, Any] | None = None
    soft_match_count: int = 0


# ----------------------------------------------------------------------
# 编译（纯函数，禁止数据库查询）
# ----------------------------------------------------------------------

_MARRIAGE_VALUE_MAP = {"single": 1, "married": 2, "divorced": 3}


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SearchInputInvalid("条件 value 必须是整数") from exc


def _float_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SearchInputInvalid("条件 value 必须是数字") from exc


def _dict_value(value: Any, key: str) -> Any:
    if not isinstance(value, dict) or key not in value or value[key] is None:
        raise SearchInputInvalid(f"条件 value 必须包含 {key}")
    return value[key]


def _single_city(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], str)
        and value[0].strip()
    ):
        return value[0].strip()
    raise SearchInputInvalid("city_code 筛选一次仅支持一座城市")


def _city_values(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    normalized = tuple(
        item.strip()
        for item in values
        if isinstance(item, str) and item.strip()
    )
    if not normalized or len(normalized) != len(values):
        raise SearchInputInvalid("city_code in 必须是非空城市编码数组")
    return tuple(dict.fromkeys(normalized))


def _marriage_value(value: Any) -> int:
    if isinstance(value, list):
        if len(value) == 1:
            value = value[0]
        else:
            raise SearchInputInvalid("marriage_status 一次仅支持一个取值")
    if isinstance(value, int) and not isinstance(value, bool) and value in (1, 2, 3):
        return value
    if isinstance(value, str):
        mapped = _MARRIAGE_VALUE_MAP.get(value.strip())
        if mapped is not None:
            return mapped
    raise SearchInputInvalid("marriage_status 必须是 1/2/3 或 single/married/divorced")


def _marriage_values(value: Any) -> tuple[int, ...]:
    values = value if isinstance(value, list) else [value]
    normalized = tuple(_marriage_value(item) for item in values)
    if not normalized:
        raise SearchInputInvalid("marriage_status in 必须是非空数组")
    return tuple(dict.fromkeys(normalized))


def _enum_value(value: Any) -> str:
    """Return the raw string value of a str/Enum or a plain string."""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


class CompiledFilters(DiscoveryFilters):
    """``DiscoveryFilters`` 子类：AST 条件 → 筛选字段的静态映射。

    使用 ``model_copy(update=...)`` 故意跳过基类的区间校验，使倒置区间
    （如 ``age_min > age_max``）可被 ``detect_range_conflicts`` 报告为冲突，
    而不是在编译期抛 ``ValueError``。本子类保持在 ai_search 模块内，不修改
    ``app/schemas/discovery.py`` 的既有 Schema。
    """

    def with_condition(self, condition: SearchCondition) -> CompiledFilters:
        field_key = condition.field_key
        operator = _enum_value(condition.operator)
        value = condition.value
        if field_key == "age":
            if operator == "between":
                return self.model_copy(
                    update={
                        "age_min": _int_value(_dict_value(value, "min")),
                        "age_max": _int_value(_dict_value(value, "max")),
                    }
                )
            if operator == "gte":
                return self.model_copy(update={"age_min": _int_value(value)})
            return self.model_copy(update={"age_max": _int_value(value)})
        if field_key == "city_code":
            return self.model_copy(update={"city_code": _single_city(value)})
        if field_key == "marriage_status":
            return self.model_copy(update={"marriage_status": _marriage_value(value)})
        if field_key == "education_level":
            return self.model_copy(update={"education_min": _int_value(value)})
        if field_key == "height_cm":
            if operator == "between":
                return self.model_copy(
                    update={
                        "height_min": _int_value(_dict_value(value, "min")),
                        "height_max": _int_value(_dict_value(value, "max")),
                    }
                )
            if operator == "gte":
                return self.model_copy(update={"height_min": _int_value(value)})
            return self.model_copy(update={"height_max": _int_value(value)})
        if field_key == "income_band":
            if operator == "between":
                return self.model_copy(
                    update={
                        "income_min": _float_value(_dict_value(value, "min")),
                        "income_max": _float_value(_dict_value(value, "max")),
                    }
                )
            if operator == "gte":
                return self.model_copy(update={"income_min": _float_value(value)})
            return self.model_copy(update={"income_max": _float_value(value)})
        raise SearchInputInvalid(f"hard 字段 {field_key} 缺少静态映射")


def detect_range_conflicts(filters: DiscoveryFilters) -> tuple[str, ...]:
    """返回 age/height_cm/income_band 区间倒置冲突（统一方案 §8.1）。"""
    conflicts: list[str] = []
    if (
        filters.age_min is not None
        and filters.age_max is not None
        and filters.age_min > filters.age_max
    ):
        conflicts.append("age 区间倒置：下限大于上限")
    if (
        filters.height_min is not None
        and filters.height_max is not None
        and filters.height_min > filters.height_max
    ):
        conflicts.append("height_cm 区间倒置：下限大于上限")
    if (
        filters.income_min is not None
        and filters.income_max is not None
        and filters.income_min > filters.income_max
    ):
        conflicts.append("income_band 区间倒置：下限大于上限")
    return tuple(conflicts)


def compile_search_conditions(conditions: list[SearchCondition]) -> CompiledSearch:
    """把 AST 条件静态编译为现有 ``DiscoveryFilters`` 与受控 soft/unknown 列表。

    - 未注册字段：confirmed → ``SearchPolicyDenied``；否则进 ``unknown``。
    - 已注册字段用非法 operator → ``SearchInputInvalid``。
    - 非 confirmed 条件不进入筛选/soft_terms（用户动作由 confirm 前置保证）。
    - 永不生成 SQL；参数化 SQL 由 ``CandidateQueryService`` 负责。
    """
    filters = CompiledFilters()
    hard_memberships: list[tuple[str, tuple[Any, ...]]] = []
    soft_terms: list[tuple[str, Any]] = []
    unknown: list[SearchCondition] = []
    for condition in conditions:
        rule = FIELD_RULES.get(condition.field_key)
        if rule is None:
            if condition.user_action == SearchConditionUserAction.CONFIRMED:
                raise SearchPolicyDenied("AI_POLICY_DENIED")
            unknown.append(condition)
            continue
        if _enum_value(condition.operator) not in rule["operators"]:
            raise SearchInputInvalid("AI_INPUT_INVALID")
        if condition.user_action != SearchConditionUserAction.CONFIRMED:
            continue
        if rule["kind"] == "hard":
            operator = _enum_value(condition.operator)
            if operator == "in" and condition.field_key == "city_code":
                values = _city_values(condition.value)
                if len(values) == 1:
                    filters = filters.with_condition(
                        condition.model_copy(update={"operator": "eq", "value": values[0]})
                    )
                else:
                    hard_memberships.append((condition.field_key, values))
            elif operator == "in" and condition.field_key == "marriage_status":
                values = _marriage_values(condition.value)
                if len(values) == 1:
                    filters = filters.with_condition(
                        condition.model_copy(update={"operator": "eq", "value": values[0]})
                    )
                else:
                    hard_memberships.append((condition.field_key, values))
            else:
                filters = filters.with_condition(condition)
        else:
            soft_terms.append((condition.field_key, condition.value))
    return CompiledSearch(
        filters=filters,
        hard_memberships=tuple(hard_memberships),
        soft_terms=tuple(soft_terms),
        unknown=tuple(unknown),
        conflicts=detect_range_conflicts(filters),
        sql_expression=None,
    )


# ----------------------------------------------------------------------
# 内部辅助（不 commit，由调用方控制事务）
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


def _is_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return False
    if isinstance(expires_at, datetime):
        return expires_at.replace(tzinfo=None) < _now_utc()
    return False


def _consent_snapshot(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    granted_at = row.get("granted_at")
    return {
        "scope": str(row.get("scope") or SEARCH_CONSENT_SCOPE),
        "version": str(row.get("version") or ""),
        "policy_revision": str(row.get("policy_revision") or SEARCH_POLICY_REVISION),
        "granted_at": granted_at.isoformat() if granted_at else None,
    }


async def _load_active_consent(
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


_DRAFT_COLUMNS = (
    "draft_id, user_id, query_text, source, locale, status, condition_revision, "
    "condition_schema_version, policy_revision, consent_snapshot_json, expires_at, "
    "last_patch_idempotency_key, last_patch_request_digest, last_patch_response_json, "
    "created_at, updated_at"
)
_CONDITION_COLUMNS = (
    "id, draft_id, condition_revision, condition_no, field_key, operator, "
    "value_json, condition_kind, confidence, source_span, user_action, "
    "created_at, updated_at"
)
_SNAPSHOT_COLUMNS = (
    "id, snapshot_id, user_id, draft_id, snapshot_hash, status, "
    "condition_schema_version, policy_revision, consent_snapshot_json, "
    "source_revision_json, result_total, degraded, expires_at, invalidated_at, created_at"
)


async def _load_draft_row(
    db: AsyncSession, draft_id: str, *, for_update: bool = False
) -> dict[str, Any] | None:
    lock = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_DRAFT_COLUMNS} FROM ai_search_draft "
            f"WHERE draft_id = :draft_id LIMIT 1{lock}"
        ),
        {"draft_id": draft_id},
    )
    return await _first_row(result)


async def _load_condition_rows(
    db: AsyncSession, draft_id: str
) -> list[dict[str, Any]]:
    result = await db.execute(
        text(
            f"SELECT {_CONDITION_COLUMNS} FROM ai_search_condition "
            "WHERE draft_id = :draft_id ORDER BY condition_no ASC"
        ),
        {"draft_id": draft_id},
    )
    return list(result.mappings().all())


async def _load_snapshot_row(
    db: AsyncSession, snapshot_id: str, *, for_update: bool = False
) -> dict[str, Any] | None:
    lock = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM ai_search_snapshot "
            f"WHERE snapshot_id = :snapshot_id LIMIT 1{lock}"
        ),
        {"snapshot_id": snapshot_id},
    )
    return await _first_row(result)


async def _find_snapshot_row_by_draft(
    db: AsyncSession, draft_id: str
) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM ai_search_snapshot "
            "WHERE draft_id = :draft_id AND invalidated_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"draft_id": draft_id},
    )
    return await _first_row(result)


async def _update_draft_status(
    db: AsyncSession, draft_id: str, status: str
) -> None:
    await db.execute(
        text(
            "UPDATE ai_search_draft SET status = :status, "
            "updated_at = UTC_TIMESTAMP() WHERE draft_id = :draft_id"
        ),
        {"status": status, "draft_id": draft_id},
    )


async def _bump_condition_revision(db: AsyncSession, draft_id: str) -> None:
    await db.execute(
        text(
            "UPDATE ai_search_draft SET condition_revision = condition_revision + 1, "
            "updated_at = UTC_TIMESTAMP() WHERE draft_id = :draft_id"
        ),
        {"draft_id": draft_id},
    )


async def _insert_condition(
    db: AsyncSession,
    draft_id: str,
    revision_no: int,
    condition_no: int,
    field_key: str,
    operator: str,
    value: Any,
    kind: str,
    confidence: float,
    source_span: str | None,
    user_action: str,
) -> None:
    await db.execute(
        text(
            "INSERT INTO ai_search_condition "
            "(draft_id, condition_revision, condition_no, field_key, operator, "
            " value_json, condition_kind, confidence, source_span, user_action, "
            " created_at, updated_at) "
            "VALUES (:draft_id, :condition_revision, :condition_no, :field_key, "
            " :operator, :value_json, :condition_kind, :confidence, :source_span, "
            " :user_action, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ),
        {
            "draft_id": draft_id,
            "condition_revision": revision_no,
            "condition_no": condition_no,
            "field_key": field_key,
            "operator": operator,
            "value_json": json.dumps(value, ensure_ascii=False) if value is not None else None,
            "condition_kind": kind,
            "confidence": float(confidence),
            "source_span": source_span,
            "user_action": user_action,
        },
    )


async def _update_condition_action(
    db: AsyncSession, draft_id: str, condition_no: int, action: str
) -> None:
    await db.execute(
        text(
            "UPDATE ai_search_condition SET user_action = :action, "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE draft_id = :draft_id AND condition_no = :condition_no"
        ),
        {"action": action, "draft_id": draft_id, "condition_no": condition_no},
    )


async def _update_condition_value(
    db: AsyncSession, draft_id: str, condition_no: int, value: Any
) -> None:
    await db.execute(
        text(
            "UPDATE ai_search_condition SET value_json = :value_json, "
            "user_action = 'edited', updated_at = UTC_TIMESTAMP() "
            "WHERE draft_id = :draft_id AND condition_no = :condition_no"
        ),
        {
            "value_json": json.dumps(value, ensure_ascii=False),
            "draft_id": draft_id,
            "condition_no": condition_no,
        },
    )


def _condition_from_row(row: dict[str, Any]) -> SearchCondition:
    return SearchCondition(
        field_key=str(row["field_key"]),
        operator=str(row["operator"]),
        value=_maybe_json(row.get("value_json")),
        kind=str(row.get("condition_kind") or "soft"),
        confidence=float(row.get("confidence") or 0.0),
        source_span=row.get("source_span"),
        user_action=str(row.get("user_action") or "pending"),
    )


def _condition_read_from_row(row: dict[str, Any]) -> SearchConditionRead:
    return SearchConditionRead(
        field_key=str(row["field_key"]),
        operator=str(row["operator"]),
        value=_maybe_json(row.get("value_json")),
        kind=str(row.get("condition_kind") or "soft"),
        confidence=float(row.get("confidence") or 0.0),
        source_span=row.get("source_span"),
        user_action=str(row.get("user_action") or "pending"),
    )


def _draft_conflicts(condition_rows: list[dict[str, Any]]) -> list[str]:
    """从已确认条件重算区间冲突（草稿读取与 confirm 一致）。

    只对 allowlist 内字段编译；off-allowlist（未知原文）条件即使被误确认，也只在
    confirm 时以 422 AI_POLICY_DENIED 拒绝，不在只读 GET 中抛错。
    """
    confirmed = [
        _condition_from_row(row)
        for row in condition_rows
        if str(row.get("user_action") or "pending") == "confirmed"
        and str(row.get("field_key") or "") in FIELD_RULES
    ]
    return list(compile_search_conditions(confirmed).conflicts)


async def _find_write_task(
    db: AsyncSession, owner_user_id: int, task_type: str, idempotency_key: str
) -> AiTaskRecord | None:
    result = await db.execute(
        text(
            "SELECT id, task_id, owner_user_id, task_type, scene, idempotency_key, "
            "request_digest, status, stage, attempt_count, max_attempts, next_run_at, "
            "lease_owner, lease_until, consent_snapshot_json, source_revision_json, "
            "payload_summary, error_code, error_message, result_ref, "
            "created_at, updated_at, started_at, finished_at "
            "FROM ai_task "
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


def _replay_or_conflict(existing: AiTaskRecord, request_hash: str) -> AiTaskRecord:
    if existing.request_digest != request_hash:
        raise TaskError(
            code="TASK_IDEMPOTENCY_CONFLICT",
            message="Idempotency-Key 已用于不同请求内容",
            status_code=409,
        )
    return existing


def _hash_draft_request(
    query_text: str, source: str | None, locale: str | None
) -> str:
    payload = json.dumps(
        {
            "query_text": query_text,
            "source": (source or "")[:24] or None,
            "locale": (locale or "")[:16] or None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _hash_confirm_request(draft_id: str, condition_revision: int) -> str:
    payload = json.dumps(
        {"draft_id": draft_id, "condition_revision": int(condition_revision)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _hash_patch_request(
    draft_id: str, expected_condition_revision: int, patches: list[Any]
) -> str:
    canonical_patches = [
        patch.model_dump(mode="json") if hasattr(patch, "model_dump") else dict(patch)
        for patch in patches
    ]
    payload = json.dumps(
        {
            "draft_id": draft_id,
            "expected_condition_revision": int(expected_condition_revision),
            "patches": canonical_patches,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _hash_delete_request(snapshot_id: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"snapshot_id": snapshot_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _cleanup_payload_for_snapshot(
    owner_user_id: int, snapshot_id: str
) -> dict[str, Any]:
    return {
        "scope": "search",
        "resource_id": f"snapshot:{owner_user_id}:{snapshot_id}",
        "version": RevisionVector().as_dict(),
        "purge_deadline": (_now_utc() + timedelta(minutes=15)).isoformat(),
    }


def _snapshot_hash(conditions: list[SearchCondition], policy_revision: str) -> str:
    raw = json.dumps(
        {
            "policy_revision": policy_revision,
            "conditions": [
                {
                    "field_key": condition.field_key,
                    "operator": str(condition.operator),
                    "value": condition.value,
                    "kind": str(condition.kind),
                    "user_action": str(condition.user_action),
                }
                for condition in conditions
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


# ----------------------------------------------------------------------
# 解析额度（每用户每分钟 ai_search_parse_rate_per_minute 次）
# ----------------------------------------------------------------------

_MINUTE_QUOTA_LUA = """
local value = redis.call('INCR', KEYS[1])
if value == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
if value > tonumber(ARGV[1]) then redis.call('DECR', KEYS[1]); return 0 end
return 1
"""

_local_minute_quota: dict[str, int] = {}


def reset_local_quota_for_testing() -> None:
    """清空本地（无 Redis）分钟额度计数，仅测试使用。"""
    _local_minute_quota.clear()


def _parse_quota_window() -> int:
    """当前分钟窗口号（测试可 monkeypatch 固定，避免跨分钟边界 flake，I-3）。"""
    return int(time.time() // 60)


async def _consume_parse_quota(db: AsyncSession, user_id: int) -> None:
    limit = settings.ai_search_parse_rate_per_minute
    window = _parse_quota_window()
    key = f"ai:search:parse:{user_id}:{window}"
    try:
        consumed = await redis_client.eval(_MINUTE_QUOTA_LUA, 1, key, limit, 120)
        if not consumed:
            raise SearchQuotaExceeded()
    except RedisError:
        if settings.environment in {"development", "testing"}:
            used = _local_minute_quota.get(key, 0)
            if used >= limit:
                raise SearchQuotaExceeded()
            _local_minute_quota[key] = used + 1
        else:
            # Redis 不可用时对限流放行（尽力而为），不阻塞搜索主链路。
            logger.warning("ai_search_quota_redis_unavailable user_id=%s", user_id)


# ----------------------------------------------------------------------
# 草稿创建与解析
# ----------------------------------------------------------------------


def normalize_search_query(query_text: str) -> str:
    """Trim 并校验 query_text（1..1000 字符，统一方案 §8.3）。"""
    normalized = query_text.strip()
    if not 1 <= len(normalized) <= 1000:
        raise SearchInputInvalid("query_text must contain 1..1000 characters")
    return normalized


async def create_search_draft(
    db: AsyncSession,
    owner_user_id: int,
    query_text: str,
    source: str | None,
    locale: str | None,
    idempotency_key: str,
) -> SearchDraftParse:
    """写 ``parsing`` 草稿并入队 ``search_parse`` 任务（202 draft+parse task）。

    输入校验（query_text 长度）先于任何数据库查询；``search_parse`` 授权缺失 →
    403 AI_CONSENT_REQUIRED；每分钟解析额度耗尽 → 429 AI_QUOTA_EXCEEDED。
    不 commit。
    """
    normalized = normalize_search_query(query_text)
    consent = await _load_active_consent(db, owner_user_id, SEARCH_CONSENT_SCOPE)
    if consent is None:
        raise SearchConsentRequired()
    request_hash = _hash_draft_request(normalized, source, locale)
    existing = await _find_write_task(
        db, owner_user_id, SEARCH_PARSE_TASK_TYPE, idempotency_key
    )
    if existing is not None:
        _replay_or_conflict(existing, request_hash)
        payload = existing.payload_summary or {}
        existing_draft_id = payload.get("draft_id")
        if not existing_draft_id:
            raise TaskError(
                code="AI_INPUT_INVALID",
                message="幂等任务缺少草稿引用",
                status_code=400,
            )
        return SearchDraftParse(
            draft_id=str(existing_draft_id),
            status=SearchDraftStatus.PARSING.value,
            task_id=existing.task_id,
            condition_schema_version=SEARCH_SCHEMA_VERSION,
        )
    await _consume_parse_quota(db, owner_user_id)
    consent_snapshot = _consent_snapshot(consent)
    revision = await _load_revision_vector(db, owner_user_id)
    draft_id = uuid.uuid4().hex
    expires_at = _now_utc() + timedelta(hours=settings.ai_search_draft_expire_hours)
    policy_revision = consent_snapshot.get("policy_revision") or SEARCH_POLICY_REVISION
    await db.execute(
        text(
            "INSERT INTO ai_search_draft "
            "(draft_id, user_id, query_text, source, locale, status, condition_revision, "
            " condition_schema_version, policy_revision, consent_snapshot_json, "
            " expires_at, created_at, updated_at) "
            "VALUES (:draft_id, :user_id, :query_text, :source, :locale, 'parsing', 0, "
            " :condition_schema_version, :policy_revision, :consent_snapshot_json, "
            " :expires_at, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ),
        {
            "draft_id": draft_id,
            "user_id": owner_user_id,
            "query_text": normalized,
            "source": (source or "")[:24] or None,
            "locale": (locale or "")[:16] or None,
            "condition_schema_version": SEARCH_SCHEMA_VERSION,
            "policy_revision": policy_revision,
            "consent_snapshot_json": json.dumps(consent_snapshot, ensure_ascii=False),
            "expires_at": expires_at,
        },
    )
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=SEARCH_PARSE_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=revision,
        consent=consent_snapshot,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                {
                    "draft_id": draft_id,
                    "source": source,
                    "locale": locale,
                },
                ensure_ascii=False,
            ),
            "task_id": task.task_id,
        },
    )
    await db.flush()
    return SearchDraftParse(
        draft_id=draft_id,
        status=SearchDraftStatus.PARSING.value,
        task_id=task.task_id,
        condition_schema_version=SEARCH_SCHEMA_VERSION,
        expires_at=expires_at,
    )


async def parse_search_draft(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """``search_parse`` Worker handler：调用 Gateway 并落条件行。

    结果只写 ``pending`` 条件与 off-allowlist 未知原文伪条件；成功后草稿转
    ``awaiting_confirmation``。已解析草稿（已有条件行）重复执行时直接推进状态
    （幂等）。失败只改变任务状态，不产生条件。返回 ``(result_ref, revisions)``。
    """
    payload = task.payload_summary or {}
    draft_id = payload.get("draft_id")
    if not draft_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_FEATURE_DISABLED", retryable=False,
        )
        return None
    draft = await _load_draft_row(db, str(draft_id))
    if draft is None or int(draft["user_id"]) != task.owner_user_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    if str(draft["status"]) not in (
        SearchDraftStatus.PARSING.value,
        SearchDraftStatus.AWAITING_CONFIRMATION.value,
    ):
        await fail_task(
            db, task.task_id, worker_id,
            error_code="RESULT_STALE", retryable=False,
        )
        return None

    existing = await _load_condition_rows(db, str(draft_id))
    if not existing:
        context = AITaskContext(
            task_id=task.task_id,
            request_id=uuid.uuid4().hex,
            scene="search_parse",
            provider=settings.ai_provider_name,
            model=settings.ai_model_name,
            prompt_version=SEARCH_PROMPT_VERSION,
            schema_version=SEARCH_SCHEMA_VERSION,
            input_revision=task.source_revision_json or {},
        )
        request = SearchParseRequest(
            query_text=str(draft["query_text"]),
            locale=draft.get("locale"),
        )
        gateway = AIGateway(timeout_seconds=settings.ai_gateway_timeout_seconds)
        outcome = await gateway.parse_search_query(context, request)
        if outcome.result is None:
            await _update_draft_status(
                db, str(draft_id), SearchDraftStatus.FAILED.value
            )
            await fail_task(
                db, task.task_id, worker_id,
                error_code=outcome.error_code or "AI_TEMPORARILY_UNAVAILABLE",
                retryable=outcome.retryable,
            )
            return None
        revision_no = int(draft.get("condition_revision") or 0)
        condition_no = 0
        for condition in outcome.result.conditions:
            await _insert_condition(
                db,
                str(draft_id),
                revision_no,
                condition_no,
                str(condition.field_key),
                str(condition.operator),
                condition.value,
                str(condition.kind),
                float(condition.confidence),
                condition.source_span,
                SearchConditionUserAction.PENDING.value,
            )
            condition_no += 1
        for term in outcome.result.unknown:
            await _insert_condition(
                db,
                str(draft_id),
                revision_no,
                condition_no,
                str(term),
                "eq",
                None,
                "soft",
                0.0,
                str(term),
                SearchConditionUserAction.PENDING.value,
            )
            condition_no += 1
    if str(draft["status"]) == SearchDraftStatus.PARSING.value:
        await _update_draft_status(
            db, str(draft_id), SearchDraftStatus.AWAITING_CONFIRMATION.value
        )
    revisions = RevisionVector(**task.source_revision_json) if task.source_revision_json else RevisionVector()
    return f"search-draft:{draft_id}", revisions


# ----------------------------------------------------------------------
# 草稿读取 / 条件编辑
# ----------------------------------------------------------------------


async def load_search_draft(
    db: AsyncSession, draft_id: str, owner_user_id: int
) -> SearchDraftRead:
    """只读草稿 + AST 条件 + 未知项 + 冲突（仅本人；过期仍可读摘要）。"""
    row = await _load_draft_row(db, draft_id)
    if row is None or int(row["user_id"]) != owner_user_id:
        raise SearchDraftNotFound()
    condition_rows = await _load_condition_rows(db, draft_id)
    conditions: list[SearchConditionRead] = []
    unknown: list[str] = []
    for condition_row in condition_rows:
        if str(condition_row["field_key"]) not in FIELD_RULES:
            if str(condition_row.get("user_action") or "pending") != "removed":
                unknown.append(str(condition_row["field_key"]))
        conditions.append(_condition_read_from_row(condition_row))
    return SearchDraftRead(
        draft_id=str(row["draft_id"]),
        status=SearchDraftStatus(str(row["status"])),
        condition_revision=int(row.get("condition_revision") or 0),
        condition_schema_version=str(
            row.get("condition_schema_version") or SEARCH_SCHEMA_VERSION
        ),
        conditions=conditions,
        unknown=unknown,
        conflicts=_draft_conflicts(condition_rows),
        expires_at=row.get("expires_at"),
    )


async def patch_search_draft(
    db: AsyncSession,
    draft_id: str,
    owner_user_id: int,
    patches: list[Any],
    expected_condition_revision: int,
    idempotency_key: str = "",
) -> SearchDraftRead:
    """显式 confirm/edit/remove 条件（condition_revision 乐观锁）。

    remove 只标记不可见，重解析不会恢复；edit 更新 value 并置 ``edited``（需再
    confirm）；仅 ``awaiting_confirmation`` 草稿可编辑。不 commit。
    """
    draft = await _load_draft_row(db, draft_id, for_update=True)
    if draft is None or int(draft["user_id"]) != owner_user_id:
        raise SearchDraftNotFound()
    request_hash = _hash_patch_request(
        draft_id, expected_condition_revision, patches
    )
    response_history = _maybe_json(draft.get("last_patch_response_json")) or {}
    history_entry = (
        response_history.get("operations", {}).get(idempotency_key)
        if isinstance(response_history, dict)
        and isinstance(response_history.get("operations"), dict)
        and idempotency_key
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
            return SearchDraftRead.model_validate(response_payload)
    if idempotency_key and draft.get("last_patch_idempotency_key"):
        if str(draft["last_patch_idempotency_key"]) == idempotency_key:
            if str(draft.get("last_patch_request_digest") or "") != request_hash:
                raise TaskError(
                    code="TASK_IDEMPOTENCY_CONFLICT",
                    message="Idempotency-Key 已用于不同请求内容",
                    status_code=409,
                )
            return await load_search_draft(db, draft_id, owner_user_id)
    if int(draft.get("condition_revision") or 0) != int(expected_condition_revision):
        raise DraftVersionConflict()
    if str(draft["status"]) != SearchDraftStatus.AWAITING_CONFIRMATION.value:
        raise SearchDraftNotConfirmed("草稿当前不可编辑（需处于待确认状态）")
    condition_rows = await _load_condition_rows(db, draft_id)
    known_nos = {int(row["condition_no"]) for row in condition_rows}
    applied = 0
    for patch in patches:
        condition_no = int(patch.condition_no)
        if condition_no not in known_nos:
            raise SearchInputInvalid(f"condition_no {condition_no} 不存在")
        action = str(patch.action)
        if action == "remove":
            await _update_condition_action(db, draft_id, condition_no, "removed")
            applied += 1
        elif action == "confirm":
            await _update_condition_action(db, draft_id, condition_no, "confirmed")
            applied += 1
        elif action == "edit":
            if patch.value is None:
                raise SearchInputInvalid("edit 必须提供 value")
            await _update_condition_value(db, draft_id, condition_no, patch.value)
            applied += 1
        else:
            raise SearchInputInvalid(f"action {action} 非法")
    if applied:
        await _bump_condition_revision(db, draft_id)
    updated = await load_search_draft(db, draft_id, owner_user_id)
    if idempotency_key:
        operations = dict(
            (response_history.get("operations") or {})
            if isinstance(response_history, dict)
            else {}
        )
        operations[idempotency_key] = {
            "request_digest": request_hash,
            "response": updated.model_dump(mode="json"),
        }
        if len(operations) > 64:
            operations = dict(list(operations.items())[-64:])
        await db.execute(
            text(
                "UPDATE ai_search_draft SET last_patch_idempotency_key = :key, "
                "last_patch_request_digest = :request_digest, "
                "last_patch_response_json = :response_json, "
                "updated_at = UTC_TIMESTAMP() WHERE draft_id = :draft_id"
            ),
            {
                "draft_id": draft_id,
                "key": idempotency_key,
                "request_digest": request_hash,
                "response_json": json.dumps(
                    {"operations": operations}, ensure_ascii=False
                ),
            },
        )
    return updated


# ----------------------------------------------------------------------
# 确认 → 不可变快照 + search_execute 任务
# ----------------------------------------------------------------------


async def confirm_search_draft(
    db: AsyncSession,
    draft_id: str,
    owner_user_id: int,
    expected_condition_revision: int,
    idempotency_key: str,
) -> SearchSnapshot:
    """用户确认全部 hard 条件且解决 conflicts 后才创建快照与候选查询任务。

    未确认（仍 ``awaiting_confirmation`` 且无已确认条件）或非确认状态草稿 →
    ``SearchDraftNotConfirmed``；编译失败不创建候选任务；同 key 同 payload 回放
    既有任务与快照。不 commit。
    """
    request_hash = _hash_confirm_request(draft_id, int(expected_condition_revision))
    existing_task = await _find_write_task(
        db, owner_user_id, SEARCH_EXECUTE_TASK_TYPE, idempotency_key
    )
    if existing_task is not None:
        _replay_or_conflict(existing_task, request_hash)
        snapshot = await _find_snapshot_row_by_draft(db, draft_id)
        if snapshot is not None:
            return SearchSnapshot(
                snapshot_id=str(snapshot["snapshot_id"]),
                task_id=existing_task.task_id,
                status=existing_task.status.value,
                condition_schema_version=str(
                    snapshot.get("condition_schema_version") or SEARCH_SCHEMA_VERSION
                ),
                expires_at=snapshot.get("expires_at"),
                replayed=True,
            )

    draft = await _load_draft_row(db, draft_id, for_update=True)
    if draft is None or int(draft["user_id"]) != owner_user_id:
        raise SearchDraftNotFound()
    if _is_expired(draft.get("expires_at")):
        await _update_draft_status(db, draft_id, SearchDraftStatus.EXPIRED.value)
        # 固化 expired 状态为独立短事务，避免后续 raise 导致回滚。
        await db.commit()
        raise SearchDraftNotConfirmed("草稿已过期")
    if str(draft["status"]) == SearchDraftStatus.CONFIRMED.value:
        snapshot = await _find_snapshot_row_by_draft(db, draft_id)
        if snapshot is not None:
            return SearchSnapshot(
                snapshot_id=str(snapshot["snapshot_id"]),
                task_id=existing_task.task_id if existing_task else "",
                status=existing_task.status.value if existing_task else "queued",
                condition_schema_version=str(
                    snapshot.get("condition_schema_version") or SEARCH_SCHEMA_VERSION
                ),
                expires_at=snapshot.get("expires_at"),
                replayed=True,
            )
        raise SearchDraftNotConfirmed("草稿已确认但快照缺失")
    if str(draft["status"]) != SearchDraftStatus.AWAITING_CONFIRMATION.value:
        raise SearchDraftNotConfirmed("草稿未处于待确认状态")
    if int(draft.get("condition_revision") or 0) != int(expected_condition_revision):
        raise DraftVersionConflict()

    condition_rows = await _load_condition_rows(db, draft_id)
    condition_objects = [_condition_from_row(row) for row in condition_rows]
    compiled = compile_search_conditions(condition_objects)
    if compiled.conflicts:
        raise SearchDraftNotConfirmed("存在未解决的区间冲突")

    active_hard = [
        condition
        for condition in condition_objects
        if condition.field_key in FIELD_RULES
        and FIELD_RULES[condition.field_key]["kind"] == "hard"
        and condition.user_action != SearchConditionUserAction.REMOVED
    ]
    missing_hard = [
        condition.field_key
        for condition in active_hard
        if condition.user_action != SearchConditionUserAction.CONFIRMED
    ]
    if missing_hard:
        raise SearchDraftNotConfirmed(
            f"存在未确认的硬条件: {', '.join(sorted(set(missing_hard)))}"
        )
    if not any(
        condition.user_action == SearchConditionUserAction.CONFIRMED
        for condition in condition_objects
        if condition.field_key in FIELD_RULES
    ):
        raise SearchDraftNotConfirmed("没有可执行的已确认条件")

    consent_snapshot = _consent_snapshot(await _load_active_consent(
        db, owner_user_id, SEARCH_CONSENT_SCOPE
    ))
    revision = await _load_revision_vector(db, owner_user_id)
    policy_revision = str(draft.get("policy_revision") or SEARCH_POLICY_REVISION)
    snapshot_id = uuid.uuid4().hex
    snapshot_hash = _snapshot_hash(condition_objects, policy_revision)
    expires_at = _now_utc() + timedelta(hours=settings.ai_search_draft_expire_hours)
    await db.execute(
        text(
            "INSERT INTO ai_search_snapshot "
            "(snapshot_id, user_id, draft_id, snapshot_hash, status, "
            " condition_schema_version, policy_revision, consent_snapshot_json, "
            " source_revision_json, result_total, degraded, expires_at, "
            " invalidated_at, created_at) "
            "VALUES (:snapshot_id, :user_id, :draft_id, :snapshot_hash, 'completed', "
            " :condition_schema_version, :policy_revision, :consent_snapshot_json, "
            " :source_revision_json, 0, 0, :expires_at, NULL, UTC_TIMESTAMP())"
        ),
        {
            "snapshot_id": snapshot_id,
            "user_id": owner_user_id,
            "draft_id": draft_id,
            "snapshot_hash": snapshot_hash,
            "condition_schema_version": SEARCH_SCHEMA_VERSION,
            "policy_revision": policy_revision,
            "consent_snapshot_json": json.dumps(consent_snapshot, ensure_ascii=False),
            "source_revision_json": json.dumps(revision.as_dict(), ensure_ascii=False),
            "expires_at": expires_at,
        },
    )
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=SEARCH_EXECUTE_TASK_TYPE,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        revisions=revision,
        consent=consent_snapshot,
    )
    await db.execute(
        text(
            "UPDATE ai_task SET payload_summary = :payload_summary, "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {
            "payload_summary": json.dumps(
                {"snapshot_id": snapshot_id, "draft_id": draft_id},
                ensure_ascii=False,
            ),
            "task_id": task.task_id,
        },
    )
    await _update_draft_status(db, draft_id, SearchDraftStatus.CONFIRMED.value)
    await db.flush()
    return SearchSnapshot(
        snapshot_id=snapshot_id,
        task_id=task.task_id,
        status=task.status.value,
        condition_schema_version=SEARCH_SCHEMA_VERSION,
        expires_at=expires_at,
    )


# ----------------------------------------------------------------------
# 候选查询构造与执行（复用 CandidateQueryService / CandidateVisibilityService）
# ----------------------------------------------------------------------

candidate_query_service = CandidateQueryService(secret_key=settings.secret_key)
candidate_visibility_service = CandidateVisibilityService()


def _hard_filter_clauses(
    filters: DiscoveryFilters,
    params: dict[str, Any],
    hard_memberships: tuple[tuple[str, tuple[Any, ...]], ...] = (),
) -> list[str]:
    """把编译后的 hard 筛选映射为参数化 SQL（与 discovery._filter_sql 同源口径）。

    参数全部来自服务器侧编译结果，模型输出永远不能成为 SQL 文本。
    """
    clauses: list[str] = []
    if filters.age_min is not None:
        clauses.append(
            f"u.birthday <= DATE_SUB(CURDATE(), INTERVAL {int(filters.age_min)} YEAR)"
        )
    if filters.age_max is not None:
        clauses.append(
            f"u.birthday >= DATE_SUB(CURDATE(), INTERVAL {int(filters.age_max) + 1} YEAR)"
        )
    if filters.city_code:
        clauses.append("p.residence_city_code = :filter_city_code")
        params["filter_city_code"] = filters.city_code
    if filters.marriage_status:
        clauses.append("u.is_married = :filter_marriage")
        params["filter_marriage"] = int(filters.marriage_status)
    if filters.education_min:
        clauses.append("p.education_level >= :filter_education")
        params["filter_education"] = int(filters.education_min)
    if filters.height_min:
        clauses.append("p.height >= :filter_height_min")
        params["filter_height_min"] = int(filters.height_min)
    if filters.height_max:
        clauses.append("p.height <= :filter_height_max")
        params["filter_height_max"] = int(filters.height_max)
    if filters.income_min is not None:
        clauses.append("p.income >= :filter_income_min")
        params["filter_income_min"] = float(filters.income_min)
    if filters.income_max is not None:
        clauses.append("p.income <= :filter_income_max")
        params["filter_income_max"] = float(filters.income_max)
    for index, (field_key, values) in enumerate(hard_memberships):
        if field_key == "city_code":
            names = []
            for value_index, value in enumerate(values):
                name = f"filter_city_code_{index}_{value_index}"
                names.append(f":{name}")
                params[name] = value
            clauses.append(f"p.residence_city_code IN ({', '.join(names)})")
        elif field_key == "marriage_status":
            names = []
            for value_index, value in enumerate(values):
                name = f"filter_marriage_{index}_{value_index}"
                names.append(f":{name}")
                params[name] = int(value)
            clauses.append(f"u.is_married IN ({', '.join(names)})")
        else:
            raise SearchInputInvalid(f"hard 字段 {field_key} 缺少静态映射")
    return clauses


def build_search_query_snapshot(
    *,
    viewer_id: int,
    viewer: dict[str, Any],
    viewer_is_vip: bool,
    compiled: CompiledSearch,
    page: int = 1,
) -> CandidateQuerySnapshot:
    """用 CompiledSearch 构造 CandidateQueryService 的候选查询快照。

    复用 ``CandidateVisibilityService.predicate``（SEARCH 场景）与
    ``CARD_SELECT/CARD_FROM``；soft 标签（interest_tags/lifestyle_tags）按字面
    JSON_CONTAINS 逐条编译；fingerprint 绑定 cursor 与查询身份。不包含任何
    模型生成的 SQL。
    """
    visibility = candidate_visibility_service.predicate(
        ViewerContext(
            user_id=viewer_id,
            realname_status=int(viewer.get("realname_status") or 0),
            is_vip=viewer_is_vip,
        ),
        VisibilityScene.SEARCH,
    )
    params: dict[str, Any] = {"viewer_id": viewer_id, **visibility.params}
    clauses = [visibility.clause]
    clauses.extend(
        _hard_filter_clauses(
            compiled.filters, params, compiled.hard_memberships
        )
    )
    filter_facts = compiled.filters.model_dump(mode="json")
    for key in ("cursor", "page", "page_size"):
        filter_facts.pop(key, None)
    query_fingerprint = build_query_fingerprint(
        {
            "viewer_id": viewer_id,
            "viewer_realname_status": int(viewer.get("realname_status") or 0),
            "viewer_is_vip": viewer_is_vip,
            "scene": VisibilityScene.SEARCH.value,
            "filters": filter_facts,
            "soft_terms": compiled.soft_terms,
            "hard_memberships": compiled.hard_memberships,
            "policy_revision": visibility.policy_revision,
            "sort_version": SORT_VERSION,
        }
    )
    return CandidateQuerySnapshot(
        select_sql=CARD_SELECT + CARD_FROM,
        count_sql="SELECT COUNT(DISTINCT u.id)" + CARD_FROM,
        where_sql=" AND ".join(clauses),
        params=params,
        query_fingerprint=query_fingerprint,
        page=page,
    )


async def _load_viewer_context(db: AsyncSession, user_id: int) -> dict[str, Any]:
    result = await db.execute(
        text(
            "SELECT u.gender, u.birthday, "
            "COALESCE(c.score, 0) AS completion_score, "
            "COALESCE(ua.realname_status, 0) AS realname_status, "
            "COALESCE(pr.only_vip_can_see_detail, 0) AS only_vip_can_see_detail "
            "FROM users u "
            "LEFT JOIN user_profile_completion c ON c.user_id = u.id "
            "LEFT JOIN user_auth ua ON ua.user_id = u.id "
            "LEFT JOIN user_privacy pr ON pr.user_id = u.id "
            "WHERE u.id = :user_id"
        ),
        {"user_id": user_id},
    )
    row = await _first_row(result)
    if row is None:
        raise SearchDraftNotFound()
    return dict(row)


async def _is_vip(db: AsyncSession, user_id: int) -> bool:
    result = await db.execute(
        text(
            "SELECT EXISTS (SELECT 1 FROM user_membership "
            "WHERE user_id = :user_id AND status = 1 "
            "AND (start_at IS NULL OR start_at <= UTC_TIMESTAMP()) "
            "AND (end_at IS NULL OR end_at > UTC_TIMESTAMP()))"
        ),
        {"user_id": user_id},
    )
    return bool(result.scalar())


async def _load_projections(
    db: AsyncSession, user_ids: list[int]
) -> dict[int, dict[str, Any]]:
    if not user_ids:
        return {}
    placeholders = ", ".join(f":uid{i}" for i in range(len(user_ids)))
    result = await db.execute(
        text(
            "SELECT id, subject_user_id, source_hash, fields_json, "
            "profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision, source_revision_json, "
            "consent_snapshot_json, status, expires_at "
            "FROM ai_feature_projection "
            f"WHERE subject_user_id IN ({placeholders}) "
            "AND projection_kind = 'personal_searchable' AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP()) "
            "ORDER BY id DESC"
        ),
        {f"uid{i}": uid for i, uid in enumerate(user_ids)},
    )
    projections: dict[int, dict[str, Any]] = {}
    for row in result.mappings().all():
        subject_user_id = int(row["subject_user_id"])
        if subject_user_id in projections:
            continue
        source_revision = _maybe_json(row.get("source_revision_json"))
        if not isinstance(source_revision, dict) and row.get("profile_revision") is not None:
            source_revision = {
                "profile": int(row.get("profile_revision") or 0),
                "preference": int(row.get("preference_revision") or 0),
                "privacy": int(row.get("privacy_revision") or 0),
                "relationship": int(row.get("relationship_revision") or 0),
                "policy": int(row.get("policy_revision") or 0),
            }
        projections[subject_user_id] = {
            "id": int(row.get("id") or 0) or None,
            "source_hash": str(row.get("source_hash") or "") or None,
            "fields": _maybe_json(row.get("fields_json")) or {},
            "profile_revision": int(row.get("profile_revision") or 0),
            "source_revision": source_revision,
            "consent_snapshot": _maybe_json(row.get("consent_snapshot_json")),
            "expires_at": row.get("expires_at"),
        }
    return projections


def _soft_matches(field_key: str, expected: Any, actual: Any) -> bool:
    expected_text = str(expected)
    if field_key in _TAG_SOFT_FIELDS:
        candidates: list[str] = []
        for value in (actual if isinstance(actual, list) else [actual]):
            candidates.append(str(value))
        return expected_text in candidates
    return str(actual) == expected_text


def _evidence_for_row(
    row: dict[str, Any],
    condition_objects: list[SearchCondition],
    compiled: CompiledSearch,
    projection: dict[str, Any] | None,
) -> SearchEvidence:
    hard_keys = sorted(
        {
            condition.field_key
            for condition in condition_objects
            if condition.field_key in FIELD_RULES
            and FIELD_RULES[condition.field_key]["kind"] == "hard"
            and condition.user_action == SearchConditionUserAction.CONFIRMED
        }
    )
    matched = list(hard_keys)
    reason_codes = ["HARD_CONDITION_MATCH"] if hard_keys else []
    unknown: list[str] = []
    fields = (projection or {}).get("fields") or {}
    profile_revision = int((projection or {}).get("profile_revision") or 0)
    soft_match_count = 0
    for field_key, value in compiled.soft_terms:
        field_value = fields.get(field_key)
        if field_value is None:
            unknown.append(field_key)
            reason_codes.append("SOFT_FIELD_UNKNOWN")
            continue
        if _soft_matches(field_key, value, field_value):
            matched.append(field_key)
            soft_match_count += 1
            reason_codes.append("SOFT_FIELD_MATCH")
        else:
            reason_codes.append("SOFT_FIELD_NO_MATCH")
    return SearchEvidence(
        matched_condition_count=len(matched),
        matched_conditions=matched,
        unknown_conditions=unknown,
        reason_codes=reason_codes,
        profile_revision=profile_revision,
        projection_id=(projection or {}).get("id"),
        source_hash=(projection or {}).get("source_hash"),
        consent_snapshot=(projection or {}).get("consent_snapshot"),
        source_revision=(projection or {}).get("source_revision"),
        soft_match_count=soft_match_count,
    )


def _result_card(row: dict[str, Any], *, viewer_is_vip: bool = False) -> dict[str, Any]:
    """只返回当前可见卡片字段；detail_locked 隐私字段不进入结果卡片。"""
    from datetime import date

    from app.services.profile import _calculate_age

    birthday = row.get("birthday")
    if isinstance(birthday, str):
        try:
            birthday = date.fromisoformat(birthday)
        except ValueError:
            birthday = None
    detail_locked = bool(row.get("only_vip_can_see_detail")) and not viewer_is_vip
    return {
        "user_id": int(row["user_id"]),
        "nickname": row.get("nickname"),
        "avatar": row.get("avatar"),
        "age": _calculate_age(birthday) if birthday else None,
        "city_code": row.get("residence_city_code") if not detail_locked else None,
        "education_level": (
            row.get("education_level")
            if not detail_locked and not row.get("hide_school")
            else None
        ),
        "height": row.get("height") if not detail_locked else None,
        "occupation": (
            row.get("occupation")
            if not detail_locked and not row.get("hide_company")
            else None
        ),
        "income": (
            float(row["income"])
            if row.get("income") is not None and not detail_locked
            else None
        ),
        "is_married": row.get("is_married") if not detail_locked else None,
        "interest_tags": (
            (_maybe_json(row.get("interest_tags")) or [])[:5]
            if not detail_locked
            else []
        ),
        "detail_locked": detail_locked,
    }


async def _upsert_result_row(
    db: AsyncSession,
    snapshot_id: str,
    target_user_id: int,
    rank_position: int,
    evidence: SearchEvidence,
    result_expires_at: datetime,
    *,
    generation: int = _SEARCH_RESULT_DEFAULT_GENERATION,
) -> None:
    # Task8 Step2：upsert 时携带 generation。ON DUPLICATE KEY UPDATE 也更新
    # generation，保证同一 (snapshot_id, target_user_id) 的行在新 generation 下
    # 被正确刷新；旧 generation 的行由 materialize 成功后统一清理。
    await db.execute(
        text(
            "INSERT INTO ai_search_result "
            "(snapshot_id, target_user_id, rank_position, matched_condition_count, "
            " matched_conditions, unknown_conditions, reason_codes, profile_revision, "
            " projection_id, source_hash, consent_snapshot_json, source_revision_json, "
            " result_expires_at, stale, generation, created_at) "
            "VALUES (:snapshot_id, :target_user_id, :rank_position, "
            " :matched_condition_count, :matched_conditions, :unknown_conditions, "
            " :reason_codes, :profile_revision, :projection_id, :source_hash, "
            " :consent_snapshot_json, :source_revision_json, :result_expires_at, "
            " 0, :generation, UTC_TIMESTAMP()) "
            "ON DUPLICATE KEY UPDATE "
            " rank_position = VALUES(rank_position), "
            " matched_condition_count = VALUES(matched_condition_count), "
            " matched_conditions = VALUES(matched_conditions), "
            " unknown_conditions = VALUES(unknown_conditions), "
            " reason_codes = VALUES(reason_codes), "
            " profile_revision = VALUES(profile_revision), "
            " projection_id = VALUES(projection_id), "
            " source_hash = VALUES(source_hash), "
            " consent_snapshot_json = VALUES(consent_snapshot_json), "
            " source_revision_json = VALUES(source_revision_json), "
            " result_expires_at = VALUES(result_expires_at), "
            " stale = 0, "
            " generation = VALUES(generation)"
        ),
        {
            "snapshot_id": snapshot_id,
            "target_user_id": target_user_id,
            "rank_position": rank_position,
            "matched_condition_count": evidence.matched_condition_count,
            "matched_conditions": json.dumps(
                evidence.matched_conditions, ensure_ascii=False
            ),
            "unknown_conditions": json.dumps(
                evidence.unknown_conditions, ensure_ascii=False
            ),
            "reason_codes": json.dumps(evidence.reason_codes, ensure_ascii=False),
            "profile_revision": evidence.profile_revision,
            "projection_id": evidence.projection_id,
            "source_hash": evidence.source_hash,
            "consent_snapshot_json": json.dumps(
                evidence.consent_snapshot, ensure_ascii=False
            ) if evidence.consent_snapshot is not None else None,
            "source_revision_json": json.dumps(
                evidence.source_revision, ensure_ascii=False
            ) if evidence.source_revision is not None else None,
            "result_expires_at": result_expires_at,
            "generation": generation,
        },
    )


def _encode_materialized_cursor(
    snapshot_id: str,
    rank_position: int,
    *,
    generation: int = _SEARCH_RESULT_DEFAULT_GENERATION,
    target_user_id: int = 0,
) -> str:
    """Task8 Step2：cursor 编码 (generation, rank_position, target_user_id) 三元组。

    ``target_user_id`` 作为相同 rank 的稳定 tie-break 锚点，保证多页翻页无重复/漏项。
    ``generation`` 用于在 active generation 切换后让旧 cursor 失效。
    """
    payload = json.dumps(
        {
            "version": _MATERIALIZED_CURSOR_VERSION,
            "snapshot_id": snapshot_id,
            "rank_position": int(rank_position),
            "generation": int(generation),
            "target_user_id": int(target_user_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(
        settings.secret_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"


def _decode_materialized_cursor(
    snapshot_id: str,
    token: str,
    *,
    active_generation: int | None = None,
) -> tuple[int, int]:
    """Task8 Step2：解码 cursor，返回 (rank_position, target_user_id)。

    ``active_generation`` 不为 None 时，校验 cursor 内的 generation 必须匹配当前
    active generation；不匹配抛 ``InvalidCandidateCursor``（让前端重新拉第一页）。

    向后兼容：旧 v1 cursor（只含 snapshot_id + rank_position，无 generation）在
    ``active_generation is None`` 或 ``active_generation == 1`` 时仍可解码，返回
    ``(rank_position, 0)``。若 active generation 已切换到 >1，旧 v1 cursor 失效。
    """
    if not token or len(token) > 512 or "." not in token:
        raise InvalidCandidateCursor("invalid materialized search cursor")
    encoded, signature = token.split(".", 1)
    try:
        expected = hmac.new(
            settings.secret_key.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        padding = "=" * (-len(signature) % 4)
        actual = base64.urlsafe_b64decode((signature + padding).encode("ascii"))
        if not hmac.compare_digest(expected, actual):
            raise InvalidCandidateCursor("invalid materialized search cursor")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        )
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidCandidateCursor("invalid materialized search cursor") from exc

    version = payload.get("version")
    if version not in {_MATERIALIZED_CURSOR_VERSION, _MATERIALIZED_CURSOR_VERSION_V1}:
        raise InvalidCandidateCursor("invalid materialized search cursor")
    if payload.get("snapshot_id") != snapshot_id:
        raise InvalidCandidateCursor("invalid materialized search cursor")
    if not isinstance(payload.get("rank_position"), int) or payload["rank_position"] < 0:
        raise InvalidCandidateCursor("invalid materialized search cursor")

    rank_position = int(payload["rank_position"])

    # v1 旧 cursor：无 generation/target_user_id 字段
    if version == _MATERIALIZED_CURSOR_VERSION_V1:
        # 旧 cursor 在 active generation >1 时失效（generation 已切换）
        if active_generation is not None and active_generation > _SEARCH_RESULT_DEFAULT_GENERATION:
            raise InvalidCandidateCursor("stale cursor: generation switched")
        return rank_position, 0

    # v2 新 cursor：校验 generation
    cursor_generation = payload.get("generation")
    if not isinstance(cursor_generation, int) or cursor_generation < 1:
        raise InvalidCandidateCursor("invalid materialized search cursor")
    if active_generation is not None and cursor_generation != active_generation:
        raise InvalidCandidateCursor("stale cursor: generation mismatch")
    target_user_id = payload.get("target_user_id")
    if not isinstance(target_user_id, int) or target_user_id < 0:
        raise InvalidCandidateCursor("invalid materialized search cursor")
    return rank_position, int(target_user_id)


async def _load_active_generation(
    db: AsyncSession, snapshot_id: str
) -> int:
    """Task8 Step2：派生 snapshot 的 active generation。

    最小加法：不加 DDL 到 snapshot 表，而是从 ``ai_search_result`` 表的
    ``MAX(generation) WHERE stale=0`` 派生 active generation。无结果行时返回默认 1。
    """
    result = await db.execute(
        text(
            "SELECT MAX(generation) AS active_generation FROM ai_search_result "
            "WHERE snapshot_id = :snapshot_id AND stale = 0"
        ),
        {"snapshot_id": snapshot_id},
    )
    row = await _first_row(result)
    if row is None or row.get("active_generation") is None:
        return _SEARCH_RESULT_DEFAULT_GENERATION
    return int(row["active_generation"])


async def _load_materialized_result_rows(
    db: AsyncSession,
    snapshot_id: str,
    after_rank: int,
    limit: int,
    *,
    active_generation: int | None = None,
) -> list[dict[str, Any]]:
    # Task8 Step2：按 active generation 过滤，并用 target_user_id 做相同 rank 的
    # 稳定 tie-break。active_generation 为 None 时退化为旧行为（兼容旧调用点）。
    generation_clause = ""
    params: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "after_rank": int(after_rank),
        "limit": int(limit),
    }
    if active_generation is not None:
        generation_clause = "AND generation = :active_generation "
        params["active_generation"] = int(active_generation)
    result = await db.execute(
        text(
            "SELECT target_user_id, projection_id, source_hash, rank_position, "
            "matched_condition_count, matched_conditions, unknown_conditions, "
            "reason_codes, profile_revision, consent_snapshot_json, "
            "source_revision_json, result_expires_at, stale, generation "
            f"FROM ai_search_result WHERE snapshot_id = :snapshot_id "
            f"AND stale = 0 {generation_clause}"
            "AND rank_position > :after_rank "
            "ORDER BY rank_position ASC, target_user_id ASC LIMIT :limit"
        ),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def _load_materialized_candidate_cards(
    db: AsyncSession, viewer_id: int, user_ids: list[int]
) -> dict[int, dict[str, Any]]:
    if not user_ids:
        return {}
    placeholders = ", ".join(f":card_uid{i}" for i in range(len(user_ids)))
    result = await db.execute(
        text(CARD_SELECT + CARD_FROM + f" WHERE u.id IN ({placeholders})"),
        {
            "viewer_id": viewer_id,
            "candidate_query_limit": len(user_ids),
            **{f"card_uid{i}": uid for i, uid in enumerate(user_ids)},
        },
    )
    return {int(row["user_id"]): dict(row) for row in result.mappings().all()}


async def _candidate_projection_is_current(
    db: AsyncSession, candidate_id: int, stored: dict[str, Any]
) -> bool:
    projection = (await _load_projections(db, [candidate_id])).get(candidate_id)
    if projection is None:
        return False
    stored_projection_id = stored.get("projection_id")
    projection_id = projection.get("id")
    stored_source_hash = str(stored.get("source_hash") or "")
    projection_source_hash = str(projection.get("source_hash") or "")
    if (
        stored_projection_id is None
        or projection_id is None
        or int(stored_projection_id or 0) != int(projection_id or 0)
        or not stored_source_hash
        or not projection_source_hash
        or stored_source_hash != projection_source_hash
    ):
        return False
    stored_revision = _maybe_json(
        stored.get("source_revision_json") or stored.get("source_revision")
    )
    projection_revision = projection.get("source_revision")
    required_revision_keys = {
        "profile", "preference", "privacy", "relationship", "policy"
    }
    if (
        not isinstance(stored_revision, dict)
        or not isinstance(projection_revision, dict)
        or set(stored_revision) != required_revision_keys
        or set(projection_revision) != required_revision_keys
        or stored_revision != projection_revision
    ):
        return False
    current = await _load_revision_vector(db, candidate_id)
    if current.as_dict() != projection_revision:
        return False
    stored_consent = _maybe_json(
        stored.get("consent_snapshot_json") or stored.get("consent_snapshot")
    )
    projection_consent = projection.get("consent_snapshot")
    if (
        not isinstance(stored_consent, dict)
        or not isinstance(projection_consent, dict)
        or stored_consent != projection_consent
        or str(stored_consent.get("scope") or "") != "profile_text_extract"
        or not stored_consent.get("version")
        or not stored_consent.get("policy_revision")
        or not stored_consent.get("granted_at")
    ):
        return False
    return await _active_consent_matches(
        db, candidate_id, stored_consent, expected_scope="profile_text_extract"
    )


async def _active_consent_matches(
    db: AsyncSession,
    user_id: int,
    snapshot: dict[str, Any],
    *,
    expected_scope: str | None = None,
) -> bool:
    scope = str(snapshot.get("scope") or "")
    version = str(snapshot.get("version") or "")
    if not scope or not version or (expected_scope and scope != expected_scope):
        return False
    row = await _first_row(
        await db.execute(
            text(
                "SELECT version, policy_revision, granted_at FROM ai_consent_grant "
                "WHERE user_id = :user_id AND scope = :scope AND version = :version "
                "AND revoked_at IS NULL ORDER BY granted_at DESC LIMIT 1"
            ),
            {"user_id": user_id, "scope": scope, "version": version},
        )
    )
    if row is None:
        return False
    if str(row.get("policy_revision") or "") != str(snapshot.get("policy_revision") or ""):
        return False
    granted_at = row.get("granted_at")
    current_granted_at = (
        granted_at.isoformat() if hasattr(granted_at, "isoformat") else str(granted_at or "")
    )
    return current_granted_at == str(snapshot.get("granted_at") or "")


async def _set_search_task_stage(
    db: AsyncSession, task_id: str, stage: str, progress: int | None = None
) -> None:
    await db.execute(
        text(
            "UPDATE ai_task SET stage = :stage, "
            "progress_percent = COALESCE(:progress, progress_percent), "
            "updated_at = UTC_TIMESTAMP() WHERE task_id = :task_id"
        ),
        {"task_id": task_id, "stage": stage, "progress": progress},
    )


async def materialize_search_snapshot(
    db: AsyncSession,
    snapshot_id: str,
    owner_user_id: int,
    *,
    task_id: str | None = None,
) -> SearchResultPageRead:
    """Worker-only path: scan the full hard-filtered baseline and materialize top 200."""
    snapshot = await _load_snapshot_row(db, snapshot_id)
    if snapshot is None or int(snapshot["user_id"]) != owner_user_id:
        raise SearchSnapshotNotFound()
    if snapshot.get("invalidated_at") is not None:
        raise SearchSnapshotNotFound()
    if _is_expired(snapshot.get("expires_at")):
        return SearchResultPageRead(snapshot_id=snapshot_id, status="stale")
    if task_id:
        await _set_search_task_stage(db, task_id, "validating", progress=10)
    draft_id = str(snapshot.get("draft_id") or "")
    condition_objects = [
        _condition_from_row(row) for row in await _load_condition_rows(db, draft_id)
    ]
    compiled = compile_search_conditions(condition_objects)
    if compiled.conflicts:
        raise SearchInputInvalid("AI_INPUT_INVALID")
    if task_id:
        await _set_search_task_stage(db, task_id, "filtering", progress=30)
    viewer = await _load_viewer_context(db, owner_user_id)
    viewer_is_vip = await _is_vip(db, owner_user_id)
    query_snapshot = build_search_query_snapshot(
        viewer_id=owner_user_id,
        viewer=viewer,
        viewer_is_vip=viewer_is_vip,
        compiled=compiled,
        page=1,
    )
    baseline_rows: list[dict[str, Any]] = []
    candidate_cursor: str | None = None
    while True:
        page = await candidate_query_service.fetch_page(
            db,
            query_snapshot,
            cursor=candidate_cursor,
            page_size=SEARCH_MATERIALIZATION_LIMIT,
        )
        baseline_rows.extend(page.items)
        if not page.next_cursor:
            break
        candidate_cursor = page.next_cursor
    projections = await _load_projections(
        db, [int(row["user_id"]) for row in baseline_rows]
    )
    visible: list[tuple[int, dict[str, Any], SearchEvidence]] = []
    for baseline_index, row in enumerate(baseline_rows):
        candidate_id = int(row["user_id"])
        decision = await candidate_visibility_service.decide(
            db, owner_user_id, candidate_id, VisibilityScene.SEARCH
        )
        if not decision.allowed:
            continue
        if candidate_id not in projections:
            # Search consumes only the versioned personal_searchable boundary;
            # an unprojected candidate cannot safely enter a materialized set.
            continue
        projection = projections[candidate_id]
        projection_evidence = {
            "projection_id": projection.get("id"),
            "source_hash": projection.get("source_hash"),
            "source_revision_json": projection.get("source_revision"),
            "consent_snapshot_json": projection.get("consent_snapshot"),
        }
        if not await _candidate_projection_is_current(
            db, candidate_id, projection_evidence
        ):
            continue
        evidence = _evidence_for_row(
            row, condition_objects, compiled, projections.get(candidate_id)
        )
        visible.append((baseline_index, row, evidence))
    if task_id:
        await _set_search_task_stage(db, task_id, "ranking", progress=85)
    visible.sort(
        key=lambda item: (
            -item[2].soft_match_count,
            item[0],
            -int(item[1]["user_id"]),
        )
    )
    materialized = visible[:SEARCH_MATERIALIZATION_LIMIT]
    result_expires_at = _now_utc() + timedelta(minutes=SEARCH_RESULT_TTL_MINUTES)
    # Task8 Step2：atomic generation。先读当前 active generation，写新 generation
    # = active + 1 的行；成功后把旧 generation 的行（不在新结果中的候选）标记
    # stale 或删除，原子切换 active generation。同一 (snapshot_id, target_user_id)
    # 的行由 upsert 覆盖为新 generation；旧候选（不再在新结果中）由
    # ``DELETE WHERE generation < new_generation`` 清理，保证「候选集合变化时
    # 旧候选为 0」。
    active_generation = await _load_active_generation(db, snapshot_id)
    new_generation = active_generation + 1
    # 先删除旧 generation 中 rank_position > limit 的溢出行
    await db.execute(
        text("DELETE FROM ai_search_result WHERE snapshot_id = :snapshot_id AND rank_position > :limit"),
        {"snapshot_id": snapshot_id, "limit": SEARCH_MATERIALIZATION_LIMIT},
    )
    for rank_position, (_, row, evidence) in enumerate(materialized, start=1):
        await _upsert_result_row(
            db,
            snapshot_id,
            int(row["user_id"]),
            rank_position,
            evidence,
            result_expires_at,
            generation=new_generation,
        )
    # 原子切换 active generation：删除旧 generation 的所有行（不在新结果中的候选）
    # 这保证「同 snapshot 第一次 200 候选、第二次候选集合变化时旧候选为 0」。
    await db.execute(
        text(
            "DELETE FROM ai_search_result "
            "WHERE snapshot_id = :snapshot_id AND generation < :new_generation"
        ),
        {"snapshot_id": snapshot_id, "new_generation": new_generation},
    )
    total = len(visible)
    degraded = total > SEARCH_MATERIALIZATION_LIMIT
    status_value = "partial" if degraded else ("empty" if total == 0 else "completed")
    await db.execute(
        text(
            "UPDATE ai_search_snapshot SET status = :status, result_total = :result_total, "
            "degraded = :degraded WHERE snapshot_id = :snapshot_id"
        ),
        {
            "snapshot_id": snapshot_id,
            "status": status_value,
            "result_total": total,
            "degraded": int(degraded),
        },
    )
    if task_id:
        await _set_search_task_stage(
            db, task_id, status_value,
            progress=100 if status_value == "completed" else None,
        )
    items = [
        SearchResultItemRead(
            user_id=int(row["user_id"]),
            card=_result_card(row, viewer_is_vip=viewer_is_vip),
            matched_condition_count=evidence.matched_condition_count,
            matched_conditions=evidence.matched_conditions,
            unknown_conditions=evidence.unknown_conditions,
            reason_codes=evidence.reason_codes,
            profile_revision=evidence.profile_revision,
            result_expires_at=result_expires_at,
        )
        for _, row, evidence in materialized[:SEARCH_PAGE_SIZE_DEFAULT]
    ]
    # Task8 Step2：next_cursor 编码当前 new generation 和首页最后一行的
    # target_user_id 作为稳定 tie-break 锚点。只有当结果数 > 页大小且
    # materialized 列表足够长时才生成 cursor。
    first_page_last_uid = (
        int(materialized[SEARCH_PAGE_SIZE_DEFAULT - 1][1]["user_id"])
        if len(materialized) > SEARCH_PAGE_SIZE_DEFAULT
        else 0
    )
    return SearchResultPageRead(
        snapshot_id=snapshot_id,
        status=status_value,
        items=items,
        next_cursor=(
            _encode_materialized_cursor(
                snapshot_id,
                SEARCH_PAGE_SIZE_DEFAULT,
                generation=new_generation,
                target_user_id=first_page_last_uid,
            )
            if total > SEARCH_PAGE_SIZE_DEFAULT
            and len(materialized) > SEARCH_PAGE_SIZE_DEFAULT
            else None
        ),
        total=total,
        total_is_estimate=False,
        degraded=degraded,
    )


async def read_materialized_search_results(
    db: AsyncSession,
    snapshot_id: str,
    owner_user_id: int,
    cursor: str | None,
    page_size: int,
) -> SearchResultPageRead:
    """Pure read path: never runs CandidateQuery or writes result rows."""
    snapshot = await _load_snapshot_row(db, snapshot_id)
    if snapshot is None or int(snapshot["user_id"]) != owner_user_id:
        raise SearchSnapshotNotFound()
    if snapshot.get("invalidated_at") is not None:
        raise SearchSnapshotNotFound()
    # Task8 Step2：读 active generation，用于 cursor generation 校验。
    active_generation = await _load_active_generation(db, snapshot_id)
    # Validate a supplied cursor before returning a stale page.  A malformed or
    # cross-snapshot token is still a client error even when the snapshot is no
    # longer readable.  旧 v1 cursor 在 active generation >1 时失效。
    after_rank, cursor_target_user_id = (
        _decode_materialized_cursor(
            snapshot_id, cursor, active_generation=active_generation
        )
        if cursor
        else (0, 0)
    )
    if _is_expired(snapshot.get("expires_at")):
        return SearchResultPageRead(snapshot_id=snapshot_id, status="stale")
    owner_source = _maybe_json(snapshot.get("source_revision_json")) or {}
    if owner_source:
        if (await _load_revision_vector(db, owner_user_id)).as_dict() != owner_source:
            return SearchResultPageRead(snapshot_id=snapshot_id, status="stale")
    owner_consent = _maybe_json(snapshot.get("consent_snapshot_json")) or {}
    if owner_consent and not await _active_consent_matches(db, owner_user_id, owner_consent):
        return SearchResultPageRead(snapshot_id=snapshot_id, status="stale")
    stored_rows = await _load_materialized_result_rows(
        db, snapshot_id, after_rank, page_size + 1,
        active_generation=active_generation,
    )
    has_more = len(stored_rows) > page_size
    stored_rows = stored_rows[:page_size]
    accepted: list[dict[str, Any]] = []
    for stored in stored_rows:
        candidate_id = int(stored["target_user_id"])
        decision = await candidate_visibility_service.decide(
            db, owner_user_id, candidate_id, VisibilityScene.SEARCH
        )
        if not decision.allowed:
            continue
        if not await _candidate_projection_is_current(db, candidate_id, stored):
            continue
        accepted.append(stored)
    cards = await _load_materialized_candidate_cards(
        db, owner_user_id, [int(row["target_user_id"]) for row in accepted]
    )
    viewer_is_vip = await _is_vip(db, owner_user_id)
    items: list[SearchResultItemRead] = []
    for row in accepted:
        card_row = cards.get(int(row["target_user_id"]))
        if card_row is None:
            continue
        items.append(
            SearchResultItemRead(
                user_id=int(row["target_user_id"]),
                card=_result_card(card_row, viewer_is_vip=viewer_is_vip),
                matched_condition_count=int(row.get("matched_condition_count") or 0),
                matched_conditions=_maybe_json(row.get("matched_conditions")) or [],
                unknown_conditions=_maybe_json(row.get("unknown_conditions")) or [],
                reason_codes=_maybe_json(row.get("reason_codes")) or [],
                profile_revision=int(row.get("profile_revision") or 0),
                result_expires_at=row.get("result_expires_at"),
            )
        )
    status_value = str(snapshot.get("status") or "completed")
    # Task8 Step2：next_cursor 编码当前 active generation 和最后一行的 target_user_id
    # 作为稳定 tie-break 锚点。
    last_row = stored_rows[-1] if stored_rows else None
    return SearchResultPageRead(
        snapshot_id=snapshot_id,
        status=status_value,
        items=items,
        next_cursor=(
            _encode_materialized_cursor(
                snapshot_id,
                int(last_row["rank_position"]),
                generation=active_generation,
                target_user_id=int(last_row["target_user_id"]),
            )
            if has_more and last_row
            else None
        ),
        total=int(snapshot.get("result_total") or 0),
        total_is_estimate=False,
        degraded=bool(snapshot.get("degraded")),
    )


async def search_execute_handler(
    db: AsyncSession, task: AiTaskRecord, worker_id: str
) -> tuple[str, RevisionVector] | None:
    """``search_execute`` Worker handler：预执行快照并持久化首屏结果。"""
    payload = task.payload_summary or {}
    snapshot_id = payload.get("snapshot_id")
    if not snapshot_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    snapshot = await _load_snapshot_row(db, str(snapshot_id))
    if snapshot is None or int(snapshot["user_id"]) != task.owner_user_id:
        await fail_task(
            db, task.task_id, worker_id,
            error_code="AI_INPUT_INVALID", retryable=False,
        )
        return None
    try:
        # G2-C 门禁约束：handler 会话不得写 ai_task 行（stage 更新会在本会话
        # 持有任务行的 X 锁，使 complete_task 在独立 finalize 会话的 FOR UPDATE
        # 自锁超时）。因此不传 task_id——物化进度以 ai_search_snapshot.status
        # 为可观测通道，任务终态由 complete_task 门禁统一写入。
        await materialize_search_snapshot(
            db,
            str(snapshot_id),
            task.owner_user_id,
        )
    except (SearchSnapshotNotFound, SearchDraftNotConfirmed, SearchInputInvalid):
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
    return f"search-snapshot:{snapshot_id}", revisions


# ----------------------------------------------------------------------
# 建议标签 / 删除
# ----------------------------------------------------------------------


async def get_search_suggestions(
    db: AsyncSession, owner_user_id: int
) -> SearchSuggestionRead:
    """只读本人已确认且允许搜索的标签（interest_tags/lifestyle_tags）。

    数据源为 ``personal_searchable`` 特征投影（仅已确认字段）；无投影时返回
    空数组。
    """
    result = await db.execute(
        text(
            "SELECT subject_user_id, fields_json, status, expires_at "
            "FROM ai_feature_projection "
            "WHERE subject_user_id = :user_id "
            "AND projection_kind = 'personal_searchable' AND status = 'active' "
            "AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP()) "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"user_id": owner_user_id},
    )
    row = await _first_row(result)
    if row is None:
        return SearchSuggestionRead(items=[], page=CursorMeta())
    fields = _maybe_json(row.get("fields_json")) or {}
    tags: list[str] = []
    for key in ("interest_tags", "lifestyle_tags"):
        value = fields.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str) and item.strip() and item.strip() not in tags:
                tags.append(item.strip())
    return SearchSuggestionRead(items=tags, page=CursorMeta())


async def delete_search_snapshot(
    db: AsyncSession,
    snapshot_id: str,
    owner_user_id: int,
    idempotency_key: str,
) -> CleanupTask:
    """软删除快照：同步不可读 + 入队 cleanup 任务（202）。"""
    request_hash = _hash_delete_request(snapshot_id)
    snapshot = await _load_snapshot_row(db, snapshot_id)
    if snapshot is None or int(snapshot["user_id"]) != owner_user_id:
        raise SearchSnapshotNotFound()
    if snapshot.get("invalidated_at") is None:
        await db.execute(
            text(
                "UPDATE ai_search_snapshot SET invalidated_at = UTC_TIMESTAMP(), "
                "updated_at = UTC_TIMESTAMP() WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
    task = await enqueue_task(
        db=db,
        owner_user_id=owner_user_id,
        task_type=SEARCH_CLEANUP_TASK_TYPE,
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
                _cleanup_payload_for_snapshot(owner_user_id, snapshot_id),
                ensure_ascii=False,
            ),
            "task_id": task.task_id,
        },
    )
    return CleanupTask(
        task_id=task.task_id,
        status=task.status.value,
        subject="search",
    )


# ----------------------------------------------------------------------
# Worker handler 注册（本任务注册 search 相关 handler）
# ----------------------------------------------------------------------


def register_search_handlers() -> None:
    """把 ``search_parse`` / ``search_execute`` 注册进 AI Worker 的 TASK_HANDLERS。

    模块导入时自动注册（路由导入本模块即生效）；幂等，可在测试中重复调用。
    """
    from app.workers import ai_worker as worker_module

    worker_module.TASK_HANDLERS.setdefault(SEARCH_PARSE_TASK_TYPE, parse_search_draft)
    worker_module.TASK_HANDLERS.setdefault(
        SEARCH_EXECUTE_TASK_TYPE, search_execute_handler
    )


register_search_handlers()
