"""Paid discovery products using the local test payment provider."""

from fastapi import APIRouter, Depends, Header, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.payments import (
    BoostPackage,
    BoostStatus,
    CreateBoostOrderRequest,
    PaidOrderResponse,
    SpotlightPaymentRequest,
)
from app.services.payments import (
    create_boost_order,
    create_spotlight_order,
    get_boost_status,
    get_paid_order,
    list_boost_packages,
)

router = APIRouter()


@router.get("/boost/packages", response_model=list[BoostPackage], summary="查询置顶套餐")
async def boost_packages() -> list[BoostPackage]:
    return await list_boost_packages()


@router.get("/users/me/boost/status", response_model=BoostStatus, summary="查询我的置顶状态")
async def boost_status(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> BoostStatus:
    return await get_boost_status(db, current.id)


@router.post("/boost/orders", response_model=PaidOrderResponse, status_code=201, summary="创建置顶测试支付订单")
async def boost_order(
    body: CreateBoostOrderRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaidOrderResponse:
    return await create_boost_order(db, current.id, body, idempotency_key)


@router.get("/boost/orders/{order_no}", response_model=PaidOrderResponse, summary="查询置顶订单")
async def boost_order_detail(order_no: str = Path(..., min_length=8, max_length=64), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> PaidOrderResponse:
    return await get_paid_order(db, current.id, order_no, 4)


@router.post("/spotlights/payments", response_model=PaidOrderResponse, status_code=201, summary="创建爆灯测试支付订单")
async def spotlight_order(
    body: SpotlightPaymentRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaidOrderResponse:
    return await create_spotlight_order(db, current.id, body, idempotency_key)


@router.get("/spotlights/payments/{order_no}", response_model=PaidOrderResponse, summary="查询爆灯支付订单")
async def spotlight_order_detail(order_no: str = Path(..., min_length=8, max_length=64), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> PaidOrderResponse:
    return await get_paid_order(db, current.id, order_no, 2)
