"""Redis-backed multi-device presence with a MySQL projection."""

from datetime import UTC, datetime

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import redis_client
from app.schemas.presence import PresenceResponse, PresenceStatusRequest
from app.services.location import remove_online_location

HEARTBEAT_INTERVAL_SECONDS = 30
PRESENCE_TTL_SECONDS = 90
DB_WRITE_INTERVAL_SECONDS = 60


def _key(user_id: int, session_id: int) -> str:
    return f"presence:user:{user_id}:session:{session_id}"


async def _set_key(user_id: int, session_id: int, status: int, *, required: bool) -> None:
    try:
        await redis_client.set(_key(user_id, session_id), str(status), ex=PRESENCE_TTL_SECONDS)
    except RedisError as exc:
        if required:
            raise HTTPException(503, detail="在线状态服务暂时不可用") from exc


async def mark_session_online(db: AsyncSession, user_id: int, session_id: int) -> None:
    """Mark a newly created login session online without blocking login on Redis."""
    await _set_key(user_id, session_id, 1, required=False)
    await db.execute(text("""UPDATE user_profile SET online_status=1, last_active_at=UTC_TIMESTAMP()
        WHERE user_id=:user_id"""), {"user_id": user_id})


async def heartbeat(db: AsyncSession, user_id: int, session_id: int) -> PresenceResponse:
    current = await db.execute(text("SELECT online_status,last_active_at FROM user_profile WHERE user_id=:id"), {"id": user_id})
    row = current.mappings().first() or {"online_status": 0, "last_active_at": None}
    try:
        stored = await redis_client.get(_key(user_id, session_id))
    except RedisError as exc:
        raise HTTPException(503, detail="在线状态服务暂时不可用") from exc
    status = 2 if int(row["online_status"] or 0) == 2 else (int(stored) if stored in {"1", "2"} else 1)
    await _set_key(user_id, session_id, status, required=True)
    last_active = row["last_active_at"]
    now = datetime.now(UTC).replace(tzinfo=None)
    should_write = last_active is None or (now - last_active).total_seconds() >= DB_WRITE_INTERVAL_SECONDS
    if should_write or status == 2:
        await db.execute(text("""UPDATE user_profile SET online_status=:status, last_active_at=UTC_TIMESTAMP()
            WHERE user_id=:user_id"""), {"user_id": user_id, "status": status})
        await db.commit()
        last_active = now
    return PresenceResponse(status=status, last_active_at=last_active, heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS, expires_after_seconds=PRESENCE_TTL_SECONDS)


async def set_status(db: AsyncSession, user_id: int, session_id: int, request: PresenceStatusRequest) -> PresenceResponse:
    await _set_key(user_id, session_id, request.status, required=True)
    await db.execute(text("""UPDATE user_profile SET online_status=:status, last_active_at=UTC_TIMESTAMP()
        WHERE user_id=:user_id"""), {"user_id": user_id, "status": request.status})
    await db.commit()
    return PresenceResponse(status=request.status, last_active_at=datetime.now(UTC).replace(tzinfo=None), heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS, expires_after_seconds=PRESENCE_TTL_SECONDS)


async def get_status(db: AsyncSession, user_id: int, session_id: int) -> PresenceResponse:
    result = await db.execute(text("SELECT online_status,last_active_at FROM user_profile WHERE user_id=:id"), {"id": user_id})
    row = result.mappings().first() or {"online_status": 0, "last_active_at": None}
    try:
        stored = await redis_client.get(_key(user_id, session_id))
    except RedisError as exc:
        raise HTTPException(503, detail="在线状态服务暂时不可用") from exc
    status = int(stored) if stored in {"1", "2"} else int(row["online_status"] or 0)
    if status == 1 and row["last_active_at"]:
        age = (datetime.now(UTC).replace(tzinfo=None) - row["last_active_at"]).total_seconds()
        if age > PRESENCE_TTL_SECONDS:
            status = 0
    return PresenceResponse(status=status, last_active_at=row["last_active_at"], heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS, expires_after_seconds=PRESENCE_TTL_SECONDS)


async def mark_session_offline(db: AsyncSession, user_id: int, session_id: int) -> None:
    try:
        await redis_client.delete(_key(user_id, session_id))
    except RedisError:
        pass
    active = await db.execute(text("SELECT COUNT(*) FROM user_session WHERE user_id=:id AND status=1 AND access_expire_at>UTC_TIMESTAMP()"), {"id": user_id})
    if not active.scalar():
        await db.execute(text("UPDATE user_profile SET online_status=0 WHERE user_id=:id"), {"id": user_id})
        await remove_online_location(user_id)


async def mark_user_offline(db: AsyncSession, user_id: int) -> None:
    try:
        async for key in redis_client.scan_iter(match=f"presence:user:{user_id}:session:*"):
            await redis_client.delete(key)
    except RedisError:
        pass
    await db.execute(text("UPDATE user_profile SET online_status=0 WHERE user_id=:id"), {"id": user_id})
    await remove_online_location(user_id)
