"""VIP and login-log contracts for member CRM."""

from datetime import datetime

from pydantic import BaseModel
from pydantic import Field, model_validator


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


class AdminVipUpdate(BaseModel):
    action: str = Field(pattern="^(OPEN|RENEW|CANCEL)$")
    package_type: str | None = Field(default=None, min_length=1, max_length=64)
    order_no: str | None = Field(default=None, min_length=8, max_length=64)
    reason: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_action(self) -> "AdminVipUpdate":
        if self.action in ("OPEN", "RENEW") and (not self.package_type or not self.order_no):
            raise ValueError("OPEN/RENEW 必须提供 package_type 和 order_no")
        if self.action == "CANCEL" and not self.reason:
            raise ValueError("CANCEL 必须填写 reason")
        return self


class AdminVipUpdateResponse(BaseModel):
    membership_id: int
    user_id: int
    action: str
    package_type: str | None
    status: int
    start_at: datetime | None
    end_at: datetime | None
    order_no: str | None
    reason: str | None
