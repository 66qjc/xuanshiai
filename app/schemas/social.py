"""Schemas for relationships, chat, notifications and safety actions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SocialUser(BaseModel):
    user_id: int
    nickname: str | None
    avatar: str | None
    age: int | None


class RelationResponse(BaseModel):
    target_user_id: int
    relation_type: Literal["like", "follow"]
    enabled: bool
    matched: bool = False


class RelationPage(BaseModel):
    items: list[SocialUser]
    page: int
    page_size: int
    total: int


class MatchItem(BaseModel):
    target: SocialUser
    status: Literal[1, 2, 3]
    matched_at: datetime | None


class ChatSessionResponse(BaseModel):
    id: int
    target: SocialUser
    last_message: str | None
    last_message_time: datetime | None
    unread_count: int


class ChatSessionPage(BaseModel):
    items: list[ChatSessionResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class ChatMessageCreate(BaseModel):
    type: Literal[1, 2, 3, 4, 5, 6] = 1
    content: str | None = Field(default=None, max_length=5000)
    media_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_content(self):
        if self.type == 1 and not self.content:
            raise ValueError("文本消息内容不能为空")
        if self.type in (2, 3, 4) and not self.media_url:
            raise ValueError("媒体消息必须提供media_url")
        if self.type in (5, 6) and not self.content and not self.media_url:
            raise ValueError("消息内容不能为空")
        return self


class ChatMessageResponse(BaseModel):
    id: int
    session_id: int
    from_user_id: int
    to_user_id: int
    type: int
    content: str | None
    media_url: str | None
    is_read: bool
    revoked: bool
    is_recalled: bool = False
    recalled_at: datetime | None = None
    created_at: datetime


class NotificationItem(BaseModel):
    id: int
    notification_type: str
    title: str | None
    content: str
    payload: dict | None
    related_user_id: int | None
    related_id: int | None
    target_type: str | None = None
    target_id: int | None = None
    actor_user_id: int | None = None
    action: str = ""
    is_read: bool
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationItem]
    page: int
    page_size: int
    total: int
    unread_count: int


class PrivacyUpdateRequest(BaseModel):
    hide_phone: bool | None = None
    hide_school: bool | None = None
    hide_company: bool | None = None
    hide_distance: bool | None = None
    hide_online_status: bool | None = None
    only_auth_can_contact: bool | None = None
    only_vip_can_see_detail: bool | None = None
    who_can_see_me: Literal[1, 2, 3, 4] | None = None
    match_status: Literal[1, 2, 3, 4, 5] | None = None
    anonymous_browse_enabled: bool | None = None
    show_profile: bool | None = None
    show_likes: bool | None = None
    show_posts: bool | None = None
    notify_like: bool | None = None
    notify_comment: bool | None = None
    notify_follow: bool | None = None
    notify_message: bool | None = None
    notify_match: bool | None = None
    notify_apply: bool | None = None
    notify_system: bool | None = None
    notify_activity: bool | None = None


class PrivacyResponse(BaseModel):
    user_id: int
    hide_phone: bool
    hide_school: bool
    hide_company: bool
    hide_distance: bool
    hide_online_status: bool
    only_auth_can_contact: bool
    only_vip_can_see_detail: bool
    who_can_see_me: int
    match_status: int
    anonymous_browse_enabled: bool
    show_profile: bool
    show_likes: bool
    show_posts: bool
    notify_like: bool
    notify_comment: bool
    notify_follow: bool
    notify_message: bool
    notify_match: bool
    notify_apply: bool
    notify_system: bool
    notify_activity: bool


class BlockRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class ReportRequest(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    images: list[str] = Field(default_factory=list, max_length=6)


class ReportResponse(BaseModel):
    id: int
    target_user_id: int
    target_type: Literal["user", "post", "comment", "paper_plane"] = "user"
    target_id: int | None = None
    type: str
    status: Literal[0, 1, 2]
    created_at: datetime


class ReportDetailResponse(BaseModel):
    id: int
    target_user_id: int
    target_type: Literal["user", "post", "comment", "paper_plane"]
    target_id: int | None
    viewer_role: Literal["reporter", "subject"]
    type: str | None
    description: str | None
    status: Literal[0, 1, 2]
    result: str | None
    action: Literal["none", "hide_content", "restore_content", "restrict_user", "dismiss"] = "none"
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None
    can_appeal: bool = False


class ReportPage(BaseModel):
    items: list[ReportDetailResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class ReportAppealCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ReportAppealResponse(BaseModel):
    id: int
    report_id: int
    appellant_user_id: int
    reason: str
    status: Literal[0, 1, 2]
    result: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class ReportAppealPage(BaseModel):
    items: list[ReportAppealResponse]
    page: int
    page_size: int
    total: int
    has_more: bool
