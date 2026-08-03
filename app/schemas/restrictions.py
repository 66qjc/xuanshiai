"""User safety restriction contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


RestrictionType = Literal[
    "TOTAL_BAN",
    "POST_RESTRICTED",
    "COMMENT_RESTRICTED",
    "MESSAGE_RESTRICTED",
    "APPLICATION_RESTRICTED",
]


class RestrictionCreate(BaseModel):
    restriction_type: RestrictionType
    reason_code: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=255)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_time_order(self) -> "RestrictionCreate":
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("限制结束时间必须晚于开始时间")
        return self


class RestrictionResponse(BaseModel):
    id: int
    user_id: int
    restriction_type: RestrictionType
    reason_code: str
    reason: str
    starts_at: datetime
    ends_at: datetime | None
    status: Literal[1, 2]
    ended_at: datetime | None
    created_by: int
    note: str | None
    created_at: datetime


class RestrictionPage(BaseModel):
    items: list[RestrictionResponse]
    page: int
    page_size: int
    total: int
    has_more: bool
