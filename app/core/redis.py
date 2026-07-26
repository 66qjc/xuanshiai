"""Redis helpers for discovery caches and daily quotas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

redis_client = Redis.from_url(settings.redis_url, decode_responses=True)

CONSUME_DAILY_LUA = """
local value = redis.call('INCR', KEYS[1])
if value == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
if value > tonumber(ARGV[1]) then redis.call('DECR', KEYS[1]); return 0 end
return 1
"""


def daily_quota_key(prefix: str, user_id: int) -> str:
    """Build a daily quota key using UTC date (aligned with consume_daily TTL reset)."""
    day = datetime.now(UTC).date().isoformat()
    return f"{prefix}:{user_id}:{day}"


async def consume_daily(key: str, limit: int) -> bool:
    """Atomically consume one daily quota item, failing closed if Redis is unavailable."""
    try:
        now = datetime.now(UTC)
        reset_at = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ttl = max(60, int((reset_at - now).total_seconds()))
        consumed = await redis_client.eval(CONSUME_DAILY_LUA, 1, key, limit, ttl)
        return bool(consumed)
    except RedisError as exc:
        raise HTTPException(503, detail="Redis服务未配置或暂时不可用") from exc

async def refund_daily(key: str) -> None:
    try:
        value = await redis_client.get(key)
        if value and int(value) > 0:
            await redis_client.decr(key)
    except RedisError as exc:
        raise HTTPException(503, detail="Redis服务未配置或暂时不可用") from exc


async def get_daily_used(key: str) -> int:
    """Return how many times a daily quota key has been consumed today."""
    try:
        value = await redis_client.get(key)
        return int(value or 0)
    except RedisError as exc:
        raise HTTPException(503, detail="Redis服务未配置或暂时不可用") from exc
