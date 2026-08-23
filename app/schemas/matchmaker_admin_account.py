"""Contracts for matchmaker back-office account administration."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MatchmakerAdminAccountItem(BaseModel):
    id: int
    username: str
    display_name: str
    matchmaker_user_id: int | None
    data_scope: Literal["SELF", "STORE", "ORGANIZATION", "ALL"] = "SELF"
    organization_id: int | None = None
    status: Literal[1, 2, 3]
    failed_count: int
    locked_until: datetime | None
    last_login_at: datetime | None
    last_login_ip: str | None
    permissions: list[str]
    created_at: datetime
    updated_at: datetime


class MatchmakerAdminAccountPage(BaseModel):
    items: list[MatchmakerAdminAccountItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class MatchmakerAdminAccountCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    matchmaker_user_id: int | None = Field(default=None, ge=1)
    data_scope: Literal["SELF", "STORE", "ORGANIZATION", "ALL"] = "SELF"
    organization_id: int | None = Field(default=None, ge=1)
    permissions: list[str] = Field(default_factory=list, max_length=50)


class MatchmakerAdminAccountUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    matchmaker_user_id: int | None = Field(default=None, ge=1)
    data_scope: Literal["SELF", "STORE", "ORGANIZATION", "ALL"] | None = None
    organization_id: int | None = Field(default=None, ge=1)
    permissions: list[str] | None = Field(default=None, max_length=50)


class MatchmakerAdminAccountStatusUpdate(BaseModel):
    status: Literal[1, 2, 3]
    reason: str = Field(min_length=1, max_length=255)


class MatchmakerAdminPasswordReset(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
    reason: str = Field(min_length=1, max_length=255)


class MatchmakerAdminSessionItem(BaseModel):
    id: int
    account_id: int
    ip: str | None
    user_agent: str | None
    access_expire_at: datetime
    refresh_expire_at: datetime
    last_used_at: datetime
    status: Literal[1, 2, 3]
    revoked_at: datetime | None


class MatchmakerAdminSessionPage(BaseModel):
    items: list[MatchmakerAdminSessionItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class MatchmakerAdminLoginLogItem(BaseModel):
    id: int
    account_id: int | None
    username: str
    login_status: Literal[0, 1]
    ip: str | None
    user_agent: str | None
    device_id: str | None
    failure_reason: str | None
    created_at: datetime


class MatchmakerAdminLoginLogPage(BaseModel):
    items: list[MatchmakerAdminLoginLogItem]
    page: int
    page_size: int
    total: int
    has_more: bool
