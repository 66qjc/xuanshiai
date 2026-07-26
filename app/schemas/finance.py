"""订单、分成、账本和提现接口契约。"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class CommissionRuleCreate(BaseModel):
    beneficiary_type: Literal["service_matchmaker", "store", "promoter", "partner"]
    name: str = Field(min_length=1, max_length=128)
    mode: Literal["fixed", "rate"]
    fixed_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    rate_percent: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=4)
    priority: int = Field(default=0, ge=0, le=100000)

    @model_validator(mode="after")
    def validate_mode(self) -> "CommissionRuleCreate":
        if self.mode == "fixed" and self.fixed_amount is None:
            raise ValueError("固定金额规则必须填写 fixed_amount")
        if self.mode == "rate" and self.rate_percent is None:
            raise ValueError("比例规则必须填写 rate_percent")
        return self


class CommissionRuleResponse(BaseModel):
    id: int
    beneficiary_type: str
    name: str
    mode: str
    fixed_amount: Decimal | None
    rate_percent: Decimal | None
    priority: int
    version: int
    status: Literal[1, 2]
    created_at: datetime


class ProductCommissionConfigCreate(BaseModel):
    beneficiary_type: Literal["service_matchmaker", "store", "promoter", "partner"]
    mode: Literal["fixed", "rate"]
    fixed_amount: Decimal | None = Field(default=None, ge=0, decimal_places=2)
    rate_percent: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=4)

    @model_validator(mode="after")
    def validate_mode(self) -> "ProductCommissionConfigCreate":
        if self.mode == "fixed" and self.fixed_amount is None:
            raise ValueError("固定金额配置必须填写 fixed_amount")
        if self.mode == "rate" and self.rate_percent is None:
            raise ValueError("比例配置必须填写 rate_percent")
        return self


class ProductCommissionConfigResponse(BaseModel):
    id: int
    product_id: int
    beneficiary_type: str
    mode: str
    fixed_amount: Decimal | None
    rate_percent: Decimal | None
    version: int
    status: Literal[1, 2]
    created_at: datetime


class FinanceOrderCreate(BaseModel):
    product_type: int = Field(ge=1, le=32)
    product_name: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0, le=1000000, decimal_places=2)


class PaymentOrderResponse(BaseModel):
    id: int
    order_no: str
    user_id: int
    product_type: int
    product_name: str
    amount: Decimal
    status: Literal[0, 1, 2, 3]
    pay_time: datetime | None
    created_at: datetime


class CommissionEntryResponse(BaseModel):
    id: int
    order_id: int
    beneficiary_type: str
    beneficiary_id: int
    base_amount: Decimal
    amount: Decimal
    status: str
    created_at: datetime


class FinanceReportRow(BaseModel):
    beneficiary_type: str
    beneficiary_id: int
    order_count: int
    total_amount: Decimal
    pending_amount: Decimal
    available_amount: Decimal


class AccountBalanceResponse(BaseModel):
    account_type: str
    account_id: int
    pending_amount: Decimal
    available_amount: Decimal


class WithdrawalCreate(BaseModel):
    amount: Decimal = Field(gt=0, le=1000000, decimal_places=2)
    payee_masked: str = Field(min_length=1, max_length=128)


class WithdrawalResponse(BaseModel):
    id: int
    account_type: str
    account_id: int
    amount: Decimal
    status: str
    payee_masked: str | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class WithdrawalReview(BaseModel):
    status: Literal["APPROVED", "REJECTED", "PROCESSING", "SUCCEEDED", "FAILED"]
    failure_reason: str | None = Field(default=None, max_length=255)


class FinanceRefundRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=255)
