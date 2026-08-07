"""Audited VIP administration backed by paid membership orders."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.member_vip_admin import AdminVipUpdate, AdminVipUpdateResponse


async def update_vip(db: AsyncSession, admin_id: int, user_id: int, request: AdminVipUpdate) -> AdminVipUpdateResponse:
    user = await db.execute(text("SELECT id FROM users WHERE id = :id FOR UPDATE"), {"id": user_id})
    if not user.scalar():
        raise HTTPException(404, detail="会员不存在")

    if request.action == "CANCEL":
        current = (await db.execute(text("""SELECT id, package_type, order_no, start_at, end_at FROM user_membership
            WHERE user_id = :user_id AND status = 1 AND (end_at IS NULL OR end_at > UTC_TIMESTAMP())
            ORDER BY end_at DESC, id DESC LIMIT 1 FOR UPDATE"""), {"user_id": user_id})).mappings().first()
        if not current:
            raise HTTPException(409, detail="会员当前没有有效 VIP")
        await db.execute(text("UPDATE user_membership SET status = 3, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": current["id"]})
        await db.execute(text("""INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id, reason)
            VALUES (:actor, 'member.vip.cancel', 'user_membership', :id, :reason)"""), {"actor": admin_id, "id": current["id"], "reason": request.reason})
        await db.commit()
        return AdminVipUpdateResponse(membership_id=int(current["id"]), user_id=user_id, action=request.action, package_type=current["package_type"], status=3, start_at=current["start_at"], end_at=current["end_at"], order_no=current["order_no"], reason=request.reason)

    order = (await db.execute(text("""SELECT po.id, po.order_no, po.amount, po.status, cp.code, cp.duration_days
        FROM payment_order po JOIN config_membership_package cp ON cp.code = :package_type
        WHERE po.order_no = :order_no AND po.user_id = :user_id AND po.type = 1 AND po.status = 1
        FOR UPDATE"""), {"package_type": request.package_type, "order_no": request.order_no, "user_id": user_id})).mappings().first()
    if not order:
        raise HTTPException(409, detail="必须提供该会员已支付且匹配套餐的会员订单")
    used = await db.execute(text("SELECT id FROM user_membership WHERE order_no = :order_no LIMIT 1 FOR UPDATE"), {"order_no": request.order_no})
    existing = used.scalar()
    if existing:
        row = (await db.execute(text("SELECT id, user_id, package_type, status, start_at, end_at, order_no FROM user_membership WHERE id = :id"), {"id": existing})).mappings().one()
        return AdminVipUpdateResponse(membership_id=int(row["id"]), user_id=int(row["user_id"]), action=request.action, package_type=row["package_type"], status=int(row["status"]), start_at=row["start_at"], end_at=row["end_at"], order_no=row["order_no"], reason="订单已处理")

    active = (await db.execute(text("""SELECT id, end_at FROM user_membership WHERE user_id = :user_id AND status = 1
        AND (end_at IS NULL OR end_at > UTC_TIMESTAMP()) ORDER BY end_at DESC, id DESC LIMIT 1 FOR UPDATE"""), {"user_id": user_id})).mappings().first()
    now = datetime.now(UTC).replace(tzinfo=None)
    start_at = active["end_at"] if request.action == "RENEW" and active and active["end_at"] else now
    end_at = start_at + timedelta(days=int(order["duration_days"]))
    result = await db.execute(text("""INSERT INTO user_membership (user_id, package_type, amount, order_no, start_at, end_at, status)
        VALUES (:user_id, :package_type, :amount, :order_no, :start_at, :end_at, 1)"""), {"user_id": user_id, "package_type": request.package_type, "amount": order["amount"], "order_no": request.order_no, "start_at": start_at, "end_at": end_at})
    membership_id = int(result.lastrowid)
    await db.execute(text("""INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor, :action, 'user_membership', :id, :reason)"""), {"actor": admin_id, "action": f"member.vip.{request.action.lower()}", "id": membership_id, "reason": request.reason or "已支付订单后台处理"})
    await db.commit()
    return AdminVipUpdateResponse(membership_id=membership_id, user_id=user_id, action=request.action, package_type=request.package_type, status=1, start_at=start_at, end_at=end_at, order_no=request.order_no, reason=request.reason)

