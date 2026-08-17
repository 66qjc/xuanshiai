"""Contracts for the tenant-scoped administrator home page."""

from datetime import date, datetime
from decimal import Decimal
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class LegacyResponse(BaseModel, Generic[T]):
    code: int = 200
    data: T
    msg: str = "success"
    success: bool = True


class HomeOperator(BaseModel):
    id: int
    account: str
    name: str
    permissions: list[str]
    locked: bool = False


class HomeAuthorization(BaseModel):
    status: str = "active"
    expires_at: datetime | None = None
    sms_remaining_count: int = Field(default=0, ge=0)


class SmsStatistics(BaseModel):
    success_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    remaining_count: int = Field(default=0, ge=0)


class HomeHeader(BaseModel):
    has_unread_feedback: bool = False
    unread_announcement_count: int = Field(default=0, ge=0)
    sms: SmsStatistics


class AdminBootstrap(BaseModel):
    operator: HomeOperator
    authorization: HomeAuthorization
    header: HomeHeader


class DashboardMetrics(BaseModel):
    member_count: int = Field(default=0, ge=0)
    platform_user_count: int = Field(default=0, ge=0)
    wechat_fan_count: int = Field(default=0, ge=0)
    online_days: int = Field(default=0, ge=0)
    lead_count: int = Field(default=0, ge=0)
    customer_lead_count: int = Field(default=0, ge=0)
    vip_count: int = Field(default=0, ge=0)
    online_vip_count: int = Field(default=0, ge=0)
    offline_vip_count: int = Field(default=0, ge=0)
    matchmaker_count: int = Field(default=0, ge=0)
    service_matchmaker_count: int = Field(default=0, ge=0)
    promotion_matchmaker_count: int = Field(default=0, ge=0)
    successful_match_count: int = Field(default=0, ge=0)
    male_member_count: int = Field(default=0, ge=0)
    female_member_count: int = Field(default=0, ge=0)
    pending_withdrawal_count: int = Field(default=0, ge=0)
    online_income: Decimal = Decimal("0.00")
    offline_income: Decimal = Decimal("0.00")


class DashboardPending(BaseModel):
    withdrawal: int = Field(default=0, ge=0)
    matchmaker_application: int = Field(default=0, ge=0)
    matchmaker_service: int = Field(default=0, ge=0)
    match_application: int = Field(default=0, ge=0)
    report: int = Field(default=0, ge=0)


class DashboardGender(BaseModel):
    male: int = Field(default=0, ge=0)
    female: int = Field(default=0, ge=0)
    unspecified: int = Field(default=0, ge=0)


class IncomeRankItem(BaseModel):
    product_type: str
    income: Decimal = Decimal("0.00")
    proportion: Decimal = Decimal("0.00")


class DailyTrend(BaseModel):
    date: date
    member_count: int = Field(default=0, ge=0)
    lead_count: int = Field(default=0, ge=0)
    paid_count: int = Field(default=0, ge=0)
    completed_refund_count: int = Field(default=0, ge=0)
    paid_amount: Decimal = Decimal("0.00")
    online_paid_amount: Decimal = Decimal("0.00")
    offline_paid_amount: Decimal = Decimal("0.00")
    completed_refund_amount: Decimal = Decimal("0.00")
    net_amount: Decimal = Decimal("0.00")


class AdminDashboard(BaseModel):
    from_date: date
    to_date: date
    metrics: DashboardMetrics
    pending: DashboardPending = Field(default_factory=DashboardPending)
    member_gender: DashboardGender = Field(default_factory=DashboardGender)
    income_rank: list[IncomeRankItem] = Field(default_factory=list)
    trends: list[DailyTrend]


class AnnouncementItem(BaseModel):
    id: int
    version_id: int | None = None
    category: str
    title: str
    title_color: str | None = None
    title_bold: bool = False
    top: bool = False
    sort_order: int = 0
    link_to: str | None = None
    created_at: datetime
    read: bool = False


class AnnouncementPage(BaseModel):
    items: list[AnnouncementItem]
    page: int
    page_size: int
    total: int
    has_more: bool


class AcademyCategory(BaseModel):
    id: int
    parent_id: int | None = None
    name: str
    description: str | None = None
    image: str | None = None
    category_type: str = "Guides"
    sort: int = 0
    enabled: bool = True
    matchmaker_class_enabled: bool = False
    children: list["AcademyCategory"] = Field(default_factory=list)


class RechargeItem(BaseModel):
    id: int
    name: str
    resource_type: str
    quantity: int = Field(ge=1)
    price: Decimal
