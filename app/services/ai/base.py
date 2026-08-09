"""AI-CORE typed request/result contracts, Provider protocol and error classes.

Business modules depend only on this Protocol and these dataclasses; they never
import a vendor SDK.  All provider output is 100% typed and validated by the
Gateway before it can reach a draft, snapshot or projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from app.schemas.ai_common import AI_FIELD_ALLOWLIST
from app.schemas.ai_profile import ProfileFieldConfirmationStatus


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

    field_key: str = Field(..., min_length=1, max_length=64)
    subject: str = Field(default="personal", min_length=1, max_length=24)
    value: Any = None
    source_quote: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_confirmation: bool = True
    confirmation_status: str = ProfileFieldConfirmationStatus.SUGGESTED.value


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
