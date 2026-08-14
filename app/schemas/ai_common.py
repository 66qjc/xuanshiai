"""Common AI-CORE schemas: task status, stable errors, consent, revisions, cursors.

The stable error codes and the response envelope follow the unified plan
§11.1-§11.3.  These schemas are the contract base for Tasks 6/7/8/10/11.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# 字段 allowlist（Task 1 冻结）：与统一方案 §8.1 及 AI_PRODUCT_SECURITY_DECISIONS 一致。
AI_FIELD_ALLOWLIST = frozenset(
    {
        "age",
        "city_code",
        "marriage_status",
        "education_level",
        "height_cm",
        "income_band",
        "occupation_group",
        "interest_tags",
        "lifestyle_tags",
        "relationship_goal",
    }
)

# consent scope 三个（Task 1 冻结）。
CONSENT_SCOPES = frozenset({"profile_text_extract", "search_parse", "compatibility_shadow"})


class AiTaskStatus(str, Enum):
    """Generic AI task state machine (统一方案 §6.4)."""

    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class AiErrorCode(str, Enum):
    """Stable global AI error codes (统一方案 §11.2)."""

    AI_FEATURE_DISABLED = "AI_FEATURE_DISABLED"
    AI_CONSENT_REQUIRED = "AI_CONSENT_REQUIRED"
    AI_INPUT_INVALID = "AI_INPUT_INVALID"
    AI_POLICY_DENIED = "AI_POLICY_DENIED"
    AI_TEMPORARILY_UNAVAILABLE = "AI_TEMPORARILY_UNAVAILABLE"
    AI_QUOTA_EXCEEDED = "AI_QUOTA_EXCEEDED"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_IDEMPOTENCY_CONFLICT = "TASK_IDEMPOTENCY_CONFLICT"
    TASK_NOT_CANCELLABLE = "TASK_NOT_CANCELLABLE"
    PROFILE_SESSION_NOT_FOUND = "PROFILE_SESSION_NOT_FOUND"
    PROFILE_SESSION_STALE = "PROFILE_SESSION_STALE"
    DRAFT_VERSION_CONFLICT = "DRAFT_VERSION_CONFLICT"
    RESULT_STALE = "RESULT_STALE"
    CANDIDATE_NOT_VISIBLE = "CANDIDATE_NOT_VISIBLE"


class AiErrorResponse(BaseModel):
    """Inner AI error payload (统一方案 §11.1/§11.3 shape)."""

    code: str
    message: str
    request_id: str = ""
    retryable: bool = False
    retry_after_ms: int = Field(default=0, ge=0)


class AiErrorDetail(BaseModel):
    """HTTP envelope wrapping the AI error payload."""

    detail: AiErrorResponse


class ConsentSnapshotSchema(BaseModel):
    """Immutable consent snapshot copied at task creation time."""

    scope: str
    version: str
    policy_revision: str
    granted_at: datetime


class AiConsentGrantRequest(BaseModel):
    """Versioned consent text metadata accepted by the public AI API."""

    consent_version: str = Field(..., min_length=1, max_length=32)
    policy_revision: str = Field(..., min_length=1, max_length=64)


class AiConsentRead(BaseModel):
    """One active consent grant; no raw policy text is returned."""

    scope: str
    version: str
    policy_revision: str
    granted_at: datetime


class AiConsentListResponse(BaseModel):
    """Current active consent grants for the authenticated user."""

    consents: list[AiConsentRead] = Field(default_factory=list)
    privacy_revision: int = Field(default=0, ge=0)


class AiConsentOperationResponse(BaseModel):
    """Grant/revoke result with the privacy revision and optional cleanup task."""

    operation_id: str
    scope: str
    operation: str
    status: str
    consent: AiConsentRead | None = None
    cleanup_task_id: str | None = None
    privacy_revision: int = Field(default=0, ge=0)


class ProjectionKind(str, Enum):
    """Frozen projection kinds (统一方案 §10.3)."""

    PERSONAL_SEARCHABLE = "personal_searchable"
    PERSONAL_COMPATIBILITY = "personal_compatibility"
    IDEAL_PARTNER_PREFERENCE = "ideal_partner_preference"


class ProjectionVisibility(str, Enum):
    """Visibility class of a feature projection (§19.5 visibility_scope).

    ``SELF_ONLY`` projections (``ideal_partner_preference``) may only be read by
    the owner's own preference computation and are never returned as candidate
    profiles.
    """

    SEARCHABLE = "searchable"
    SELF_ONLY = "self_only"


class FeatureProjectionRead(BaseModel):
    """Read surface of one ``ai_feature_projection`` row (Task 9+).

    Only allowlisted fields are exposed under ``fields``; the full five-dimension
    revision vector is carried so downstream consumers can re-check validity.
    """

    subject_user_id: int
    projection_kind: ProjectionKind
    source_hash: str
    projection_version: str
    fields: dict[str, Any]
    source_revision: RevisionVectorSchema
    privacy_revision: int
    consent_snapshot: ConsentSnapshotSchema
    visibility_class: ProjectionVisibility
    status: str = "active"
    expires_at: datetime | None = None
    invalidated_at: datetime | None = None
    purge_after: datetime | None = None


class RevisionVectorSchema(BaseModel):
    """Five-dimensional revision vector mirror of ``RevisionVector``."""

    profile: int = 0
    preference: int = 0
    privacy: int = 0
    relationship: int = 0
    policy: int = 0


class CursorMeta(BaseModel):
    """Common signed-cursor pagination fields for AI listing endpoints."""

    next_cursor: str | None = None
    total: int = 0
    total_is_estimate: bool = False
    has_more: bool = False


class TaskPollState(BaseModel):
    """Async 202 task-accepted shape returned by every AI write endpoint."""

    task_id: str
    status: AiTaskStatus
    stage: str | None = None
    poll_after_ms: int = Field(default=1000, ge=0)
    expires_at: datetime | None = None
