"""Independent authentication and management contracts for the matchmaker back office."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.organization import ResourceAssignmentResponse


class MatchmakerAdminLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class MatchmakerAdminAccount(BaseModel):
    id: int
    username: str
    display_name: str
    matchmaker_user_id: int | None
    data_scope: Literal["SELF", "STORE", "ORGANIZATION", "ALL"] = "SELF"
    organization_id: int | None = None
    status: Literal[1, 2]
    last_login_at: datetime | None


class MatchmakerAdminTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    account: MatchmakerAdminAccount


class MatchmakerAdminRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=512)


class MatchmakerAdminMeResponse(BaseModel):
    account: MatchmakerAdminAccount
    permissions: list[str]


class MatchmakerStatusUpdate(BaseModel):
    status: Literal[1, 2]
    reason: str | None = Field(default=None, max_length=255)


class MatchmakerStatusResponse(BaseModel):
    matchmaker_id: int
    status: Literal[1, 2]
    reason: str | None


class MatchmakerStatistics(BaseModel):
    total: int
    available: int
    pending_services: int
    active_services: int
    completed_services: int
    cancelled_services: int


class ResourceAssignmentPage(BaseModel):
    items: list[ResourceAssignmentResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class RewardRule(BaseModel):
    id: int
    task_code: str
    task_name: str
    task_type: Literal[1, 2, 3]
    reward_type: Literal[1, 2, 3, 4, 5]
    reward_value: int
    daily_limit: int
    is_active: Literal[0, 1]
    sort: int
    created_at: datetime | None
    updated_at: datetime | None


class RewardRuleCreate(BaseModel):
    task_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    task_name: str = Field(min_length=1, max_length=64)
    task_type: Literal[1, 2, 3] = 1
    reward_type: Literal[1, 2, 3, 4, 5] = 1
    reward_value: int = Field(default=0, ge=0, le=2147483647)
    daily_limit: int = Field(default=0, ge=0, le=2147483647)
    is_active: Literal[0, 1] = 1
    sort: int = Field(default=0, ge=0, le=2147483647)


class RewardRuleUpdate(BaseModel):
    task_name: str | None = Field(default=None, min_length=1, max_length=64)
    task_type: Literal[1, 2, 3] | None = None
    reward_type: Literal[1, 2, 3, 4, 5] | None = None
    reward_value: int | None = Field(default=None, ge=0, le=2147483647)
    daily_limit: int | None = Field(default=None, ge=0, le=2147483647)
    is_active: Literal[0, 1] | None = None
    sort: int | None = Field(default=None, ge=0, le=2147483647)


class RewardRuleDeleteResponse(BaseModel):
    task_code: str
    deleted: bool
