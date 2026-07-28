"""Administrative moderation schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MediaReviewRequest(BaseModel):
    status: Literal[1, 2, 3]
    reason: str | None = Field(default=None, max_length=255)


class MediaReviewResponse(BaseModel):
    media_id: int
    user_id: int
    status: Literal[1, 2, 3]
    reason: str | None


class ReportReviewRequest(BaseModel):
    status: Literal[1, 2]
    result: str = Field(min_length=1, max_length=255)
    action: Literal["none", "hide_content", "restore_content", "dismiss"] = "none"


class ReportReviewResponse(BaseModel):
    report_id: int
    status: Literal[1, 2]
    result: str
    action: Literal["none", "hide_content", "restore_content", "dismiss"] = "none"
    content_moderated: bool = False


class AdminReportItem(BaseModel):
    id: int
    reporter_user_id: int
    target_user_id: int
    target_type: Literal["user", "post", "comment", "paper_plane"]
    target_id: int | None
    type: str | None
    description: str | None
    status: Literal[0, 1, 2]
    result: str | None
    created_at: datetime
    updated_at: datetime | None = None


class AdminReportPage(BaseModel):
    items: list[AdminReportItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class ContentModerationRequest(BaseModel):
    status: Literal[1, 2]
    reason: str | None = Field(default=None, max_length=255)


class ContentModerationResponse(BaseModel):
    target_type: Literal["post", "comment", "paper_plane"]
    target_id: int
    status: Literal[1, 2]
    reason: str | None = None


class CertificationReviewRequest(BaseModel):
    status: Literal[2, 3]
    reason: str | None = Field(default=None, max_length=255)


class CertificationReviewResponse(BaseModel):
    user_id: int
    kind: Literal["education", "house", "marriage"]
    status: Literal[2, 3]
    reason: str | None


class ModerationItem(BaseModel):
    id: int
    target_type: Literal["post", "comment", "paper_plane", "paper_plane_reply", "paper_plane_message", "media"]
    target_id: int
    user_id: int
    status: Literal["pending", "approved", "rejected", "replaced", "deleted", "hidden"]
    risk_level: int
    matched_words: list[str]
    raw_content: str | None
    display_content: str | None
    reason: str | None
    created_at: datetime
    expires_at: datetime


class ModerationItemPage(BaseModel):
    items: list[ModerationItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class ModerationReviewRequest(BaseModel):
    action: Literal["approve", "reject", "replace", "delete", "hide"]
    reason: str = Field(min_length=1, max_length=255)
    display_content: str | None = Field(default=None, max_length=2000)


class ModerationReviewResponse(BaseModel):
    id: int
    target_type: str
    target_id: int
    status: str
    reason: str


class AdminGrantRequest(BaseModel):
    user_id: int = Field(ge=1)
    permissions: list[str] = Field(default_factory=list, max_length=50)


class AdminGrantResponse(BaseModel):
    user_id: int
    role_code: Literal["admin"]
    permissions: list[str]
