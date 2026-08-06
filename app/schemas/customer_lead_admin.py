"""Customer lead contracts for the back office."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


LeadStatus = Literal["NEW", "CONTACTED", "INTENDED", "CONVERTED", "LOST", "CLOSED"]


class CustomerLeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    wechat: str | None = Field(default=None, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    intention_level: Literal[1, 2, 3] = 1
    remark: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_contact(self) -> "CustomerLeadCreate":
        if not self.phone and not self.wechat:
            raise ValueError("phone 或 wechat 至少提供一个")
        return self


class CustomerLeadUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    wechat: str | None = Field(default=None, max_length=128)
    intention_level: Literal[1, 2, 3] | None = None
    status: LeadStatus | None = None
    remark: str | None = Field(default=None, max_length=2000)
    next_follow_at: datetime | None = None

    @model_validator(mode="after")
    def require_update(self) -> "CustomerLeadUpdate":
        if all(value is None for value in (self.name, self.phone, self.wechat, self.intention_level, self.status, self.remark, self.next_follow_at)):
            raise ValueError("至少提供一个需要修改的字段")
        return self


class CustomerLeadAssignment(BaseModel):
    matchmaker_id: int | None = Field(default=None, ge=1)
    organization_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_owner(self) -> "CustomerLeadAssignment":
        if self.matchmaker_id is None and self.organization_id is None:
            raise ValueError("至少指定红娘或门店")
        return self


class CustomerLeadFollowUpCreate(BaseModel):
    method: Literal["PHONE", "WECHAT", "VISIT", "OTHER"]
    content: str = Field(min_length=1, max_length=2000)
    intention_level: Literal[1, 2, 3] | None = None
    next_follow_at: datetime | None = None


class CustomerLeadFollowUp(BaseModel):
    id: int
    lead_id: int
    method: str
    content: str
    intention_level: int | None
    next_follow_at: datetime | None
    created_by: int
    created_at: datetime


class CustomerLead(BaseModel):
    id: int
    name: str
    phone: str | None
    wechat: str | None
    source: str
    intention_level: Literal[1, 2, 3]
    status: LeadStatus
    matchmaker_id: int | None
    organization_id: int | None
    next_follow_at: datetime | None
    converted_user_id: int | None
    remark: str | None
    created_by: int
    created_at: datetime
    updated_at: datetime


class CustomerLeadPage(BaseModel):
    items: list[CustomerLead]
    page: int
    page_size: int
    total: int
    has_more: bool


class CustomerLeadStatistics(BaseModel):
    total: int
    new_count: int
    contacted_count: int
    intended_count: int
    converted_count: int
    lost_count: int

