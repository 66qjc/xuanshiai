import json
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import redis_client
from app.schemas.quotas import QuotaItem, QuotaSummary


async def _membership_rights(db: AsyncSession, user_id: int) -> tuple[bool, dict]:
    result = await db.execute(text("SELECT p.rights FROM user_membership m LEFT JOIN config_membership_package p ON p.code=m.package_type WHERE m.user_id=:user_id AND m.status=1 AND (m.start_at IS NULL OR m.start_at<=UTC_TIMESTAMP()) AND (m.end_at IS NULL OR m.end_at>UTC_TIMESTAMP()) ORDER BY m.end_at DESC LIMIT 1"), {"user_id": user_id})
    row = result.first()
    if not row:
        return False, {}
    value = row[0]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    return True, value if isinstance(value, dict) else {}


async def _extra_remaining(db: AsyncSession, user_id: int, quota_code: str) -> int:
    result = await db.execute(text("SELECT COALESCE(SUM(remaining),0) FROM user_quota_grant WHERE user_id=:user_id AND quota_code=:quota_code AND (expires_at IS NULL OR expires_at>UTC_TIMESTAMP())"), {"user_id": user_id, "quota_code": quota_code})
    return int(result.scalar() or 0)


async def consume_extra(db: AsyncSession, user_id: int, quota_code: str, reason: str, target_user_id: int | None = None) -> bool:
    result = await db.execute(text("SELECT id FROM user_quota_grant WHERE user_id=:user_id AND quota_code=:quota_code AND remaining>0 AND (expires_at IS NULL OR expires_at>UTC_TIMESTAMP()) ORDER BY expires_at IS NULL, expires_at, id LIMIT 1 FOR UPDATE"), {"user_id": user_id, "quota_code": quota_code})
    row = result.first()
    if not row:
        return False
    await db.execute(text("UPDATE user_quota_grant SET remaining=remaining-1 WHERE id=:id"), {"id": row[0]})
    await db.execute(text("INSERT INTO user_quota_usage (user_id,quota_code,quota_date,source,reason,target_user_id) VALUES (:user_id,:quota_code,:quota_date,'points',:reason,:target_user_id)"), {"user_id": user_id, "quota_code": quota_code, "quota_date": date.today(), "reason": reason, "target_user_id": target_user_id})
    return True


async def grant(db: AsyncSession, user_id: int, quota_code: str, amount: int, source: str, order_no: str) -> None:
    await db.execute(text("INSERT INTO user_quota_grant (user_id,quota_code,remaining,source,order_no) VALUES (:user_id,:quota_code,:remaining,:source,:order_no)"), {"user_id": user_id, "quota_code": quota_code, "remaining": amount, "source": source, "order_no": order_no})


async def summary(db: AsyncSession, user_id: int) -> QuotaSummary:
    vip, rights = await _membership_rights(db, user_id)
    definitions = (
        ("apply", settings.apply_daily_free_limit + (int(rights.get("apply_bonus") or 0) if vip else 0), "apply"),
        ("browse", 20 if vip else settings.browse_daily_limit, "browse"),
        ("superlike", settings.superlike_daily_vip_limit if vip else settings.superlike_daily_free_limit, "superlike"),
        ("paper_plane", settings.paper_plane_daily_limit, "paper_plane"),
    )
    items: list[QuotaItem] = []
    for quota_code, daily_limit, redis_code in definitions:
        used = int(await redis_client.get(f"discovery:{redis_code}:{user_id}:{date.today().isoformat()}") or 0)
        if quota_code == "paper_plane":
            used = int(await redis_client.get(f"paper-plane:{user_id}:{date.today().isoformat()}") or 0)
        daily_remaining = max(0, daily_limit - used)
        extra = await _extra_remaining(db, user_id, quota_code)
        items.append(QuotaItem(quota_code=quota_code, daily_limit=daily_limit, daily_used=used, daily_remaining=daily_remaining, extra_remaining=extra, total_remaining=daily_remaining + extra))
    return QuotaSummary(items=items)
