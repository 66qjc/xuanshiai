"""一期订单、分成、余额和提现接口。"""

from fastapi import APIRouter, Body, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, CurrentUser, get_current_user, get_current_matchmaker_admin
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
    LedgerEntryPage,
    PaymentOrderAdminPage,
    WithdrawalAdminPage,
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
    admin_list_ledger,
    admin_list_orders,
    admin_list_withdrawals,
)

router = APIRouter(prefix="/finance")
admin_router = APIRouter(prefix="/admin/finance")


def _finance_actor(current: CurrentMatchmakerAdmin) -> CurrentUser:
    return CurrentUser(id=current.account.id, session_id=current.session_id, phone=None, status=1, realname_status=2)


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
async def rule(body: CommissionRuleCreate = Body(...), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CommissionRuleResponse:
    admin.require("finance.write")
    return await create_rule(db, _finance_actor(admin), body)


@admin_router.get("/commission-rules", response_model=list[CommissionRuleResponse], summary="查询分成规则")
async def rules(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[CommissionRuleResponse]:
    admin.require("finance.read")
    return await list_rules(db)


@admin_router.post("/product-commission-rules/{product_id}", response_model=ProductCommissionConfigResponse, status_code=201, summary="配置商品分成对象")
async def product_commission_rule(
    product_id: int = Path(..., ge=1), body: ProductCommissionConfigCreate = Body(...),
    admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db),
) -> ProductCommissionConfigResponse:
    admin.require("finance.write")
    return await create_product_commission_config(db, _finance_actor(admin), product_id, body)


@admin_router.get("/report", response_model=list[FinanceReportRow], summary="查询分成汇总报表")
async def report(admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[FinanceReportRow]:
    admin.require("finance.read")
    return await admin_finance_report(db)


@admin_router.get("/orders", response_model=PaymentOrderAdminPage, summary="后台分页查询订单")
async def admin_orders(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    status: int | None = Query(None, ge=0, le=3), user_id: int | None = Query(None, ge=1),
    order_no: str | None = Query(None, min_length=1, max_length=64),
    admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> PaymentOrderAdminPage:
    admin.require("finance.read")
    return await admin_list_orders(db, page, page_size, status, user_id, order_no)


@admin_router.get("/withdrawals", response_model=WithdrawalAdminPage, summary="后台分页查询提现")
async def admin_withdrawals(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None, max_length=32),
    admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> WithdrawalAdminPage:
    admin.require("finance.read")
    return await admin_list_withdrawals(db, page, page_size, status)


@admin_router.get("/ledger", response_model=LedgerEntryPage, summary="后台分页查询资金流水")
async def admin_ledger(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    account_type: str | None = Query(None, max_length=32),
    account_id: int | None = Query(None, ge=1),
    admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> LedgerEntryPage:
    admin.require("finance.read")
    return await admin_list_ledger(db, page, page_size, account_type, account_id)


@admin_router.post("/orders/{order_id}/settle", response_model=list[CommissionEntryResponse], summary="结算已支付订单分成")
async def settle(order_id: int = Path(..., ge=1), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[CommissionEntryResponse]:
    admin.require("finance.write")
    return await mark_order_paid_and_settle(db, _finance_actor(admin), order_id)


@admin_router.post("/orders/{order_id}/refund", status_code=204, summary="退款并冲正分成")
async def refund(order_id: int = Path(..., ge=1), body: FinanceRefundRequest = Body(...), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> None:
    admin.require("finance.write")
    await refund_order(db, _finance_actor(admin), order_id, body)


@admin_router.post("/commission-entries/{entry_id}/release", response_model=CommissionEntryResponse, summary="释放待结算分成")
async def release(entry_id: int = Path(..., ge=1), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CommissionEntryResponse:
    admin.require("finance.write")
    return await release_commission(db, _finance_actor(admin), entry_id)


@admin_router.patch("/withdrawals/{withdrawal_id}", response_model=WithdrawalResponse, summary="审核提现")
async def review(withdrawal_id: int = Path(..., ge=1), body: WithdrawalReview = Body(...), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> WithdrawalResponse:
    admin.require("finance.write")
    return await review_withdrawal(db, _finance_actor(admin), withdrawal_id, body)
