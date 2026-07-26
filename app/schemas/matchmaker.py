"""Request and response contracts for the first-phase matchmaker module."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class MatchmakerCard(BaseModel):
    user_id: int
    nickname: str | None
    avatar: str | None
    application_type: Literal["service_matchmaker"]
    intro: str
    certification_tags: list[str]
    success_count: int
    rating_score: float
    rating_count: int
    is_available: bool


class MatchmakerPage(BaseModel):
    items: list[MatchmakerCard]
    page: int
    page_size: int
    total: int
    has_more: bool


class MatchmakerServiceProductCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=128)
    service_type: Literal[1, 3]
    price: Decimal = Field(gt=0, le=1000000, decimal_places=2)
    description: str = Field(min_length=1, max_length=2000)
    active: bool = True


class MatchmakerServiceProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    service_type: Literal[1, 3] | None = None
    price: Decimal | None = Field(default=None, gt=0, le=1000000, decimal_places=2)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    active: bool | None = None

    @model_validator(mode="after")
    def require_update(self) -> "MatchmakerServiceProductUpdate":
        if all(value is None for value in (self.name, self.service_type, self.price, self.description, self.active)):
            raise ValueError("至少提供一个需要修改的商品字段")
        return self


class MatchmakerServiceProductResponse(BaseModel):
    id: int
    code: str
    name: str
    service_type: Literal[1, 3]
    price: Decimal
    description: str
    active: bool
    created_at: datetime
    updated_at: datetime


class MatchmakerServiceOrderCreate(BaseModel):
    product_id: int = Field(ge=1)
    matchmaker_id: int = Field(ge=1)
    requirement: str = Field(min_length=10, max_length=2000)


class MatchmakerServiceOrderResponse(BaseModel):
    id: int
    order_no: str
    user_id: int
    matchmaker_id: int
    product_id: int
    service_type: Literal[1, 3]
    product_name: str
    amount: Decimal
    status: Literal[0, 1, 2, 3]
    service_request_id: int | None
    created_at: datetime
    pay_time: datetime | None


class MatchmakerServiceRequestCreate(BaseModel):
    order_no: str = Field(min_length=8, max_length=64)
    requirement: str = Field(min_length=10, max_length=2000)


class MatchmakerServiceRequestUpdate(BaseModel):
    status: Literal[1, 2, 3]
    feedback: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_feedback(self) -> "MatchmakerServiceRequestUpdate":
        if self.status in (2, 3) and not self.feedback:
            raise ValueError("完成或取消服务时必须填写处理说明")
        return self


class MatchmakerServiceRequestResponse(BaseModel):
    id: int
    user_id: int
    matchmaker_id: int | None
    service_type: Literal[1, 3]
    status: Literal[0, 1, 2, 3]
    order_id: int
    product_id: int
    requirement: str
    feedback: str | None
    created_at: datetime
    updated_at: datetime
    start_at: datetime | None
    end_at: datetime | None


class MatchmakerContactUpdate(BaseModel):
    wechat_contact: str = Field(min_length=1, max_length=128)


class MatchmakerContactResponse(BaseModel):
    service_id: int
    matchmaker_id: int
    wechat_contact: str
    delivered_at: datetime


class MatchmakerContactExchangeCreate(BaseModel):
    target_user_id: int = Field(ge=1)


class MatchmakerContactExchangeUpdate(BaseModel):
    action: Literal["CONSENT", "REVOKE"]


class MatchmakerContactExchangeResponse(BaseModel):
    id: int
    service_id: int
    source_user_id: int
    target_user_id: int
    status: Literal["PENDING", "ONE_SIDE_CONSENT", "APPROVED", "DELIVERED", "REVOKED", "HIDDEN"]
    source_consented_at: datetime | None
    target_consented_at: datetime | None
    delivered_at: datetime | None
    hidden_at: datetime | None
    hidden_reason: str | None
    created_at: datetime
    updated_at: datetime


class MatchmakerServiceRequestPage(BaseModel):
    items: list[MatchmakerServiceRequestResponse]
    page: int
    page_size: int
    total: int
    has_more: bool


class MatchmakerAdminServiceRequestUpdate(BaseModel):
    matchmaker_id: int | None = Field(default=None, ge=1)
    status: Literal[0, 1, 2, 3] | None = None
    feedback: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_update(self) -> "MatchmakerAdminServiceRequestUpdate":
        if self.matchmaker_id is None and self.status is None and self.feedback is None:
            raise ValueError("至少提供一个需要修改的字段")
        if self.status in (2, 3) and not self.feedback:
            raise ValueError("完成或取消服务时必须填写处理说明")
        return self
