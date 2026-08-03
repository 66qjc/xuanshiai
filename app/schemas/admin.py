"""Administrative moderation schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    action: Literal["none", "hide_content", "restore_content", "restrict_user", "dismiss"] = "none"
    restriction_type: Literal["TOTAL_BAN", "POST_RESTRICTED", "COMMENT_RESTRICTED", "MESSAGE_RESTRICTED", "APPLICATION_RESTRICTED"] | None = None
    restriction_reason_code: str | None = Field(default=None, min_length=1, max_length=64)
    restriction_ends_at: datetime | None = None

    @model_validator(mode="after")
    def validate_status_action(self) -> "ReportReviewRequest":
        if self.action in ("hide_content", "restore_content") and self.status != 1:
            raise ValueError("内容处置只能用于成立的举报")
        if self.action == "dismiss" and self.status != 2:
            raise ValueError("dismiss 只能用于驳回的举报")
        if self.action == "restrict_user" and (self.status != 1 or not self.restriction_type or not self.restriction_reason_code):
            raise ValueError("restrict_user requires a successful review, restriction type and reason code")
        if self.restriction_ends_at and self.restriction_ends_at <= datetime.now(self.restriction_ends_at.tzinfo):
            raise ValueError("restriction end time must be in the future")
        return self


class ReportReviewResponse(BaseModel):
    report_id: int
    status: Literal[1, 2]
    result: str
    action: Literal["none", "hide_content", "restore_content", "restrict_user", "dismiss"] = "none"
    content_moderated: bool = False
    restriction_created: bool = False


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
    action: Literal["none", "hide_content", "restore_content", "restrict_user", "dismiss"] = "none"
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class AdminReportPage(BaseModel):
    items: list[AdminReportItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class AdminReportAppealItem(BaseModel):
    id: int
    report_id: int
    appellant_user_id: int
    reason: str
    status: Literal[0, 1, 2]
    result: str | None = None
    reviewed_by: int | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None
    target_type: Literal["user", "post", "comment", "paper_plane"]
    target_id: int | None
    original_reviewer_id: int | None = None


class AdminReportAppealPage(BaseModel):
    items: list[AdminReportAppealItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class ReportAppealReviewRequest(BaseModel):
    status: Literal[1, 2]
    result: str = Field(min_length=1, max_length=255)


class ReportAppealReviewResponse(BaseModel):
    appeal_id: int
    report_id: int
    status: Literal[1, 2]
    result: str
    content_restored: bool = False


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
    provider: str = "local"
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
