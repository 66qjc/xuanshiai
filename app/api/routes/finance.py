"""一期订单、分成、余额和提现接口。"""

from fastapi import APIRouter, Body, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_admin, get_current_user
from app.db.session import get_db
from app.schemas.finance import (
    AccountBalanceResponse,
    CommissionEntryResponse,
    CommissionRuleCreate,
    CommissionRuleResponse,
    FinanceOrderCreate,
    FinanceReportRow,
    FinanceRefundRequest,
    ProductCommissionConfigCreate,
    ProductCommissionConfigResponse,
    PaymentOrderResponse,
    WithdrawalCreate,
    WithdrawalResponse,
    WithdrawalReview,
)
from app.services.finance import (
    create_order,
    create_rule,
    get_balance,
    list_rules,
    list_user_commissions,
    admin_finance_report,
    refund_order,
    release_commission,
    mark_order_paid_and_settle,
    request_withdrawal,
    review_withdrawal,
    create_product_commission_config,
)

router = APIRouter(prefix="/finance")
admin_router = APIRouter(prefix="/admin/finance")


@router.post("/orders", response_model=PaymentOrderResponse, status_code=201, summary="创建待支付订单")
async def order(body: FinanceOrderCreate = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> PaymentOrderResponse:
    return await create_order(db, current, body)


@router.get("/balance", response_model=AccountBalanceResponse, summary="查询我的余额")
async def balance(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AccountBalanceResponse:
    return await get_balance(db, "user", current.id)


@router.get("/commission-entries", response_model=list[CommissionEntryResponse], summary="查询我的分成明细")
async def commission_entries(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[CommissionEntryResponse]:
    return await list_user_commissions(db, current)


@router.post("/withdrawals", response_model=WithdrawalResponse, status_code=201, summary="申请提现")
async def withdrawal(body: WithdrawalCreate = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> WithdrawalResponse:
    return await request_withdrawal(db, current, body)


@admin_router.post("/commission-rules", response_model=CommissionRuleResponse, status_code=201, summary="创建分成规则")
async def rule(body: CommissionRuleCreate = Body(...), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> CommissionRuleResponse:
    return await create_rule(db, admin, body)


@admin_router.get("/commission-rules", response_model=list[CommissionRuleResponse], summary="查询分成规则")
async def rules(admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> list[CommissionRuleResponse]:
    return await list_rules(db)


@admin_router.post("/product-commission-rules/{product_id}", response_model=ProductCommissionConfigResponse, status_code=201, summary="配置商品分成对象")
async def product_commission_rule(
    product_id: int = Path(..., ge=1), body: ProductCommissionConfigCreate = Body(...),
    admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
) -> ProductCommissionConfigResponse:
    return await create_product_commission_config(db, admin, product_id, body)


@admin_router.get("/report", response_model=list[FinanceReportRow], summary="查询分成汇总报表")
async def report(admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> list[FinanceReportRow]:
    return await admin_finance_report(db)


@admin_router.post("/orders/{order_id}/settle", response_model=list[CommissionEntryResponse], summary="结算已支付订单分成")
async def settle(order_id: int = Path(..., ge=1), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> list[CommissionEntryResponse]:
    return await mark_order_paid_and_settle(db, admin, order_id)


@admin_router.post("/orders/{order_id}/refund", status_code=204, summary="退款并冲正分成")
async def refund(order_id: int = Path(..., ge=1), body: FinanceRefundRequest = Body(...), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> None:
    await refund_order(db, admin, order_id, body)


@admin_router.post("/commission-entries/{entry_id}/release", response_model=CommissionEntryResponse, summary="释放待结算分成")
async def release(entry_id: int = Path(..., ge=1), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> CommissionEntryResponse:
    return await release_commission(db, admin, entry_id)


@admin_router.patch("/withdrawals/{withdrawal_id}", response_model=WithdrawalResponse, summary="审核提现")
async def review(withdrawal_id: int = Path(..., ge=1), body: WithdrawalReview = Body(...), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> WithdrawalResponse:
    return await review_withdrawal(db, admin, withdrawal_id, body)
