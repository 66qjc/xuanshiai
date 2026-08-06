"""Member CRM contracts for the independent matchmaker back office."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class MemberListItem(BaseModel):
    id: int
    nickname: str | None
    phone: str | None
    gender: int | None
    status: int
    is_vip: bool
    vip_end_at: datetime | None
    matchmaker_id: int | None
    created_at: datetime


class MemberPage(BaseModel):
    items: list[MemberListItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class MemberDetail(MemberListItem):
    avatar: str | None
    birthday: date | None
    is_married: int | None
    residence_city_code: str | None


class MemberStatusUpdate(BaseModel):
    status: Literal[1, 2, 3]
    reason: str = Field(min_length=1, max_length=255)


class MemberStatusResponse(BaseModel):
    id: int
    status: Literal[1, 2, 3]
    reason: str


class MemberStatistics(BaseModel):
    total: int
    male: int
    female: int
    vip: int
    active: int
