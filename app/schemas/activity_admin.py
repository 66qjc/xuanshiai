"""Activity management contracts for the back office."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ActivityAdminCreate(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    cover: str | None = Field(default=None, max_length=255)
    type: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=255)
    start_time: datetime
    end_time: datetime
    signup_deadline: datetime | None = None
    max_people: int = Field(default=0, ge=0, le=100000)
    price: float = Field(default=0, ge=0, le=1000000)
    description: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def validate_times(self) -> "ActivityAdminCreate":
        if self.end_time <= self.start_time:
            raise ValueError("end_time 必须晚于 start_time")
        if self.signup_deadline and self.signup_deadline > self.start_time:
            raise ValueError("signup_deadline 不能晚于 start_time")
        return self


class ActivityAdminUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    cover: str | None = Field(default=None, max_length=255)
    type: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=64)
    address: str | None = Field(default=None, max_length=255)
    start_time: datetime | None = None
    end_time: datetime | None = None
    signup_deadline: datetime | None = None
    max_people: int | None = Field(default=None, ge=0, le=100000)
    price: float | None = Field(default=None, ge=0, le=1000000)
    description: str | None = Field(default=None, max_length=10000)

    @model_validator(mode="after")
    def require_update(self) -> "ActivityAdminUpdate":
        if not self.model_dump(exclude_unset=True):
            raise ValueError("至少提供一个需要修改的字段")
        return self


class ActivityStatusUpdate(BaseModel):
    status: Literal[1, 2, 3, 4, 5]
    reason: str | None = Field(default=None, max_length=255)


class ActivityAdminItem(BaseModel):
    id: int
    title: str
    cover: str | None
    type: str | None
    city: str | None
    address: str | None
    start_time: datetime
    end_time: datetime
    signup_deadline: datetime | None
    max_people: int
    current_people: int
    price: float
    status: int
    description: str | None
    created_by: int | None
    created_at: datetime


class ActivityAdminPage(BaseModel):
    items: list[ActivityAdminItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class ActivitySignupAdminItem(BaseModel):
    id: int
    activity_id: int
    user_id: int
    nickname: str | None
    real_name: str | None
    phone: str | None
    remark: str | None
    status: Literal[0, 1, 2, 3]
    cancel_reason: str | None
    created_at: datetime
    updated_at: datetime


class ActivitySignupAdminPage(BaseModel):
    items: list[ActivitySignupAdminItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class ActivitySignupStatusUpdate(BaseModel):
    status: Literal[1, 2, 3]
    reason: str | None = Field(default=None, max_length=255)

