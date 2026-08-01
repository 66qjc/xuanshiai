"""Payment contracts for development/testing payment flows."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class TestPaymentRequest(BaseModel):
    order_no: str = Field(min_length=8, max_length=64)
    success: bool = True
    transaction_id: str | None = Field(default=None, min_length=1, max_length=64)


class TestPaymentResponse(BaseModel):
    order_no: str
    status: int
    transaction_id: str | None
    payment_required: bool
    fulfilled: bool


class PaidOrderResponse(BaseModel):
    order_no: str
    product_code: str
    product_name: str
    amount: Decimal
    status: int
    expire_at: datetime | None
    payment_required: bool


class BoostPackage(BaseModel):
    code: str
    name: str
    days: int
    price: Decimal
    recommended: bool


class CreateBoostOrderRequest(BaseModel):
    package_code: str = Field(min_length=1, max_length=32)


class BoostStatus(BaseModel):
    active: bool
    remaining_days: int
    expires_at: datetime | None
    order_no: str | None


class SpotlightPaymentRequest(BaseModel):
    target_user_id: int = Field(ge=1)
