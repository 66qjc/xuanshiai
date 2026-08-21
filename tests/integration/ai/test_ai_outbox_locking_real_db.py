"""Real dual-session lock checks for the derivation-outbox claim/consume path.

Plan Task 4 / G2-B: prove on real MySQL that two independent sessions can
claim outbox events without a lock wait.  ``claim_outbox_events`` uses
``SELECT ... FOR UPDATE SKIP LOCKED`` plus a per-row lease ``UPDATE``, so a
second session that races the first must *skip* the first session's locked
rows (returning immediately) instead of blocking on them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.services.derivation_outbox import claim_outbox_events

_EVENT_PREFIX = "g2b-lock-"


def _revision_json() -> str:
    return json.dumps(
        {"profile": 1, "preference": 0, "privacy": 0, "relationship": 0, "policy": 0}
    )


def _event_id(i: int) -> str:
    return f"{_EVENT_PREFIX}{i:04d}"


async def _seed_events(db: AsyncSession, count: int, now: datetime) -> None:
    # published_at 必须是 claim 时刻之前的时间：DATETIME 列 fsp=0 会把小数秒
    # 四舍五入到整秒，若用当前时刻可能被进位到未来，导致 claim 的
    # `published_at <= :now` 过滤失败。生产语义也是"先发布、后消费"。
    published_at = now - timedelta(seconds=5)
    for i in range(count):
        await db.execute(
            text(
                "INSERT INTO derivation_outbox "
                "(event_id, aggregate_type, aggregate_id, event_type, changed_fields, "
                " source_revision_json, occurred_at, priority, published_at, status) "
                "VALUES (:event_id, 'user', :agg_id, 'profile_updated', :changed, "
                " :rev, :now, 10, :published, 'pending') "
                "ON DUPLICATE KEY UPDATE status='pending', published_at=:published"
            ),
            {
                "event_id": _event_id(i),
                "agg_id": 100000 + i,
                "changed": json.dumps(["profile_updated"]),
                "rev": _revision_json(),
                "now": now,
                "published": published_at,
            },
        )
    await db.commit()


async def _clear_events(db: AsyncSession) -> None:
    await db.execute(
        text("DELETE FROM derivation_outbox WHERE event_id LIKE :prefix"),
        {"prefix": f"{_EVENT_PREFIX}%"},
    )
    await db.execute(
        text("DELETE FROM derivation_consumer_receipt WHERE event_id LIKE :prefix"),
        {"prefix": f"{_EVENT_PREFIX}%"},
    )
    await db.commit()


@pytest_asyncio.fixture
async def session_factory(
    real_db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(real_db_engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_dual_session_claim_skips_locked_rows_without_lock_wait(
    real_db_engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as seed_db:
        await _clear_events(seed_db)
        await _seed_events(seed_db, 10, now)

    # Session A claims all 10 rows and holds the FOR UPDATE locks without
    # committing (lease durability belongs to the caller).
    async with session_factory() as db_a:
        claimed_a = [
            e.event_id
            for e in await claim_outbox_events(db_a, "cleanup", "worker-a", now, 10)
        ]
        # Session B must skip A's still-locked rows and return immediately.
        async with session_factory() as db_b:
            claimed_b = [
                e.event_id
                for e in await claim_outbox_events(db_b, "cleanup", "worker-b", now, 10)
            ]
        assert claimed_b == [], (
            f"second session blocked/claimed locked rows: {claimed_b}"
        )
        await db_a.commit()

    assert len(claimed_a) == 10

    # After A commits, the lease is durable: B still cannot claim the same rows
    # because they are owned by worker-a with a future lease.
    async with session_factory() as db:
        reclaimed = [
            e.event_id for e in await claim_outbox_events(db, "cleanup", "worker-b", now, 10)
        ]
        assert reclaimed == []

        owned = await db.execute(
            text(
                "SELECT lease_owner FROM derivation_outbox "
                "WHERE event_id LIKE :prefix AND status = 'processing'"
            ),
            {"prefix": f"{_EVENT_PREFIX}%"},
        )
        owners = {row["lease_owner"] for row in owned.mappings().all()}
        assert owners == {"worker-a"}

    async with session_factory() as cleanup_db:
        await _clear_events(cleanup_db)


@pytest.mark.asyncio
async def test_two_consumers_claim_disjoint_events(
    real_db_engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as seed_db:
        await _clear_events(seed_db)
        await _seed_events(seed_db, 20, now)

    async with session_factory() as db_a:
        first = [e.event_id for e in await claim_outbox_events(db_a, "cleanup", "worker-a", now, 10)]
        await db_a.commit()

    async with session_factory() as db_b:
        second = [e.event_id for e in await claim_outbox_events(db_b, "cleanup", "worker-b", now, 10)]
        await db_b.commit()

    assert len(first) == 10
    assert len(second) == 10
    assert set(first).isdisjoint(second), "two consumers claimed overlapping events"
    assert len(set(first) | set(second)) == 20

    async with session_factory() as cleanup_db:
        await _clear_events(cleanup_db)
