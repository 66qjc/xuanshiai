"""M04 AI 画像 schemas: session, turn, draft field and immutable revision contracts.

Enums and field names follow the unified plan §7.  ``ProfileSubject`` isolates
``personal`` (mapped to the user's own approved profile) from ``ideal_partner``
(only ever mapped to the user's own preference projection).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictInt, StrictStr, TypeAdapter, model_validator

from app.schemas.ai_common import AI_FIELD_ALLOWLIST, AiTaskStatus


class ProfileSubject(str, Enum):
    """Profile subject enum; ideal_partner never becomes another user's facts."""

    PERSONAL = "personal"
    IDEAL_PARTNER = "ideal_partner"


# ----------------------------------------------------------------------
# WP-P1 / F4：条目（entry）分类契约
# ----------------------------------------------------------------------
# 分类冻结为 9 个 slug，前后端与 prompt 共用同一常量；越界一律
# AI_INPUT_INVALID（服务层）或 ValidationError（provider 边界）。
# entry 不计入发布门槛与进度——条目是丰富度增强，不改变建构门槛边界。
PROFILE_ENTRY_CATEGORIES: frozenset[str] = frozenset(
    {
        "basics",
        "occupation",
        "appearance",
        "personality",
        "values",
        "interests",
        "routine",
        "diet",
        "life_plan",
    }
)

# 分类中文标签：读取端分组展示与 entry_digest 摘要行前缀共用。
PROFILE_ENTRY_CATEGORY_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "basics": "基本情况",
        "occupation": "工作状态",
        "appearance": "外形特征",
        "personality": "性格特征",
        "values": "价值观",
        "interests": "兴趣爱好",
        "routine": "作息习惯",
        "diet": "饮食习惯",
        "life_plan": "生活规划",
    }
)

# entry 单条正文上限：服务层校验 + DB VARCHAR(200) 双保险。
PROFILE_ENTRY_CONTENT_MAX_LENGTH = 200


class NumericPreferenceRange(BaseModel):
    """One-sided or two-sided numeric preference, always serialized as min/max."""

    min: StrictInt | None = None
    max: StrictInt | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> NumericPreferenceRange:
        if self.min is None and self.max is None:
            raise ValueError("a preference range needs min or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("preference range min cannot exceed max")
        return self


# These frozen dictionaries are the extraction contract.  They make the two
# subjects intentionally non-interchangeable: personal values are facts,
# while ideal_partner values are constraints consumed only by preference and
# compatibility projections.
PERSONAL_FACT_FIELD_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "age": "integer",
        "city_code": "string",
        "marriage_status": "enum",
        "education_level": "integer",
        "height_cm": "integer",
        "income_band": "integer",
        "occupation_group": "enum",
        "interest_tags": "tags",
        "lifestyle_tags": "tags",
        "relationship_goal": "enum",
    }
)
IDEAL_PARTNER_FIELD_KINDS: Mapping[str, str] = MappingProxyType(
    {
        "age": "range",
        "city_code": "set",
        "marriage_status": "set",
        "education_level": "range",
        "height_cm": "range",
        "income_band": "range",
        "occupation_group": "set",
        "interest_tags": "tags",
        "lifestyle_tags": "tags",
        "relationship_goal": "set",
    }
)
PROFILE_ENUM_DICTIONARY: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "marriage_status": frozenset({"single", "divorced", "widowed"}),
        "occupation_group": frozenset(
            {"technology", "education", "healthcare", "finance", "public_service", "other"}
        ),
        "relationship_goal": frozenset({"marriage", "dating", "friendship"}),
    }
)

_TAG_ADAPTER = TypeAdapter(tuple[StrictStr, ...])
_STRING_ADAPTER = TypeAdapter(StrictStr)
_INTEGER_ADAPTER = TypeAdapter(StrictInt)
_RANGE_FIELDS = frozenset({"age", "education_level", "height_cm", "income_band"})
_TAG_FIELDS = frozenset({"interest_tags", "lifestyle_tags"})
_RANGE_LIMITS: Mapping[str, tuple[int, int | None]] = MappingProxyType(
    {
        "age": (18, 100),
        "education_level": (1, 8),
        "height_cm": (100, 250),
        "income_band": (0, None),
    }
)


def normalize_profile_extracted_value(
    subject: ProfileSubject, field_key: str, value: Any
) -> int | str | tuple[str, ...] | dict[str, int | None]:
    """Validate and normalize one provider value using the frozen subject contract.

    It deliberately rejects unknown/authentication fields instead of letting a
    later storage layer silently skip them.  ``ValueError`` is converted by the
    Gateway into the stable safe ``AI_INPUT_INVALID`` code.
    """
    try:
        subject = ProfileSubject(subject)
    except (TypeError, ValueError) as exc:
        raise ValueError("unknown profile subject") from exc
    kinds = (
        PERSONAL_FACT_FIELD_KINDS
        if subject is ProfileSubject.PERSONAL
        else IDEAL_PARTNER_FIELD_KINDS
    )
    kind = kinds.get(field_key)
    if kind is None or field_key not in AI_FIELD_ALLOWLIST:
        raise ValueError("unknown or non-profile field")

    if kind == "range":
        numeric_range = NumericPreferenceRange.model_validate(value)
        floor, ceiling = _RANGE_LIMITS[field_key]
        for bound in (numeric_range.min, numeric_range.max):
            if bound is not None and (bound < floor or (ceiling is not None and bound > ceiling)):
                raise ValueError("preference range is outside the allowed bounds")
        return numeric_range.model_dump()

    if kind in {"tags", "set"}:
        values = _TAG_ADAPTER.validate_python(value)
        if not values or len(set(values)) != len(values):
            raise ValueError("tag or enum collection must be non-empty and unique")
        if field_key in PROFILE_ENUM_DICTIONARY:
            allowed = PROFILE_ENUM_DICTIONARY[field_key]
            if not set(values).issubset(allowed):
                raise ValueError("enum collection contains an unsupported value")
        return values

    if kind == "enum":
        enum_value = _STRING_ADAPTER.validate_python(value)
        if enum_value not in PROFILE_ENUM_DICTIONARY[field_key]:
            raise ValueError("enum value is unsupported")
        return enum_value

    if kind == "integer":
        integer = _INTEGER_ADAPTER.validate_python(value)
        floor, ceiling = _RANGE_LIMITS.get(field_key, (0, None))
        if integer < floor or (ceiling is not None and integer > ceiling):
            raise ValueError("integer value is outside the allowed bounds")
        return integer

    text_value = _STRING_ADAPTER.validate_python(value)
    if not text_value:
        raise ValueError("string value must not be empty")
    return text_value


class ProfileSessionStatus(str, Enum):
    """Profile session lifecycle (统一方案 §7.2)."""

    DRAFT = "draft"
    EXTRACTING = "extracting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PAUSED = "paused"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"


class ProfileFieldConfirmationStatus(str, Enum):
    """Field confirmation state (统一方案 §7.2)."""

    SUGGESTED = "suggested"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    DELETED = "deleted"


class ProfileFieldPatchAction(str, Enum):
    """Per-field edit actions (统一方案 §7.4)."""

    CONFIRM = "confirm"
    REPLACE = "replace"
    REJECT = "reject"
    DELETE = "delete"


class ProfileProgress(BaseModel):
    """Profile build progress basis (统一方案 §16: coverage, not completeness)."""

    basis: str = "confirmed_field_coverage"
    value: float = Field(default=0.0, ge=0.0, le=1.0)
    # WP-P2 提前建构引导：确认字段覆盖达到可配置阈值（默认 7/10 ≈ 67%）
    # 时为 True，并携带引导文案；阈值与发布硬门槛共用 settings.ai_profile_min_fields。
    can_early_publish: bool = False
    early_publish_hint: str = ""


class ProfileQuestion(BaseModel):
    """One interview question whose id/text/field_key are frozen by the question bank.

    ``field_key`` 是该问题对应的目标抽取字段（属于 ``AI_FIELD_ALLOWLIST``），
    前端据此稳定映射到 typed field 编辑器，不依赖问题文案或顺序。加法字段，
    保留原有 ``id``/``text``，不做破坏性重命名。
    """

    id: str
    text: str
    field_key: str


class ProfileSessionCreateRequest(BaseModel):
    subject: ProfileSubject
    consent_version: str = Field(..., min_length=1, max_length=32)
    input_mode: Literal["text"] = "text"


class ProfileTurnCreateRequest(BaseModel):
    client_turn_id: str = Field(..., min_length=8, max_length=128)
    answer_text: str = Field(..., min_length=1, max_length=2000)


class ProfileSkipQuestionRequest(BaseModel):
    """Skip the current interview question without confirming a field."""

    field_key: str = Field(..., min_length=1, max_length=64)


class ProfileSessionRead(BaseModel):
    session_id: str
    subject: ProfileSubject
    status: ProfileSessionStatus
    input_mode: str = "text"
    progress: ProfileProgress
    current_question: dict[str, str] | None = None
    # 加法字段（Task6 Step2）：当前会话的活动草稿 ID，供前端直接跳转草稿编辑器；
    # 无活动草稿（如新建会话尚未抽取）为 ``None``。保留现有字段，不破坏旧契约。
    draft_id: str | None = None
    profile_revision: int = 0
    preference_revision: int = 0
    expires_at: datetime | None = None
    created_at: datetime


class ProfileTurnRead(BaseModel):
    turn_id: str
    session_id: str
    client_turn_id: str
    turn_no: int
    role: str = "user"
    answer_text: str
    status: str = "saved"
    created_at: datetime


class ProfileDraftFieldRead(BaseModel):
    field_key: str
    subject: ProfileSubject
    value: Any = None
    display_value: str | None = None
    source_quote: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_confirmation: bool = True
    confirmation_status: ProfileFieldConfirmationStatus
    content_hash: str | None = None
    # WP-P1 加法字段：条目（entry）语义。structured 字段恒为默认值，
    # 旧前端零感知；entry 行带 category/content，value 恒为 None。
    field_kind: str = Field(default="structured", pattern="^(structured|entry)$")
    category: str | None = None
    content: str | None = None
    replaces_field_key: str | None = None


class ProfileDraftRead(BaseModel):
    draft_id: str
    subject: ProfileSubject
    status: str = "draft"
    expected_revision: int = 0
    policy_revision: str
    schema_version: str = "profile-extract-v1"
    fields: list[ProfileDraftFieldRead] = Field(default_factory=list)
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProfileDraftFieldPatchRequest(BaseModel):
    field_key: str = Field(..., min_length=1, max_length=64)
    action: ProfileFieldPatchAction
    value: Any | None = None
    expected_revision: int = Field(..., ge=0)


class ProfileRevisionFieldRead(BaseModel):
    revision_id: int
    field_key: str
    subject: ProfileSubject
    value: Any = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    content_hash: str


class ProfileRevisionRead(BaseModel):
    revision_id: int
    subject: ProfileSubject
    revision_no: int
    policy_revision: str
    field_count: int = 0
    published_at: datetime


class ProfileDraftPatchRequest(BaseModel):
    """PATCH draft body: draft-level optimistic lock plus per-field actions.

    Each ``ProfileDraftFieldPatchRequest`` also carries its own
    ``expected_revision`` (统一方案 §7.4「PATCH 逐项 confirm/replace/reject/delete，
    必须携带 expected_revision」); the service rejects any action whose revision
    does not match the current draft revision with ``409 DRAFT_VERSION_CONFLICT``.
    """

    expected_revision: int = Field(..., ge=0)
    actions: list[ProfileDraftFieldPatchRequest] = Field(
        default_factory=list, min_length=1, max_length=50
    )


class ProfilePublishAccepted(BaseModel):
    """202 publish response: a queued projection task plus the immutable revision.

    ``replayed=True`` marks a same-key same-payload retry: the first task is
    returned and nothing is written twice.  Revision fields are ``null`` on a
    replay because no new revision is created.

    ``narrative_task_id`` carries the async narrative generation task so the
    frontend can poll it via the standard task-status endpoint instead of
    polling the narrative business interface with a fixed short window.
    """

    task_id: str
    status: AiTaskStatus
    stage: str | None = None
    poll_after_ms: int = Field(default=1000, ge=0)
    expires_at: datetime | None = None
    replayed: bool = False
    revision_id: int | None = None
    revision_no: int | None = None
    subject: ProfileSubject | None = None
    field_count: int | None = None
    narrative_task_id: str | None = None


class ProfileRevisionPage(BaseModel):
    """Cursor-paginated immutable revision history (self-owned, read-only)."""

    items: list[ProfileRevisionRead] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
    total_is_estimate: bool = False
    has_more: bool = False


class ProfileFieldAllowlist(BaseModel):
    """Server-owned field dictionary used to guard AI extraction."""

    allowlist: frozenset[str] = AI_FIELD_ALLOWLIST


class ProfileTurnSubmissionRead(BaseModel):
    """202 turn+task shape returned by ``POST /profile-sessions/{id}/turns``.

    ``replayed=True`` marks a duplicate ``client_turn_id``: the original turn is
    returned and no second task is created (task fields are ``null``).
    """

    turn_id: str
    session_id: str
    client_turn_id: str
    turn_no: int
    role: str = "user"
    status: str = "saved"
    replayed: bool = False
    task_id: str | None = None
    task_status: AiTaskStatus | None = None
    stage: str | None = None
    poll_after_ms: int = Field(default=0, ge=0)
    expires_at: datetime | None = None


class CleanupTaskAccepted(BaseModel):
    """202 soft-delete response: the session is already hidden synchronously."""

    task_id: str
    status: AiTaskStatus
    cleanup_requested: bool = True


# ----------------------------------------------------------------------
# 画像叙事层（narrative）响应 schema — 对齐前端 mock 数据结构
# ----------------------------------------------------------------------


class ProfileNarrativeDimension(BaseModel):
    """叙事层维度卡片。"""

    key: str
    icon: str
    title: str
    summary: str


class ProfileNarrativeIdealWeight(BaseModel):
    """理想型权重（仅 ideal_partner subject 有值）。"""

    key: str
    label: str
    percent: int


class ProfileNarrativeRecentChange(BaseModel):
    """最近变化趋势。"""

    direction: str
    summary: str
    observation: str


class ProfileNarrativeHistoryObservation(BaseModel):
    """历史版本观察记录。"""

    revision_id: int = 1
    keywords: list[str] = []
    observation: str


class ProfileNarrativeRead(BaseModel):
    """GET /ai/profiles/{subject}/narrative 响应。

    ``status`` 为 ``'pending'`` | ``'pending_confirmation'`` | ``'confirmed'``：
    pending 表示未发布或 narrative 任务未完成；新叙事层先生成为
    ``pending_confirmation``（待用户确认，前端据此驱动确认 UI），用户调用
    confirm 后转 ``'confirmed'``。``'published'`` 仅为历史行兼容值。
    pending/pending_confirmation 状态下前端应引导确认，字段内容已可展示。
    """

    subject: str
    status: str = "pending"
    persona_title: str = ""
    persona_tags: list[str] = []
    insight: str = ""
    dimensions: list[ProfileNarrativeDimension] = []
    ideal_weights: list[ProfileNarrativeIdealWeight] = []
    recent_change: ProfileNarrativeRecentChange | None = None
    history_observations: list[ProfileNarrativeHistoryObservation] = []
    # 写在最后：整份画像的概括性收束（旧画像无此字段时为空串）。
    conclusion: str = ""
