import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.membership import (
    CreateMembershipOrderRequest,
    MembershipHistoryItem,
    MembershipHistoryPage,
    MembershipOrderResponse,
    MembershipPackage,
    MembershipStatus,
)

DEFAULT_RIGHTS = {
    "apply_daily_limit": 3,
    "apply_bonus": 0,
    "superlike_daily_limit": 1,
    "browse_daily_limit": 8,
    "visitor_detail": False,
    "browse_history_scope": "today",
}


def _rights(value, vip: bool = False) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    rights = {**DEFAULT_RIGHTS, **(value if isinstance(value, dict) else {})}
    if vip:
        rights.update(
            apply_daily_limit=3 + int(rights.get("apply_bonus") or 0),
            superlike_daily_limit=3,
            browse_daily_limit=20,
            visitor_detail=True,
            browse_history_scope="all",
        )
    return rights


async def list_packages(db: AsyncSession) -> list[MembershipPackage]:
    result = await db.execute(text("SELECT code,name,duration_days,price,original_price,daily_price,badge,rights FROM config_membership_package WHERE is_active=1 ORDER BY sort,id"))
    return [
        MembershipPackage(
            code=row["code"], name=row["name"], duration_days=row["duration_days"],
            price=settings.membership_price_override(row["code"], "price", float(row["price"])),
            original_price=settings.membership_price_override(row["code"], "original_price", float(row["original_price"]) if row["original_price"] is not None else None),
            daily_price=settings.membership_price_override(row["code"], "daily_price", float(row["daily_price"]) if row["daily_price"] is not None else None),
            badge=row["badge"], rights=_rights(row["rights"]),
        )
        for row in result.mappings()
    ]


async def get_status(db: AsyncSession, user_id: int) -> MembershipStatus:
    result = await db.execute(text("SELECT m.package_type,m.start_at,m.end_at,p.rights FROM user_membership m LEFT JOIN config_membership_package p ON p.code=m.package_type WHERE m.user_id=:id AND m.status=1 AND (m.start_at IS NULL OR m.start_at<=UTC_TIMESTAMP()) AND (m.end_at IS NULL OR m.end_at>UTC_TIMESTAMP()) ORDER BY m.end_at DESC LIMIT 1"), {"id": user_id})
    row = result.mappings().first()
    return MembershipStatus(is_vip=bool(row), package_type=row["package_type"] if row else None, start_at=row["start_at"] if row else None, end_at=row["end_at"] if row else None, rights=_rights(row["rights"] if row else None, bool(row)))


async def history(db: AsyncSession, user_id: int, page: int, page_size: int) -> MembershipHistoryPage:
    total = int((await db.execute(text("SELECT COUNT(*) FROM user_membership WHERE user_id=:id"), {"id": user_id})).scalar() or 0)
    result = await db.execute(text("SELECT id,package_type,amount,order_no,start_at,end_at,status FROM user_membership WHERE user_id=:id ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"), {"id": user_id, "limit": page_size, "offset": (page - 1) * page_size})
    now = datetime.now(UTC).replace(tzinfo=None)
    items = [MembershipHistoryItem(id=r["id"], package_type=r["package_type"], amount=float(r["amount"]) if r["amount"] is not None else None, order_no=r["order_no"], start_at=r["start_at"], end_at=r["end_at"], status=r["status"], is_vip=r["status"] == 1 and (r["end_at"] is None or r["end_at"] > now), rights=_rights(None, r["status"] == 1)) for r in result.mappings()]
    return MembershipHistoryPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def create_order(db: AsyncSession, user_id: int, body: CreateMembershipOrderRequest, idempotency_key: str | None) -> MembershipOrderResponse:
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(422, detail="Idempotency-Key is required")
    existing = await db.execute(text("SELECT order_no,product_type,product_name,amount,pay_type,status,expire_at FROM payment_order WHERE user_id=:uid AND idempotency_key=:key"), {"uid": user_id, "key": idempotency_key})
    row = existing.mappings().first()
    if row:
        if str(row["product_type"]) != body.package_code:
            raise HTTPException(409, detail="Idempotency-Key 已用于其他会员套餐，请更换新的幂等键")
        return MembershipOrderResponse(order_no=row["order_no"], package_code=str(row["product_type"]), product_name=row["product_name"], amount=float(row["amount"]), pay_type=row["pay_type"], status=row["status"], expire_at=row["expire_at"], payment_required=row["status"] == 0)
    package = (await db.execute(text("SELECT id,code,name,price FROM config_membership_package WHERE code=:code AND is_active=1"), {"code": body.package_code})).mappings().first()
    if not package:
        raise HTTPException(404, detail="Membership package not found")
    price = settings.membership_price_override(package["code"], "price", float(package["price"]))
    order_no = f"VIP{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}{secrets.token_hex(5).upper()}"
    expire_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=30)
    pay_type = 4 if settings.is_test_mode else 1
    await db.execute(text("INSERT INTO payment_order (user_id,order_no,type,product_id,product_type,product_name,amount,pay_type,status,expire_at,idempotency_key) VALUES (:uid,:order_no,1,:pid,:code,:name,:amount,:pay_type,0,:expire_at,:key)"), {"uid": user_id, "order_no": order_no, "pid": package["id"], "code": package["code"], "name": package["name"], "amount": price, "pay_type": pay_type, "expire_at": expire_at, "key": idempotency_key})
    await db.commit()
    return MembershipOrderResponse(order_no=order_no, package_code=package["code"], product_name=package["name"], amount=price, pay_type=pay_type, status=0, expire_at=expire_at, payment_required=True)


async def get_order(db: AsyncSession, user_id: int, order_no: str) -> MembershipOrderResponse:
    row = (await db.execute(text("SELECT product_type,product_name,amount,pay_type,status,expire_at,order_no FROM payment_order WHERE user_id=:uid AND order_no=:order_no"), {"uid": user_id, "order_no": order_no})).mappings().first()
    if not row:
        raise HTTPException(404, detail="Order not found")
    return MembershipOrderResponse(order_no=row["order_no"], package_code=str(row["product_type"]), product_name=row["product_name"], amount=float(row["amount"]), pay_type=row["pay_type"], status=row["status"], expire_at=row["expire_at"], payment_required=row["status"] == 0)


async def pay_order_with_balance(db: AsyncSession, user_id: int, order_no: str, idempotency_key: str) -> MembershipOrderResponse:
    if not idempotency_key or len(idempotency_key) < 8 or len(idempotency_key) > 128:
        raise HTTPException(422, detail="Idempotency-Key 长度必须为 8-128")
    result = await db.execute(text("""SELECT po.id, po.order_no, po.product_type, po.product_name, po.amount,
        po.pay_type, po.status, po.expire_at, cp.code, cp.duration_days
        FROM payment_order po JOIN config_membership_package cp ON cp.code = po.product_type
        WHERE po.user_id = :user_id AND po.order_no = :order_no AND po.type = 1 FOR UPDATE"""), {"user_id": user_id, "order_no": order_no})
    order = result.mappings().first()
    if not order:
        raise HTTPException(404, detail="会员订单不存在")
    if int(order["status"]) == 1:
        return MembershipOrderResponse(order_no=order["order_no"], package_code=order["code"], product_name=order["product_name"], amount=float(order["amount"]), pay_type=order["pay_type"], status=1, expire_at=order["expire_at"], payment_required=False)
    if int(order["status"]) != 0:
        raise HTTPException(409, detail="订单当前不可支付")
    if order["expire_at"] and order["expire_at"] <= datetime.now(UTC).replace(tzinfo=None):
        raise HTTPException(409, detail="订单已过期")
    await db.execute(text("SELECT id FROM account_ledger WHERE account_type = 'user' AND account_id = :user_id FOR UPDATE"), {"user_id": user_id})
    balance = (await db.execute(text("""SELECT COALESCE(SUM(CASE WHEN state = 'AVAILABLE' AND direction = 'CREDIT' THEN amount
        WHEN state = 'AVAILABLE' AND direction = 'DEBIT' THEN -amount ELSE 0 END), 0) FROM account_ledger WHERE account_type = 'user' AND account_id = :user_id"""), {"user_id": user_id})).scalar()
    amount = Decimal(str(order["amount"]))
    if Decimal(str(balance or 0)) < amount:
        raise HTTPException(409, detail="余额不足")
    await db.execute(text("""UPDATE payment_order SET status = 1, pay_type = 2, pay_time = UTC_TIMESTAMP(),
        transaction_id = :transaction_id WHERE id = :id AND status = 0"""), {"transaction_id": f"BALANCE-{order['id']}-{idempotency_key[:32]}", "id": order["id"]})
    await db.execute(text("""INSERT INTO account_ledger (account_type, account_id, direction, amount, state, source_type, source_id, idempotency_key)
        VALUES ('user', :user_id, 'DEBIT', :amount, 'AVAILABLE', 'membership_order', :order_id, :ledger_key)
        ON DUPLICATE KEY UPDATE id = id"""), {"user_id": user_id, "amount": amount, "order_id": order["id"], "ledger_key": f"membership-balance:{order['id']}"})
    start_at = datetime.now(UTC).replace(tzinfo=None)
    await db.execute(text("""INSERT INTO user_membership (user_id, package_type, amount, order_no, start_at, end_at, status)
        VALUES (:user_id, :package_type, :amount, :order_no, :start_at, :end_at, 1)"""), {"user_id": user_id, "package_type": order["code"], "amount": amount, "order_no": order["order_no"], "start_at": start_at, "end_at": start_at + timedelta(days=int(order["duration_days"]))})
    await db.commit()
    return MembershipOrderResponse(order_no=order["order_no"], package_code=order["code"], product_name=order["product_name"], amount=float(amount), pay_type=2, status=1, expire_at=order["expire_at"], payment_required=False)


async def handle_wechat_callback(db: AsyncSession, body) -> None:
    if settings.wechat_payment_mode == "real":
        raise HTTPException(503, detail="WeChat payment callback verification is not configured")

    async with db.begin():
        result = await db.execute(text("SELECT po.id,po.user_id,po.amount,po.status,po.order_no,cp.code,cp.duration_days FROM payment_order po JOIN config_membership_package cp ON cp.code=po.product_type WHERE po.order_no=:order_no AND po.type=1 FOR UPDATE"), {"order_no": body.order_no})
        order = result.mappings().first()
        if not order:
            raise HTTPException(404, detail="Membership order not found")
        if order["status"] == 1:
            return
        if order["status"] != 0:
            raise HTTPException(409, detail="Membership order cannot be paid")
        start_at = datetime.now(UTC).replace(tzinfo=None)
        end_at = start_at + timedelta(days=int(order["duration_days"]))
        await db.execute(text("UPDATE payment_order SET status=1,pay_time=UTC_TIMESTAMP(),transaction_id=:transaction_id WHERE id=:id"), {"transaction_id": body.transaction_id, "id": order["id"]})
        await db.execute(text("INSERT INTO user_membership (user_id,package_type,amount,order_no,start_at,end_at,status) VALUES (:user_id,:package_type,:amount,:order_no,:start_at,:end_at,1)"), {"user_id": order["user_id"], "package_type": order["code"], "amount": order["amount"], "order_no": order["order_no"], "start_at": start_at, "end_at": end_at})
