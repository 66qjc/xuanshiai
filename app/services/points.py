import secrets
from datetime import UTC, date, datetime

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.quotas import grant
from app.schemas.points import CheckinResponse, ClaimTaskResponse, InvitePage, InviteItem, PointLedgerItem, PointLedgerPage, PointProduct, PointsSummary, RedeemRequest, RedeemResponse, TaskItem

TASKS = {"profile_complete": ("完成资料", "profile_complete_reward"), "realname_verified": ("完成实名认证", "realname_verified_reward")}


def _task_definitions() -> dict[str, tuple[str, int]]:
    return {code: (title, getattr(settings, f"point_{reward_name}")) for code, (title, reward_name) in TASKS.items()}


async def _balance(db: AsyncSession, user_id: int, lock: bool = False) -> int:
    suffix = " FOR UPDATE" if lock else ""
    row = (await db.execute(text(f"SELECT balance FROM user_points WHERE user_id=:id ORDER BY id DESC LIMIT 1{suffix}"), {"id": user_id})).first()
    return int(row[0]) if row else 0


async def _credit(db: AsyncSession, user_id: int, amount: int, point_type: int, description: str) -> int:
    if amount <= 0:
        raise ValueError("point amount must be positive")
    before = await _balance(db, user_id, True)
    after = before + amount
    await db.execute(text("INSERT INTO user_points (user_id,type,amount,balance,`desc`) VALUES (:id,:type,:amount,:balance,:description)"), {"id": user_id, "type": point_type, "amount": amount, "balance": after, "description": description})
    return after


async def summary(db: AsyncSession, user_id: int) -> PointsSummary:
    result = await db.execute(text("SELECT COALESCE((SELECT balance FROM user_points WHERE user_id=:id ORDER BY id DESC LIMIT 1),0), COALESCE(SUM(CASE WHEN amount>0 THEN amount ELSE 0 END),0), COALESCE(SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END),0) FROM user_points WHERE user_id=:id"), {"id": user_id})
    balance, earned, spent = result.first()
    return PointsSummary(balance=int(balance or 0), total_earned=int(earned or 0), total_spent=int(spent or 0))


async def ledger(db: AsyncSession, user_id: int, page: int, page_size: int) -> PointLedgerPage:
    total = int((await db.execute(text("SELECT COUNT(*) FROM user_points WHERE user_id=:id"), {"id": user_id})).scalar() or 0)
    result = await db.execute(text("SELECT id,type,amount,balance,`desc`,created_at FROM user_points WHERE user_id=:id ORDER BY id DESC LIMIT :limit OFFSET :offset"), {"id": user_id, "limit": page_size, "offset": (page - 1) * page_size})
    items = [PointLedgerItem(id=r["id"], type=r["type"], amount=r["amount"], balance=r["balance"], description=r["desc"], created_at=r["created_at"]) for r in result.mappings()]
    return PointLedgerPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def checkin(db: AsyncSession, user_id: int) -> CheckinResponse:
    today = date.today()
    async with db.begin():
        exists = (await db.execute(text("SELECT id,points FROM user_checkin WHERE user_id=:id AND checkin_date=:day FOR UPDATE"), {"id": user_id, "day": today})).mappings().first()
        if exists:
            balance = await _balance(db, user_id)
            return CheckinResponse(checked_in=True, points=int(exists["points"] or 0), balance=balance, checkin_date=today.isoformat())
        reward = settings.point_checkin_reward
        balance = await _credit(db, user_id, reward, 1, "每日签到")
        await db.execute(text("INSERT INTO user_checkin (user_id,checkin_date,points) VALUES (:id,:day,:points)"), {"id": user_id, "day": today, "points": reward})
    return CheckinResponse(checked_in=True, points=reward, balance=balance, checkin_date=today.isoformat())


async def tasks(db: AsyncSession, user_id: int) -> list[TaskItem]:
    result = await db.execute(text("SELECT task_code,status,completed_at FROM user_task WHERE user_id=:id"), {"id": user_id})
    saved = {r["task_code"]: r for r in result.mappings()}
    return [TaskItem(task_code=code, title=title, reward=reward, status=int(saved.get(code, {}).get("status", 1)), completed_at=saved.get(code, {}).get("completed_at")) for code, (title, reward) in _task_definitions().items()]


async def claim_task(db: AsyncSession, user_id: int, task_code: str) -> ClaimTaskResponse:
    task_definitions = _task_definitions()
    if task_code not in task_definitions:
        raise HTTPException(404, detail="任务不存在")
    title, reward = task_definitions[task_code]
    async with db.begin():
        row = (await db.execute(text("SELECT status FROM user_task WHERE user_id=:id AND task_code=:code FOR UPDATE"), {"id": user_id, "code": task_code})).first()
        if row and row[0] == 2:
            return ClaimTaskResponse(task_code=task_code, claimed=True, points=0, balance=await _balance(db, user_id))
        if task_code == "profile_complete":
            complete = (await db.execute(text("SELECT score FROM user_profile_completion WHERE user_id=:id"), {"id": user_id})).scalar()
            if float(complete or 0) < 100:
                raise HTTPException(409, detail="资料尚未完成")
        else:
            verified = (await db.execute(text("SELECT realname_status FROM user_auth WHERE user_id=:id"), {"id": user_id})).scalar()
            if verified != 2:
                raise HTTPException(409, detail="实名认证尚未通过")
        await db.execute(text("INSERT INTO user_task (user_id,task_code,status,reward,completed_at) VALUES (:id,:code,2,:reward,UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE status=2,reward=:reward,completed_at=UTC_TIMESTAMP()"), {"id": user_id, "code": task_code, "reward": str(reward)})
        balance = await _credit(db, user_id, reward, 2, title)
    return ClaimTaskResponse(task_code=task_code, claimed=True, points=reward, balance=balance)


async def invites(db: AsyncSession, user_id: int, page: int, page_size: int) -> InvitePage:
    total = int((await db.execute(text("SELECT COUNT(*) FROM invite_record WHERE inviter_id=:id"), {"id": user_id})).scalar() or 0)
    result = await db.execute(text("SELECT id,invitee_id,status,created_at FROM invite_record WHERE inviter_id=:id ORDER BY id DESC LIMIT :limit OFFSET :offset"), {"id": user_id, "limit": page_size, "offset": (page - 1) * page_size})
    items = [InviteItem(id=r["id"], invitee_id=r["invitee_id"], status=r["status"], register_rewarded=int(r["status"] or 0) >= 1, realname_rewarded=int(r["status"] or 0) >= 2, created_at=r["created_at"]) for r in result.mappings()]
    return InvitePage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def products(db: AsyncSession) -> list[PointProduct]:
    result = await db.execute(text("SELECT code,name,product_type,points_cost,value,stock FROM config_point_product WHERE is_active=1 ORDER BY sort,id"))
    return [PointProduct(code=r["code"], name=r["name"], product_type=r["product_type"], points_cost=settings.point_cost_override(r["code"], r["points_cost"]), value=r["value"], stock=r["stock"]) for r in result.mappings()]


async def redeem(db: AsyncSession, user_id: int, body: RedeemRequest, idempotency_key: str | None) -> RedeemResponse:
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(422, detail="请提供有效的 Idempotency-Key")
    async with db.begin():
        existing = (await db.execute(text("SELECT o.order_no,o.product_code,p.name,o.points_cost,o.status FROM point_redeem_order o LEFT JOIN config_point_product p ON p.id=o.product_id WHERE o.user_id=:user_id AND o.idempotency_key=:key"), {"user_id": user_id, "key": idempotency_key})).mappings().first()
        if existing:
            balance = await _balance(db, user_id)
            return RedeemResponse(order_no=existing["order_no"], product_code=existing["product_code"], product_name=existing["name"] or existing["product_code"], points_cost=existing["points_cost"], status=existing["status"], balance=balance)
        product = (await db.execute(text("SELECT id,code,name,points_cost,stock FROM config_point_product WHERE code=:code AND is_active=1 FOR UPDATE"), {"code": body.product_code})).mappings().first()
        if not product:
            raise HTTPException(404, detail="积分商品或权益不存在")
        points_cost = settings.point_cost_override(product["code"], product["points_cost"])
        if product["stock"] is not None and product["stock"] <= 0:
            raise HTTPException(409, detail="积分商品库存不足")
        before = await _balance(db, user_id, True)
        if before < points_cost:
            raise HTTPException(409, detail="积分余额不足")
        after = before - points_cost
        order_no = f"PT{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}{secrets.token_hex(5).upper()}"
        await db.execute(text("INSERT INTO user_points (user_id,type,amount,balance,`desc`) VALUES (:id,4,:amount,:balance,:description)"), {"id": user_id, "amount": -points_cost, "balance": after, "description": f"兑换{product['name']}"})
        right_quota = {"extra_apply": "apply", "browse_unlock": "browse", "paper_plane_unlock": "paper_plane"}.get(product["code"])
        order_status = 1 if right_quota else 0
        await db.execute(text("INSERT INTO point_redeem_order (order_no,user_id,product_id,product_code,points_cost,status,idempotency_key) VALUES (:order_no,:user_id,:product_id,:product_code,:points_cost,:status,:key)"), {"order_no": order_no, "user_id": user_id, "product_id": product["id"], "product_code": product["code"], "points_cost": points_cost, "status": order_status, "key": idempotency_key})
        if right_quota:
            try:
                amount = max(1, int(product["value"] or 1))
            except (TypeError, ValueError):
                raise HTTPException(422, detail="积分商品权益数量配置无效")
            await grant(db, user_id, right_quota, amount, "points", order_no)
        if product["stock"] is not None:
            await db.execute(text("UPDATE config_point_product SET stock=stock-1 WHERE id=:id"), {"id": product["id"]})
    return RedeemResponse(order_no=order_no, product_code=product["code"], product_name=product["name"], points_cost=points_cost, status=order_status, balance=after)
