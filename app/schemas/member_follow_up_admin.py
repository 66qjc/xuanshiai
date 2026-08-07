"""Member CRM follow-up and behavior contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MemberFollowUpCreate(BaseModel):
    method: Literal["PHONE", "WECHAT", "VISIT", "OTHER"]
    content: str = Field(min_length=1, max_length=2000)
    next_follow_at: datetime | None = None


class MemberFollowUp(BaseModel):
    id: int
    user_id: int
    method: str
    content: str
    next_follow_at: datetime | None
    created_by: int
    created_at: datetime


class MemberFollowUpPage(BaseModel):
    items: list[MemberFollowUp]
    page: int
    page_size: int
    total: int
    has_more: bool


class MemberBehaviorItem(BaseModel):
    event_type: Literal["login", "browse", "favorite", "swipe", "apply"]
    event_id: int
    target_user_id: int | None
    target_nickname: str | None
    detail: str | None
    occurred_at: datetime


class MemberBehaviorPage(BaseModel):
    items: list[MemberBehaviorItem]
    page: int
    page_size: int
    total: int
    has_more: bool

