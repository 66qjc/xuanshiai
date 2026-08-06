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
