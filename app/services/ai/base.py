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


class StructuredExtractResult(BaseModel):
    """Typed provider result for profile extraction (统一方案 §6.2 shape)."""

    schema_version: str = "profile-extract-v1"
    fields: tuple[ExtractedField, ...] = ()
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
    icon: str = Field(..., max_length=8)
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
    prompt_version: str = Field(default="profile-narrative-prompt-v1", min_length=1, max_length=32)
    persona_title: str = Field(..., min_length=1, max_length=64)
    persona_tags: tuple[str, ...] = ()
    insight: str = Field(..., min_length=1, max_length=500)
    dimensions: tuple[NarrativeDimension, ...] = ()
    ideal_weights: tuple[NarrativeIdealWeight, ...] = ()
    recent_change: NarrativeRecentChange | None = None
    history_observations: tuple[NarrativeHistoryObservation, ...] = ()


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
