"""VIP and login-log contracts for member CRM."""

from datetime import datetime

from pydantic import BaseModel


class AdminVipItem(BaseModel):
    membership_id: int
    user_id: int
    nickname: str | None
    phone: str | None
    package_type: str | None
    amount: float | None
    order_no: str | None
    start_at: datetime | None
    end_at: datetime | None
    status: int


class AdminVipPage(BaseModel):
    items: list[AdminVipItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class AdminLoginLogItem(BaseModel):
    id: int
    user_id: int
    nickname: str | None
    login_status: int
    ip: str | None
    device_id: str | None
    platform: str | None
    failure_reason: str | None
    created_at: datetime


class AdminLoginLogPage(BaseModel):
    items: list[AdminLoginLogItem]
    page: int
    page_size: int
    total: int
    has_more: bool

