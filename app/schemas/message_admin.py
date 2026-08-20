from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

class AdminMessageItem(BaseModel):
    id: int
    session_id: int
    from_user_id: int
    to_user_id: int
    type: int
    content: str | None
    media_url: str | None
    is_read: bool
    revoked_at: datetime | None
    created_at: datetime

class AdminMessagePage(BaseModel):
    items: list[AdminMessageItem]
    page: int
    page_size: int
    total: int
    has_more: bool

class AdminMessageModerationRequest(BaseModel):
    action: Literal["recall", "restore"]
    reason: str = Field(min_length=1, max_length=255)

class AdminAnnouncementCreate(BaseModel):
    category: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    link_to: str | None = Field(default=None, max_length=500)
    published_at: datetime | None = None

class AdminAnnouncementItem(BaseModel):
    id: int
    category: str
    title: str
    link_to: str | None
    published_at: datetime | None
    created_at: datetime
