"""Independent back-office contracts for stores and resource assignments."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StoreAdminItem(BaseModel):
    id: int
    code: str
    name: str
    display_name: str | None
    region_code: str | None
    status: Literal[1, 2, 3]
    auto_redirect: bool
    created_at: datetime
    updated_at: datetime


class StoreAdminPage(BaseModel):
    items: list[StoreAdminItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class StoreMemberAdminPage(BaseModel):
    items: list["StoreMemberAdminItem"]
    page: int
    page_size: int
    total: int
    has_more: bool


class AssignmentAdminPage(BaseModel):
    items: list["AssignmentAdminItem"]
    page: int
    page_size: int
    total: int
    has_more: bool
    page: int
    page_size: int
    total: int
    has_more: bool


class StoreAdminUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)
    region_code: str | None = Field(default=None, max_length=64)
    auto_redirect: bool | None = None


class StoreStatusUpdate(BaseModel):
    status: Literal[1, 2, 3]
    reason: str | None = Field(default=None, max_length=255)


class StoreMemberAdminItem(BaseModel):
    id: int
    organization_id: int
    user_id: int
    nickname: str | None
    phone_masked: str | None
    role_code: str
    status: Literal[1, 2, 3]
    started_at: datetime
    ended_at: datetime | None


class StoreReport(BaseModel):
    store_id: int
    active_member_count: int
    active_assignment_count: int
    total_assignment_count: int


class AssignmentAdminItem(BaseModel):
    id: int
    user_id: int
    nickname: str | None
    organization_id: int | None
    organization_name: str | None
    matchmaker_id: int | None
    matchmaker_name: str | None
    source: str
    status: Literal[1, 2]
    effective_at: datetime
    ended_at: datetime | None
    end_reason: str | None


class AssignmentAdminUpdate(BaseModel):
    organization_id: int | None = Field(default=None, ge=1)
    matchmaker_id: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=255)
