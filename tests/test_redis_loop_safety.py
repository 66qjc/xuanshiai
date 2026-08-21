"""Regression test for the asyncio Redis pool/event-loop boundary."""

from __future__ import annotations

import asyncio

from app.core import redis as redis_module


def test_loop_aware_redis_uses_one_client_per_event_loop(monkeypatch) -> None:
    created: list[object] = []

    class _FakeRedis:
        pass

    def factory(*args, **kwargs):
        client = _FakeRedis()
        created.append(client)
        return client

    monkeypatch.setattr(redis_module.Redis, "from_url", factory)
    client = redis_module.LoopAwareRedis("redis://test")

    async def use_client() -> object:
        return client._client()

    first = asyncio.run(use_client())
    second = asyncio.run(use_client())

    assert first is not second
    assert created == [first, second]
