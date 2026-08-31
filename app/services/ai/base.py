"""AI-CORE typed request/result contracts, Provider protocol and error classes.

Business modules depend only on this Protocol and these dataclasses; they never
import a vendor SDK.  All provider output is 100% typed and validated by the
Gateway before it can reach a draft, snapshot or projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.ai_common import AI_FIELD_ALLOWLIST
from app.schemas.ai_profile import (
    PROFILE_ENTRY_CATEGORIES,
    PROFILE_ENTRY_CONTENT_MAX_LENGTH,
    ProfileFieldConfirmationStatus,
    ProfileSubject,
    normalize_profile_extracted_value,
)


@dataclass(frozen=True)
class StructuredExtractRequest:
    """Minimal input projection for profile extraction.

    Carries only the turn texts and the frozen allowlist; never raw ids, phone
    numbers or other hidden profile data.
    """

    subject: str
    turn_texts: tuple[str, ...]
    consent_version: str
    policy_revision: str
    allowlist: frozenset[str] = AI_FIELD_ALLOWLIST
    locale: str | None = None
    target_field_key: str | None = None
    # WP-P4：build=建构问答（既有抽取）；update=对话式追加（澄清式追问，
    # 产出 clarifying_question 或 entry patch 候选）。
    session_kind: str = "build"
    # update 会话专用：该维度已发布条目摘要（含 field_key），供 modify patch
    # 定位被改写条目；build 会话为 None。
    entry_digest: str | None = None


class ExtractedField(BaseModel):
    """One structured field candidate with its source evidence."""

    model_config = ConfigDict(extra="forbid")

    field_key: str = Field(..., min_length=1, max_length=64)
    subject: ProfileSubject
    value: Any = None
    source_quote: str | None = None
    source_span: str | None = Field(default=None, max_length=500)
    source_turn_ids: tuple[str, ...] = ()
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_confirmation: bool = True
    confirmation_status: str = Field(
        default=ProfileFieldConfirmationStatus.SUGGESTED.value,
        pattern="^suggested$",
    )
    schema_version: str = Field(default="profile-extract-v1", min_length=1, max_length=32)
    prompt_version: str = Field(default="profile-extract-prompt-v1", min_length=1, max_length=32)
    policy_revision: str = Field(default="ai-policy-2026-08-07-v1", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_subject_aware_value_and_provenance(self) -> ExtractedField:
        self.value = normalize_profile_extracted_value(self.subject, self.field_key, self.value)
        if self.source_span is None:
            self.source_span = self.source_quote
        elif self.source_quote is not None and self.source_quote != self.source_span:
            raise ValueError("source_quote and source_span must agree")
        if not self.needs_confirmation:
            raise ValueError("provider fields must require confirmation")
        return self


class ExtractedEntry(BaseModel):
    """One free-text profile entry candidate（WP-P1 条目模型）.

    与 ``ExtractedField`` 同一套 provenance 纪律：只准从用户原话归纳
    （faithfulness），必须携带 source_quote/span 证据并等待用户确认。
    ``category`` 受 9 枚举冻结约束；``content`` ≤200 字（DB VARCHAR(200)
    双保险）。entry 的 ``field_key`` 由草稿写入层生成，provider 不产出。
    """

    model_config = ConfigDict(extra="forbid")

    category: str = Field(..., min_length=1, max_length=32)
    content: str = Field(..., min_length=1, max_length=PROFILE_ENTRY_CONTENT_MAX_LENGTH)
    subject: ProfileSubject
    source_quote: str | None = None
    source_span: str | None = Field(default=None, max_length=500)
    source_turn_ids: tuple[str, ...] = ()
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_confirmation: bool = True
    confirmation_status: str = Field(
        default=ProfileFieldConfirmationStatus.SUGGESTED.value,
        pattern="^suggested$",
    )
    schema_version: str = Field(default="profile-extract-v1", min_length=1, max_length=32)
    prompt_version: str = Field(default="profile-extract-prompt-v1", min_length=1, max_length=32)
    policy_revision: str = Field(default="ai-policy-2026-08-07-v1", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_entry_category_and_provenance(self) -> ExtractedEntry:
        if self.category not in PROFILE_ENTRY_CATEGORIES:
            raise ValueError("entry category is not in the frozen allowlist")
        if not self.content.strip():
            raise ValueError("entry content must not be blank")
        if self.source_span is None:
            self.source_span = self.source_quote
        elif self.source_quote is not None and self.source_quote != self.source_span:
            raise ValueError("source_quote and source_span must agree")
        if not self.needs_confirmation:
            raise ValueError("provider entries must require confirmation")
        return self


class ExtractedPatch(BaseModel):
    """One entry patch candidate produced by an update (clarify) session（WP-P4）.

    ``add`` 新增条目；``modify`` 改写既有条目——必须携带
    ``replaces_field_key`` 指向被改写条目（旧条目行永不删除，覆盖语义由
    读取端 New 角标/排序表达，追加不覆盖是硬约束）。
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., pattern="^(add|modify)$")
    category: str = Field(..., min_length=1, max_length=32)
    content: str = Field(..., min_length=1, max_length=PROFILE_ENTRY_CONTENT_MAX_LENGTH)
    replaces_field_key: str | None = Field(default=None, max_length=64)
    subject: ProfileSubject
    source_quote: str | None = None
    source_span: str | None = Field(default=None, max_length=500)
    source_turn_ids: tuple[str, ...] = ()
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_confirmation: bool = True
    confirmation_status: str = Field(
        default=ProfileFieldConfirmationStatus.SUGGESTED.value,
        pattern="^suggested$",
    )
    schema_version: str = Field(default="profile-extract-v1", min_length=1, max_length=32)
    prompt_version: str = Field(default="profile-extract-prompt-v1", min_length=1, max_length=32)
    policy_revision: str = Field(default="ai-policy-2026-08-07-v1", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_patch_contract(self) -> ExtractedPatch:
        if self.category not in PROFILE_ENTRY_CATEGORIES:
            raise ValueError("patch category is not in the frozen allowlist")
        if not self.content.strip():
            raise ValueError("patch content must not be blank")
        if self.action == "modify" and not self.replaces_field_key:
            raise ValueError("modify patch requires replaces_field_key")
        if self.action == "add" and self.replaces_field_key:
            raise ValueError("add patch must not carry replaces_field_key")
        if self.source_span is None:
            self.source_span = self.source_quote
        elif self.source_quote is not None and self.source_quote != self.source_span:
            raise ValueError("source_quote and source_span must agree")
        if not self.needs_confirmation:
            raise ValueError("provider patches must require confirmation")
        return self


class StructuredExtractResult(BaseModel):
    """Typed provider result for profile extraction (统一方案 §6.2 shape)."""

    schema_version: str = "profile-extract-v1"
    fields: tuple[ExtractedField, ...] = ()
    # WP-P1 加法通道：条目候选。默认空 tuple，既有 provider/fake 零感知。
    entries: tuple[ExtractedEntry, ...] = ()
    # WP-P4 加法通道：update 会话的澄清追问与 entry patch 候选。
    clarifying_question: str | None = Field(default=None, max_length=300)
    patches: tuple[ExtractedPatch, ...] = ()
    unknown_or_ambiguous: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchParseRequest:
    """Query text plus the frozen search field/operator allowlist."""

    query_text: str
    locale: str | None = None
    allowlist: frozenset[str] = AI_FIELD_ALLOWLIST


class SearchCondition(BaseModel):
    """One AST condition; never a SQL fragment."""

    field_key: str = Field(..., min_length=1, max_length=64)
    operator: str = Field(..., min_length=1, max_length=24)
    value: Any = None
    kind: str = Field(default="hard", pattern="^(hard|soft|rank)$")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_span: str | None = None
    user_action: str = Field(default="pending", pattern="^(pending|confirmed|edited|removed)$")


class SearchParseResult(BaseModel):
    """Typed provider result for search query parsing (§8.1 AST shape)."""

    schema_version: str = "search-condition-v1"
    conditions: tuple[SearchCondition, ...] = ()
    unknown: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchSuggestRequest:
    """WP-S3：猜你喜欢的输入投影（最小化，不含原文/ID）。

    ``context_lines`` 由服务层从用户双投影（interest/lifestyle 标签、
    ideal_partner 结构化字段、entry_digest）折成的摘要行；LLM 只准基于
    这些行归纳搜索词（faithfulness），禁止编造用户没有的偏好。
    """

    context_lines: tuple[str, ...] = ()
    consent_version: str = "consent-v1"
    policy_revision: str = "ai-policy-v1"


class SearchSuggestResult(BaseModel):
    """Typed provider result for AI 猜你喜欢（3~5 条自然语言搜索词）。"""

    schema_version: str = Field(default="search-suggest-v1", min_length=1, max_length=32)
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModerationRequest:
    """Text to moderate; the Gateway never logs the raw text."""

    text: str
    scene: str = "profile"


class ModerationResult(BaseModel):
    """Content-governance verdict."""

    allowed: bool = True
    action: str = Field(default="allow", pattern="^(allow|reject|review)$")
    reason_code: str | None = None


# ----------------------------------------------------------------------
# Narrative (画像叙事层) — 发布后基于已确认字段生成人格画像解读
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class NarrativeRequest:
    """Input for profile narrative generation.

    Carries the user's confirmed fields (current + previous revision) so the
    provider can synthesise a persona portrait and detect changes over time.
    Field dicts are plain ``{field_key, value, display_value}`` — no raw
    answer text, no ids.
    """

    subject: str
    current_fields: tuple[dict[str, Any], ...]
    previous_fields: tuple[dict[str, Any], ...]
    history_summaries: tuple[dict[str, Any], ...]
    consent_version: str
    policy_revision: str


class NarrativeDimension(BaseModel):
    """One persona dimension card (e.g. 感情观 / 性格 / 生活方式)."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=64)
    icon: str = Field(..., max_length=16)
    title: str = Field(..., min_length=1, max_length=32)
    summary: str = Field(..., min_length=1, max_length=200)


class NarrativeIdealWeight(BaseModel):
    """One ideal-partner preference weight (only for ideal_partner subject)."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=32)
    percent: int = Field(..., ge=0, le=100)


class NarrativeRecentChange(BaseModel):
    """Diff insight between current and previous revision."""

    model_config = ConfigDict(extra="forbid")

    direction: str = Field(..., pattern="^(up|down)$")
    summary: str = Field(..., min_length=1, max_length=200)
    observation: str = Field(..., min_length=1, max_length=300)


class NarrativeHistoryObservation(BaseModel):
    """One historical revision observation for the timeline."""

    model_config = ConfigDict(extra="forbid")

    revision_id: int = Field(..., ge=1)
    keywords: tuple[str, ...] = ()
    observation: str = Field(..., min_length=1, max_length=300)


class NarrativeResult(BaseModel):
    """Typed provider result for narrative generation (画像叙事层)."""

    schema_version: str = Field(default="profile-narrative-v1", min_length=1, max_length=32)
    prompt_version: str = Field(default="profile-narrative-prompt-v4", min_length=1, max_length=32)
    persona_title: str = Field(..., min_length=1, max_length=64)
    persona_tags: tuple[str, ...] = ()
    insight: str = Field(..., min_length=1, max_length=500)
    dimensions: tuple[NarrativeDimension, ...] = ()
    ideal_weights: tuple[NarrativeIdealWeight, ...] = ()
    recent_change: NarrativeRecentChange | None = None
    history_observations: tuple[NarrativeHistoryObservation, ...] = ()
    # 写在最后：整份画像的概括性收束（prompt v3 起生成；旧数据无此字段为 None）。
    conclusion: str | None = Field(default=None, min_length=1, max_length=300)


class AIProvider(Protocol):
    """Provider adapter interface. One implementation in phase 1: MockAIProvider."""

    async def structured_extract(
        self, request: StructuredExtractRequest
    ) -> StructuredExtractResult: ...

    async def parse_search_query(
        self, request: SearchParseRequest
    ) -> SearchParseResult: ...

    async def moderate_text(
        self, request: ModerationRequest
    ) -> ModerationResult: ...

    async def generate_narrative(
        self, request: NarrativeRequest
    ) -> NarrativeResult: ...

    async def generate_reply(
        self, request: ReplyRequest
    ) -> ReplyResult: ...

    async def generate_search_suggestions(
        self, request: SearchSuggestRequest
    ) -> SearchSuggestResult: ...

    async def compare_compatibility(
        self, request: CompatibilityCompareRequest
    ) -> CompatibilityCompareResult: ...


@dataclass(frozen=True)
class ReplyRequest:
    """Input for one voice-conversation reply generation.

    ``transcript`` is the user's final ASR text for this turn (the only raw
    user text crossing the boundary).  ``known_fields`` carries already
    extracted field summaries as plain ``{field_key, value}`` dicts so the
    reply can reference what is already known without re-asking.
    """

    transcript: str
    field_key: str = ""
    known_fields: tuple[dict[str, Any], ...] = ()
    consent_version: str = "consent-v1"
    policy_revision: str = "ai-policy-v1"


class ReplyResult(BaseModel):
    """Typed provider result for one conversational reply.

    ``reply_text`` is spoken aloud via TTS — length is capped to keep the
    spoken turn short (prompt asks for ≤30 chars; 200 is a hard guard).
    """

    model_config = ConfigDict(extra="forbid")

    reply_text: str = Field(..., min_length=1, max_length=200)


@dataclass(frozen=True)
class CompatibilityCompareRequest:
    """Input for one LLM 双向匹配度精算（WP-C1b）。

    两侧均为**已发布投影的可读摘要**：结构化字段序列化文本 + 条目摘要
    （entry_digest）。不含原始回答、用户 ID 或认证/会员信号。
    """

    viewer_personal: str
    target_personal: str
    viewer_ideal: str = ""
    target_ideal: str = ""
    viewer_personal_digest: str | None = None
    viewer_ideal_digest: str | None = None
    target_personal_digest: str | None = None
    target_ideal_digest: str | None = None


class CompatibilityCompareDirection(BaseModel):
    """一个方向的精算结果：0-100 概率 + 恰好 3 条中文可解释理由。"""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(..., ge=0, le=100)
    reasons: tuple[str, ...] = Field(..., min_length=3, max_length=3)


class CompatibilityCompareResult(BaseModel):
    """Typed provider result for 双向匹配度精算（写入快照前原值 0-100）。"""

    model_config = ConfigDict(extra="forbid")

    viewer_to_target: CompatibilityCompareDirection
    target_to_viewer: CompatibilityCompareDirection


class ProviderErrorKind(str, Enum):
    """Retryability classification for provider failures (统一方案 §6.4)."""

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


class ProviderError(Exception):
    """Normalised provider failure carrying a stable code and retryability.

    Retryable: network timeout, provider 429, transient 5xx.
    Non-retryable: schema violation, policy denial, consent revocation,
    missing resource, version conflict.
    """

    def __init__(
        self,
        code: str,
        message: str,
        kind: ProviderErrorKind = ProviderErrorKind.NON_RETRYABLE,
        retry_after_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind
        self.retry_after_ms = int(retry_after_ms)

    @property
    def retryable(self) -> bool:
        return self.kind is ProviderErrorKind.RETRYABLE


@dataclass(frozen=True)
class AITaskContext:
    """Audit/trace metadata for one Gateway invocation."""

    task_id: str
    request_id: str
    scene: str
    provider: str = "mock"
    model: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    input_revision: dict[str, int] = field(default_factory=dict)
    policy_revision: str | None = None


@dataclass(frozen=True)
class GatewayCallRecord:
    """Minimal auditable record of one provider call.

    Never contains prompt text, original answers, provider raw responses or
    secrets — by construction this dataclass only exposes metadata.
    """

    request_id: str
    task_id: str
    scene: str
    provider: str
    model: str | None
    prompt_version: str | None
    schema_version: str | None
    duration_ms: int
    token_usage: dict[str, int] | None
    error_code: str | None
    succeeded: bool
    input_revision: dict[str, int] = field(default_factory=dict)
    policy_revision: str | None = None
