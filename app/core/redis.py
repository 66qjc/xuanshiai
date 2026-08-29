"""Redis helpers for discovery caches and daily quotas."""

from __future__ import annotations

import asyncio
import weakref
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings


class LoopAwareRedis:
    """Keep async Redis connection pools bound to the running event loop.

    The API process normally uses one long-lived loop, while tests and sync
    ``TestClient`` calls may create several loops in one process.  Reusing a
    redis-py asyncio pool across those loops leaves sockets attached to a
    closed Proactor loop.  A client per loop preserves the module-level API
    while preventing that cross-loop reuse.

    Clients are stored in a :class:`weakref.WeakKeyDictionary` keyed by the
    event loop, so when a loop is garbage-collected (e.g. a test finishes and
    its loop goes out of scope) the mapping entry — and the Redis client it
    holds — is reclaimed automatically.  A ``finalize`` callback closes the
    Redis client (``await client.aclose()``) when the loop is collected,
    avoiding leaked connection pools on short-lived loops.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._clients: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, Redis
        ] = weakref.WeakKeyDictionary()

    def _client(self) -> Redis:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or loop.is_closed():
            # 每次新建 client 时读取当前 settings.redis_url：测试环境通过
            # monkeypatch 切换测试 Redis（compose.ai-test.yml 6380）后，
            # 本模块的延迟建连才能跟随 settings，而不是停留在导入期 URL。
            client = Redis.from_url(settings.redis_url, decode_responses=True)
            # When the loop is GC'd, best-effort close the client pool so its
            # sockets are not left dangling.  aclose() is a coroutine; schedule
            # it on the loop if still running, otherwise skip (loop already
            # closed — sockets will be torn down by the OS).
            def _on_finalize(loop_ref: Any, c: Redis = client) -> None:
                resolved = loop_ref()
                if resolved is None or resolved.is_closed():
                    return
                try:
                    resolved.create_task(c.aclose())
                except RuntimeError:
                    pass  # loop not running — nothing to schedule

            # Keep a strong reference to the finalizer so it is not immediately
            # GC'd (CPython detaches the callback when the finalize object is
            # collected).  Stored on the client so its lifetime matches the
            # client stored in the WeakKeyDictionary.
            client._finalizer = weakref.finalize(  # type: ignore[attr-defined]
                loop, _on_finalize, weakref.ref(loop)
            )
            self._clients[loop] = client
        return client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client(), name)


redis_client = LoopAwareRedis(settings.redis_url)
_local_quota_counts: dict[str, int] = {}

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


def _local_fallback_enabled() -> bool:
    return settings.environment in {"development", "testing"}


def _consume_local(key: str, limit: int) -> bool:
    used = _local_quota_counts.get(key, 0)
    if used >= limit:
        return False
    _local_quota_counts[key] = used + 1
    return True


async def consume_daily(key: str, limit: int) -> bool:
    """Atomically consume one daily quota item, failing closed if Redis is unavailable."""
    try:
        now = datetime.now(UTC)
        reset_at = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        ttl = max(60, int((reset_at - now).total_seconds()))
        consumed = await redis_client.eval(CONSUME_DAILY_LUA, 1, key, limit, ttl)
        return bool(consumed)
    except RedisError as exc:
        if _local_fallback_enabled():
            return _consume_local(key, limit)
        raise HTTPException(503, detail="Redis服务未配置或暂时不可用") from exc

async def refund_daily(key: str) -> None:
    try:
        value = await redis_client.get(key)
        if value and int(value) > 0:
            await redis_client.decr(key)
    except RedisError as exc:
        if _local_fallback_enabled():
            used = _local_quota_counts.get(key, 0)
            if used > 0:
                _local_quota_counts[key] = used - 1
            return
        raise HTTPException(503, detail="Redis服务未配置或暂时不可用") from exc


async def get_daily_used(key: str) -> int:
    """Return how many times a daily quota key has been consumed today."""
    try:
        value = await redis_client.get(key)
        return int(value or 0)
    except RedisError as exc:
        if _local_fallback_enabled():
            return _local_quota_counts.get(key, 0)
        raise HTTPException(503, detail="Redis服务未配置或暂时不可用") from exc
