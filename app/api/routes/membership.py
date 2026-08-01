from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.membership import CreateMembershipOrderRequest, MembershipHistoryPage, MembershipOrderResponse, MembershipPackage, MembershipStatus, WechatPaymentCallback
from app.schemas.payments import TestPaymentRequest, TestPaymentResponse
from app.services.membership import create_order, get_order, get_status, handle_wechat_callback, history, list_packages
from app.services.payments import complete_test_payment

router = APIRouter()

@router.get("/membership/packages", response_model=list[MembershipPackage], summary="查询在售会员套餐")
async def packages(db: AsyncSession = Depends(get_db)) -> list[MembershipPackage]: return await list_packages(db)

@router.get("/users/me/membership", response_model=MembershipStatus, summary="查询当前会员状态")
async def status(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> MembershipStatus: return await get_status(db, current.id)

@router.get("/users/me/membership/history", response_model=MembershipHistoryPage, summary="查询会员历史")
async def membership_history(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> MembershipHistoryPage: return await history(db, current.id, page, page_size)

@router.post("/membership/orders", response_model=MembershipOrderResponse, summary="创建会员测试支付订单")
async def order(body: CreateMembershipOrderRequest, idempotency_key: str | None = Header(None, alias="Idempotency-Key"), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> MembershipOrderResponse: return await create_order(db, current.id, body, idempotency_key)

@router.get("/membership/orders/{order_no}", response_model=MembershipOrderResponse, summary="查询会员订单")
async def order_detail(order_no: str, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> MembershipOrderResponse: return await get_order(db, current.id, order_no)


@router.post("/payments/wechat/callback", status_code=204, summary="微信支付回调")
async def wechat_callback(body: WechatPaymentCallback, db: AsyncSession = Depends(get_db)) -> None:
    await handle_wechat_callback(db, body)


@router.post("/payments/test/pay", response_model=TestPaymentResponse, summary="执行测试支付并履约")
async def test_pay(body: TestPaymentRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> TestPaymentResponse:
    """Development/testing only; no external payment provider is called."""
    return await complete_test_payment(db, current.id, body)
