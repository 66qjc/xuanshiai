"""一期订单、分成规则、账本和提现服务。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser
from app.core.config import settings
from app.schemas.finance import (
    AccountBalanceResponse,
    CommissionEntryResponse,
    CommissionRuleCreate,
    CommissionRuleResponse,
    FinanceOrderCreate,
    FinanceReportRow,
    FinanceRefundRequest,
    PaymentOrderResponse,
    ProductCommissionConfigCreate,
    ProductCommissionConfigResponse,
    WithdrawalCreate,
    WithdrawalResponse,
    WithdrawalReview,
)
from app.services.matchmaker import activate_paid_service_order


CENT = Decimal("0.01")


def _dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _order(row: Any) -> PaymentOrderResponse:
    return PaymentOrderResponse(**{**dict(row), "pay_time": _dt(row["pay_time"]) if row["pay_time"] else None, "created_at": _dt(row["created_at"])})


def _withdrawal(row: Any) -> WithdrawalResponse:
    return WithdrawalResponse(**{**dict(row), "created_at": _dt(row["created_at"]), "updated_at": _dt(row["updated_at"])})


def _rule(row: Any) -> CommissionRuleResponse:
    return CommissionRuleResponse(**{**dict(row), "created_at": _dt(row["created_at"])})


def _product_commission(row: Any) -> ProductCommissionConfigResponse:
    return ProductCommissionConfigResponse(**{**dict(row), "created_at": _dt(row["created_at"])})


async def create_product_commission_config(
    db: AsyncSession, admin: CurrentUser, product_id: int, request: ProductCommissionConfigCreate
) -> ProductCommissionConfigResponse:
    product = await db.execute(text("SELECT id FROM matchmaker_service_product WHERE id = :id"), {"id": product_id})
    if not product.scalar():
        raise HTTPException(404, detail="红娘服务商品不存在")
    version = int((await db.execute(text("""SELECT COALESCE(MAX(version), 0) + 1
        FROM product_commission_config WHERE product_id = :product_id AND beneficiary_type = :kind"""), {
            "product_id": product_id, "kind": request.beneficiary_type,
        })).scalar() or 1)
    await db.execute(text("""UPDATE product_commission_config SET status = 2
        WHERE product_id = :product_id AND beneficiary_type = :kind AND status = 1"""), {
        "product_id": product_id, "kind": request.beneficiary_type,
    })
    result = await db.execute(text("""INSERT INTO product_commission_config
        (product_id, beneficiary_type, mode, fixed_amount, rate_percent, version, created_by)
        VALUES (:product_id, :kind, :mode, :fixed_amount, :rate_percent, :version, :admin_id)"""), {
        "product_id": product_id, "kind": request.beneficiary_type, "mode": request.mode,
        "fixed_amount": request.fixed_amount, "rate_percent": request.rate_percent,
        "version": version, "admin_id": admin.id,
    })
    await db.commit()
    row = (await db.execute(text("""SELECT id, product_id, beneficiary_type, mode,
        fixed_amount, rate_percent, version, status, created_at
        FROM product_commission_config WHERE id = :id"""), {"id": result.lastrowid})).mappings().one()
    return _product_commission(row)


async def create_rule(db: AsyncSession, admin: CurrentUser, request: CommissionRuleCreate) -> CommissionRuleResponse:
    version_result = await db.execute(text("SELECT COALESCE(MAX(version), 0) + 1 FROM commission_rule WHERE beneficiary_type = :kind"), {"kind": request.beneficiary_type})
    version = int(version_result.scalar() or 1)
    result = await db.execute(text("""INSERT INTO commission_rule
        (beneficiary_type, name, mode, fixed_amount, rate_percent, priority, version, created_by)
        VALUES (:kind, :name, :mode, :fixed_amount, :rate_percent, :priority, :version, :admin_id)"""), {
        "kind": request.beneficiary_type, "name": request.name, "mode": request.mode,
        "fixed_amount": request.fixed_amount, "rate_percent": request.rate_percent,
        "priority": request.priority, "version": version, "admin_id": admin.id,
    })
    rule_id = int(result.lastrowid)
    await db.commit()
    result = await db.execute(text("""SELECT id, beneficiary_type, name, mode, fixed_amount,
        rate_percent, priority, version, status, created_at FROM commission_rule WHERE id = :id"""), {"id": rule_id})
    return _rule(result.mappings().one())


async def create_order(db: AsyncSession, current: CurrentUser, request: FinanceOrderCreate) -> PaymentOrderResponse:
    import secrets

    order_no = f"XS{datetime.utcnow():%Y%m%d%H%M%S}{current.id:08d}{secrets.token_hex(4)}"
    result = await db.execute(text("""INSERT INTO payment_order
        (user_id, order_no, type, product_type, product_name, amount, status, expire_at)
        VALUES (:user_id, :order_no, :type, :product_type, :name, :amount, 0, DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 MINUTE))"""), {
        "user_id": current.id, "order_no": order_no, "type": request.product_type,
        "product_type": request.product_type, "name": request.product_name, "amount": request.amount,
    })
    order_id = int(result.lastrowid)
    await db.commit()
    result = await db.execute(text("""SELECT id, order_no, user_id, product_type, product_name,
        amount, status, pay_time, created_at FROM payment_order WHERE id = :id"""), {"id": order_id})
    return _order(result.mappings().one())


async def list_rules(db: AsyncSession) -> list[CommissionRuleResponse]:
    result = await db.execute(text("""SELECT id, beneficiary_type, name, mode, fixed_amount,
        rate_percent, priority, version, status, created_at FROM commission_rule
        WHERE status = 1 ORDER BY beneficiary_type, priority DESC, id DESC"""))
    return [_rule(row) for row in result.mappings().all()]


async def mark_order_paid_and_settle(db: AsyncSession, admin: CurrentUser, order_id: int) -> list[CommissionEntryResponse]:
    if not settings.is_test_mode:
        raise HTTPException(503, detail="支付成功状态必须由真实支付回调确认")
    result = await db.execute(text("""SELECT id, user_id, amount, status, service_product_id,
        matchmaker_id FROM payment_order
        WHERE id = :id FOR UPDATE"""), {"id": order_id})
    order = result.mappings().first()
    if not order:
        raise HTTPException(404, detail="支付订单不存在")
    if order["status"] == 3:
        raise HTTPException(409, detail="已退款订单不能结算")
    if order["status"] == 0:
        await db.execute(text("""UPDATE payment_order SET status = 1, pay_time = UTC_TIMESTAMP(),
            transaction_id = CONCAT('sandbox-', order_no) WHERE id = :id"""), {"id": order_id})
        order = {**dict(order), "status": 1}
    if order.get("service_product_id"):
        await activate_paid_service_order(db, order_id)
    base = Decimal(str(order["amount"])).quantize(CENT)
    beneficiaries: list[tuple[str, int]] = []
    assignment = await db.execute(text("""SELECT organization_id, matchmaker_id FROM resource_assignment
        WHERE user_id = :user_id AND status = 1 ORDER BY effective_at DESC LIMIT 1"""), {"user_id": order["user_id"]})
    assigned = assignment.mappings().first()
    if assigned and assigned["organization_id"]:
        beneficiaries.append(("store", int(assigned["organization_id"])))
    if order.get("matchmaker_id"):
        beneficiaries.append(("service_matchmaker", int(order["matchmaker_id"])))
    elif assigned and assigned["matchmaker_id"]:
        beneficiaries.append(("service_matchmaker", int(assigned["matchmaker_id"])))
    promotion = await db.execute(text("""SELECT promoter_id FROM promotion_attribution
        WHERE user_id = :user_id AND status = 1 LIMIT 1"""), {"user_id": order["user_id"]})
    promoter_id = promotion.scalar()
    if promoter_id:
        beneficiaries.append(("promoter", int(promoter_id)))
        partner = await db.execute(text("""SELECT pm.team_id, pt.owner_user_id FROM partner_membership pm
            JOIN partner_team pt ON pt.id = pm.team_id AND pt.status = 1
            WHERE pm.promoter_id = :promoter_id AND pm.status = 1 LIMIT 1"""), {"promoter_id": promoter_id})
        team = partner.mappings().first()
        if team:
            beneficiaries.append(("partner", int(team["owner_user_id"])))
    total = Decimal("0.00")
    entries: list[CommissionEntryResponse] = []
    for kind, beneficiary_id in beneficiaries:
        rule_table = "product_commission_config" if order.get("service_product_id") else "commission_rule"
        rule_result = await db.execute(text(f"""SELECT id, mode, fixed_amount, rate_percent, version
            FROM {rule_table} WHERE beneficiary_type = :kind AND status = 1
            {"AND product_id = :product_id" if rule_table == "product_commission_config" else ""}
            ORDER BY version DESC, id DESC LIMIT 1"""), {
                "kind": kind, "product_id": order.get("service_product_id"),
            })
        rule = rule_result.mappings().first()
        if not rule:
            continue
        amount = Decimal(str(rule["fixed_amount"])) if rule["mode"] == "fixed" else base * Decimal(str(rule["rate_percent"])) / Decimal("100")
        amount = amount.quantize(CENT, rounding=ROUND_DOWN)
        if amount <= 0:
            continue
        total += amount
        if total > base:
            await db.rollback()
            raise HTTPException(409, detail="当前分成规则总额超过订单可分成金额")
        key = f"commission:{order_id}:{kind}:{beneficiary_id}"
        await db.execute(text("""INSERT INTO commission_entry
            (order_id, beneficiary_type, beneficiary_id, rule_id, rule_version, base_amount, amount, idempotency_key)
            VALUES (:order_id, :kind, :beneficiary_id, :rule_id, :version, :base, :amount, :key)
            ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)"""), {
            "order_id": order_id, "kind": kind, "beneficiary_id": beneficiary_id,
            "rule_id": rule["id"], "version": rule["version"], "base": base, "amount": amount, "key": key,
        })
        entry_id = int((await db.execute(text("SELECT LAST_INSERT_ID()")).scalar()) or 0)
        await db.execute(text("""INSERT INTO account_ledger
            (account_type, account_id, direction, amount, state, source_type, source_id, idempotency_key)
            VALUES (:account_type, :account_id, 'CREDIT', :amount, 'PENDING', 'commission', :source_id, :key)
            ON DUPLICATE KEY UPDATE id = id"""), {
            "account_type": "store" if kind == "store" else "user", "account_id": beneficiary_id,
            "amount": amount, "source_id": entry_id, "key": f"ledger:commission:{entry_id}",
        })
        entry_result = await db.execute(text("""SELECT id, order_id, beneficiary_type, beneficiary_id,
            base_amount, amount, status, created_at FROM commission_entry WHERE id = :id"""), {"id": entry_id})
        entry = entry_result.mappings().one()
        entries.append(CommissionEntryResponse(**{**dict(entry), "created_at": _dt(entry["created_at"]) }))
    await db.commit()
    return entries


async def list_user_commissions(db: AsyncSession, current: CurrentUser) -> list[CommissionEntryResponse]:
    result = await db.execute(text("""SELECT id, order_id, beneficiary_type, beneficiary_id,
        base_amount, amount, status, created_at FROM commission_entry
        WHERE beneficiary_id = :user_id AND beneficiary_type IN ('service_matchmaker', 'promoter', 'partner')
        ORDER BY created_at DESC, id DESC LIMIT 200"""), {"user_id": current.id})
    return [CommissionEntryResponse(**{**dict(row), "created_at": _dt(row["created_at"])}) for row in result.mappings().all()]


async def admin_finance_report(db: AsyncSession) -> list[FinanceReportRow]:
    result = await db.execute(text("""SELECT ce.beneficiary_type, ce.beneficiary_id,
        COUNT(DISTINCT ce.order_id) AS order_count, COALESCE(SUM(ce.amount), 0) AS total_amount,
        COALESCE(SUM(CASE WHEN ce.status = 'PENDING' THEN ce.amount ELSE 0 END), 0) AS pending_amount,
        COALESCE(SUM(CASE WHEN ce.status = 'AVAILABLE' THEN ce.amount ELSE 0 END), 0) AS available_amount
        FROM commission_entry ce GROUP BY ce.beneficiary_type, ce.beneficiary_id
        ORDER BY total_amount DESC"""))
    return [FinanceReportRow(**dict(row)) for row in result.mappings().all()]


async def release_commission(db: AsyncSession, admin: CurrentUser, entry_id: int) -> CommissionEntryResponse:
    result = await db.execute(text("""SELECT id, order_id, beneficiary_type, beneficiary_id,
        base_amount, amount, status, created_at FROM commission_entry WHERE id = :id FOR UPDATE"""), {"id": entry_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="分成明细不存在")
    if row["status"] != "PENDING":
        raise HTTPException(409, detail="当前分成明细不能转为可用")
    await db.execute(text("UPDATE commission_entry SET status = 'AVAILABLE' WHERE id = :id"), {"id": entry_id})
    await db.execute(text("""UPDATE account_ledger SET state = 'AVAILABLE'
        WHERE source_type = 'commission' AND source_id = :entry_id AND direction = 'CREDIT'"""), {"entry_id": entry_id})
    await db.commit()
    result = await db.execute(text("""SELECT id, order_id, beneficiary_type, beneficiary_id,
        base_amount, amount, status, created_at FROM commission_entry WHERE id = :id"""), {"id": entry_id})
    row = result.mappings().one()
    return CommissionEntryResponse(**{**dict(row), "created_at": _dt(row["created_at"])})


async def refund_order(db: AsyncSession, admin: CurrentUser, order_id: int, request: FinanceRefundRequest) -> None:
    result = await db.execute(text("""SELECT id, status, service_request_id
        FROM payment_order WHERE id = :id FOR UPDATE"""), {"id": order_id})
    order = result.mappings().first()
    if not order:
        raise HTTPException(404, detail="支付订单不存在")
    if order["status"] != 1:
        raise HTTPException(409, detail="只有支付成功订单才能退款")
    entries = await db.execute(text("""SELECT id, beneficiary_type, beneficiary_id, amount, status
        FROM commission_entry WHERE order_id = :order_id AND status <> 'REVERSED' FOR UPDATE"""), {"order_id": order_id})
    for entry in entries.mappings().all():
        await db.execute(text("UPDATE commission_entry SET status = 'REVERSED' WHERE id = :id"), {"id": entry["id"]})
        state = "AVAILABLE" if entry["status"] == "AVAILABLE" else "PENDING"
        account_type = "store" if entry["beneficiary_type"] == "store" else "user"
        await db.execute(text("""INSERT INTO account_ledger
            (account_type, account_id, direction, amount, state, source_type, source_id, idempotency_key)
            VALUES (:account_type, :account_id, 'DEBIT', :amount, :state, 'commission_refund', :source_id, :key)
            ON DUPLICATE KEY UPDATE id = id"""), {
            "account_type": account_type, "account_id": entry["beneficiary_id"], "amount": entry["amount"],
            "state": state, "source_id": entry["id"], "key": f"ledger:commission-refund:{entry['id']}",
        })
    service_result = await db.execute(text("""SELECT id FROM matchmaker_service
        WHERE order_id = :order_id FOR UPDATE"""), {"order_id": order_id})
    for service_row in service_result.mappings().all():
        service_id = int(service_row["id"])
        await db.execute(text("""UPDATE matchmaker_service SET status = 3,
            feedback = CONCAT(COALESCE(feedback, ''), '\n退款关闭：', :reason),
            end_at = COALESCE(end_at, UTC_TIMESTAMP()), updated_at = UTC_TIMESTAMP()
            WHERE id = :service_id AND status <> 3"""), {"service_id": service_id, "reason": request.reason})
        await db.execute(text("""UPDATE matchmaker_contact_exchange SET status = 'HIDDEN',
            hidden_at = UTC_TIMESTAMP(), hidden_reason = '订单退款', updated_at = UTC_TIMESTAMP()
            WHERE service_id = :service_id AND status NOT IN ('REVOKED', 'HIDDEN')"""), {"service_id": service_id})
        await db.execute(text("""UPDATE meeting_request SET status = 'CLOSED', updated_at = UTC_TIMESTAMP()
            WHERE service_id = :service_id AND status IN ('SUBMITTED', 'CONTACTED', 'ACCEPTED')"""), {"service_id": service_id})
        await db.execute(text("""UPDATE meeting_record mr JOIN meeting_request rq ON rq.id = mr.request_id
            SET mr.status = 'CANCELLED', mr.cancel_reason = '关联红娘服务已退款', mr.updated_at = UTC_TIMESTAMP()
            WHERE rq.service_id = :service_id AND mr.status IN ('SCHEDULED', 'REMINDED')"""), {"service_id": service_id})
        await db.execute(text("""INSERT INTO business_audit_log
            (actor_user_id, action, resource_type, resource_id, reason)
            VALUES (:admin_id, 'matchmaker_service.refund_close', 'matchmaker_service', :service_id, :reason)"""), {
            "admin_id": admin.id, "service_id": service_id, "reason": request.reason,
        })
    await db.execute(text("UPDATE payment_order SET status = 3, refund_time = UTC_TIMESTAMP() WHERE id = :id"), {"id": order_id})
    await db.commit()


async def get_balance(db: AsyncSession, account_type: str, account_id: int) -> AccountBalanceResponse:
    result = await db.execute(text("""SELECT
        COALESCE(SUM(CASE WHEN state = 'PENDING' AND direction = 'CREDIT' THEN amount WHEN state = 'PENDING' AND direction = 'DEBIT' THEN -amount ELSE 0 END), 0) AS pending_amount,
        COALESCE(SUM(CASE WHEN state = 'AVAILABLE' AND direction = 'CREDIT' THEN amount WHEN state = 'AVAILABLE' AND direction = 'DEBIT' THEN -amount ELSE 0 END), 0) AS available_amount
        FROM account_ledger WHERE account_type = :account_type AND account_id = :account_id"""), {"account_type": account_type, "account_id": account_id})
    row = result.mappings().one()
    return AccountBalanceResponse(account_type=account_type, account_id=account_id, pending_amount=row["pending_amount"], available_amount=row["available_amount"])


async def request_withdrawal(db: AsyncSession, current: CurrentUser, request: WithdrawalCreate) -> WithdrawalResponse:
    # Serialize balance checks with other withdrawals for the same account.
    await db.execute(text("""SELECT id FROM account_ledger
        WHERE account_type = 'user' AND account_id = :user_id FOR UPDATE"""), {"user_id": current.id})
    balance = await get_balance(db, "user", current.id)
    if Decimal(str(balance.available_amount)) < request.amount:
        raise HTTPException(409, detail="可提现余额不足")
    result = await db.execute(text("""INSERT INTO withdrawal_request
        (account_type, account_id, amount, payee_masked) VALUES ('user', :user_id, :amount, :payee)"""), {
        "user_id": current.id, "amount": request.amount, "payee": request.payee_masked,
    })
    withdrawal_id = int(result.lastrowid)
    await db.execute(text("""INSERT INTO account_ledger
        (account_type, account_id, direction, amount, state, source_type, source_id, idempotency_key)
        VALUES ('user', :user_id, 'DEBIT', :amount, 'AVAILABLE', 'withdrawal', :source_id, :key)"""), {
        "user_id": current.id, "amount": request.amount, "source_id": withdrawal_id, "key": f"ledger:withdrawal:{withdrawal_id}",
    })
    await db.commit()
    result = await db.execute(text("""SELECT id, account_type, account_id, amount, status,
        payee_masked, failure_reason, created_at, updated_at FROM withdrawal_request WHERE id = :id"""), {"id": withdrawal_id})
    return _withdrawal(result.mappings().one())


async def review_withdrawal(db: AsyncSession, admin: CurrentUser, withdrawal_id: int, request: WithdrawalReview) -> WithdrawalResponse:
    result = await db.execute(text("""SELECT id, account_type, account_id, amount, status,
        payee_masked, failure_reason, created_at, updated_at FROM withdrawal_request WHERE id = :id FOR UPDATE"""), {"id": withdrawal_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="提现申请不存在")
    allowed = {
        "PENDING_REVIEW": {"APPROVED", "REJECTED"},
        "APPROVED": {"PROCESSING", "FAILED", "REJECTED"},
        "PROCESSING": {"SUCCEEDED", "FAILED"},
    }
    if request.status not in allowed.get(row["status"], set()):
        raise HTTPException(409, detail="提现申请已经结束")
    if request.status in ("REJECTED", "FAILED"):
        await db.execute(text("""INSERT INTO account_ledger
            (account_type, account_id, direction, amount, state, source_type, source_id, idempotency_key)
            VALUES (:account_type, :account_id, 'CREDIT', :amount, 'AVAILABLE', 'withdrawal_reversal', :source_id, :key)
            ON DUPLICATE KEY UPDATE id = id"""), {
            "account_type": row["account_type"], "account_id": row["account_id"], "amount": row["amount"],
            "source_id": withdrawal_id, "key": f"ledger:withdrawal-reversal:{withdrawal_id}",
        })
    await db.execute(text("""UPDATE withdrawal_request SET status = :status,
        failure_reason = :reason, reviewed_by = :admin_id, reviewed_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP()
        WHERE id = :id"""), {"status": request.status, "reason": request.failure_reason, "admin_id": admin.id, "id": withdrawal_id})
    await db.commit()
    result = await db.execute(text("""SELECT id, account_type, account_id, amount, status,
        payee_masked, failure_reason, created_at, updated_at FROM withdrawal_request WHERE id = :id"""), {"id": withdrawal_id})
    return _withdrawal(result.mappings().one())
