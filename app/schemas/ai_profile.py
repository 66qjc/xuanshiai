"""M04 AI 画像 schemas: session, turn, draft field and immutable revision contracts.

Enums and field names follow the unified plan §7.  ``ProfileSubject`` isolates
``personal`` (mapped to the user's own approved profile) from ``ideal_partner``
(only ever mapped to the user's own preference projection).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.ai_common import AI_FIELD_ALLOWLIST, AiTaskStatus


class ProfileSubject(str, Enum):
    """Profile subject enum; ideal_partner never becomes another user's facts."""

    PERSONAL = "personal"
    IDEAL_PARTNER = "ideal_partner"


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


class ProfileQuestion(BaseModel):
    """One interview question whose id/text are frozen by the question bank."""

    id: str
    text: str


class ProfileSessionCreateRequest(BaseModel):
    subject: ProfileSubject
    consent_version: str = Field(..., min_length=1, max_length=32)
    input_mode: Literal["text"] = "text"


class ProfileTurnCreateRequest(BaseModel):
    client_turn_id: str = Field(..., min_length=8, max_length=128)
    answer_text: str = Field(..., min_length=1, max_length=2000)


class ProfileSessionRead(BaseModel):
    session_id: str
    subject: ProfileSubject
    status: ProfileSessionStatus
    input_mode: str = "text"
    progress: ProfileProgress
    current_question: dict[str, str] | None = None
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
