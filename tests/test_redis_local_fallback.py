import pytest
from redis.exceptions import ConnectionError

from app.core import redis as redis_module


class BrokenRedis:
    async def eval(self, *_args: object) -> int:
        raise ConnectionError("redis offline")

    async def get(self, *_args: object) -> None:
        raise ConnectionError("redis offline")

    async def decr(self, *_args: object) -> None:
        raise ConnectionError("redis offline")


@pytest.mark.asyncio
async def test_development_quotas_fall_back_to_process_memory_when_redis_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redis_module, "redis_client", BrokenRedis())
    monkeypatch.setattr(redis_module.settings, "environment", "development")

    key = "paper-plane:local-fallback"

    assert await redis_module.consume_daily(key, 2) is True
    assert await redis_module.get_daily_used(key) == 1
    assert await redis_module.consume_daily(key, 2) is True
    assert await redis_module.consume_daily(key, 2) is False

    await redis_module.refund_daily(key)

    assert await redis_module.get_daily_used(key) == 1
