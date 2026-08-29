"""M03 AI 搜索 schemas: controlled AST conditions, drafts, snapshots and results.

The condition field/operator/kind/user_action allowlists are server-owned and
follow the unified plan §8.1.  Model output can never become SQL; it is always
compiled server-side into parameterised filters by later tasks.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.ai_common import AI_FIELD_ALLOWLIST, CursorMeta


class SearchConditionOperator(str, Enum):
    """Allowed operators; only the per-field subset in FIELD_OPERATOR_ALLOWLIST."""

    BETWEEN = "between"
    GTE = "gte"
    LTE = "lte"
    EQ = "eq"
    IN = "in"
    CONTAINS = "contains"


# 每条 allowlist 字段允许的 operator（统一方案 §8.1）。
FIELD_OPERATOR_ALLOWLIST: dict[str, frozenset[str]] = {
    "age": frozenset({"between", "gte", "lte"}),
    "city_code": frozenset({"eq", "in"}),
    "marriage_status": frozenset({"eq", "in"}),
    "education_level": frozenset({"gte"}),
    "height_cm": frozenset({"between", "gte", "lte"}),
    "income_band": frozenset({"between", "gte", "lte"}),
    "occupation_group": frozenset({"eq"}),
    "interest_tags": frozenset({"contains"}),
    "lifestyle_tags": frozenset({"contains"}),
    "relationship_goal": frozenset({"eq"}),
}


class SearchConditionKind(str, Enum):
    """hard conditions gate visibility; soft/rank only order or explain."""

    HARD = "hard"
    SOFT = "soft"
    RANK = "rank"


class SearchConditionUserAction(str, Enum):
    """Per-condition user decision (统一方案 §8.2)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    EDITED = "edited"
    REMOVED = "removed"


class SearchDraftStatus(str, Enum):
    """Search draft lifecycle (统一方案 §8.2)."""

    PARSING = "parsing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    FAILED = "failed"


class SearchTaskStage(str, Enum):
    """Real execution stages for search tasks (统一方案 §8.2)."""

    VALIDATING = "validating"
    CHECKING_VISIBILITY = "checking_visibility"
    FILTERING = "filtering"
    RANKING = "ranking"
    COMPLETED = "completed"
    EMPTY = "empty"
    PARTIAL = "partial"


class SearchCondition(BaseModel):
    """One AST condition; never a SQL fragment (统一方案 §8.1).

    Server-owned allowlists constrain ``field_key``/``operator``/``kind``/
    ``user_action`` at compilation time; ``compile_search_conditions`` is the
    only place where a condition becomes a parameterised filter.
    """

    field_key: str = Field(..., min_length=1, max_length=64)
    operator: SearchConditionOperator = Field(...)
    value: Any = None
    kind: SearchConditionKind = SearchConditionKind.HARD
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_span: str | None = None
    user_action: SearchConditionUserAction = SearchConditionUserAction.PENDING


class SearchConditionRead(BaseModel):
    field_key: str
    operator: SearchConditionOperator
    value: Any = None
    kind: SearchConditionKind
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source_span: str | None = None
    user_action: SearchConditionUserAction = SearchConditionUserAction.PENDING


class SearchDraftCreateRequest(BaseModel):
    query_text: str = Field(..., min_length=1, max_length=1000)
    source: str | None = Field(default=None, max_length=24)
    locale: str | None = Field(default=None, max_length=16)


class SearchDraftRead(BaseModel):
    draft_id: str
    status: SearchDraftStatus
    condition_revision: int = 0
    condition_schema_version: str = "search-condition-v1"
    conditions: list[SearchConditionRead] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None


class SearchDraftParseRead(BaseModel):
    """202 response after creating a draft and its parse task."""

    draft_id: str
    status: SearchDraftStatus
    task_id: str
    condition_schema_version: str = "search-condition-v1"
    expires_at: datetime | None = None


class SearchConditionPatchRequest(BaseModel):
    condition_no: int = Field(..., ge=0)
    action: Literal["confirm", "edit", "remove"]
    value: Any | None = None


class SearchSnapshotRead(BaseModel):
    snapshot_id: str
    status: str = "completed"
    condition_schema_version: str = "search-condition-v1"
    expires_at: datetime | None = None
    degraded: bool = False


class SearchSnapshotAccepted(BaseModel):
    """202 response after confirming a draft into an immutable snapshot.

    ``status`` is the plain ``ai_task.status`` string of the enqueued
    ``search_execute`` task; the snapshot itself is ``completed`` once the
    worker writes its first result page.
    """

    snapshot_id: str
    task_id: str
    status: str = "queued"
    stage: str | None = None
    poll_after_ms: int = Field(default=1000, ge=0)
    expires_at: datetime | None = None
    condition_schema_version: str = "search-condition-v1"
    degraded: bool = False


class SearchResultItemRead(BaseModel):
    user_id: int
    card: dict[str, Any]
    matched_condition_count: int = 0
    matched_conditions: list[str] = Field(default_factory=list)
    unknown_conditions: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)
    profile_revision: int = 0
    result_expires_at: datetime | None = None


class SearchResultPageRead(BaseModel):
    snapshot_id: str
    status: str = "completed"
    items: list[SearchResultItemRead] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
    total_is_estimate: bool = False
    degraded: bool = False


class SearchFieldAllowlist(BaseModel):
    """Server-owned search field/operator dictionary for AST compilation."""

    fields: frozenset[str] = AI_FIELD_ALLOWLIST
    operators: dict[str, frozenset[str]] = FIELD_OPERATOR_ALLOWLIST


class SearchSuggestionRead(BaseModel):
    """Editable tag suggestions; empty array when nothing is confirmed.

    WP-S3：source='ai' 表示来自猜你喜欢 AI 归纳缓存；'tags' 为既有标签回显。
    """

    items: list[str] = Field(default_factory=list)
    page: CursorMeta = Field(default_factory=CursorMeta)
    source: str = Field(default="tags", pattern="^(ai|tags)$")


class SearchSuggestGenerateRead(BaseModel):
    """WP-S3：生成请求的 202 出参（degraded=无投影降级，未建任务）。"""

    task_id: str = ""
    status: str = Field(default="queued")
    source: str = Field(default="ai", pattern="^(ai|tags)$")
    replayed: bool = False
