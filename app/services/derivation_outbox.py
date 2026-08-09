"""Claim and idempotently consume derivation-outbox events.

``claim_outbox_events`` leases unprocessed rows for one consumer by joining the
consumer receipt table; ``consume_outbox_event`` inserts a receipt in the
caller's transaction so a repeated event for the same ``(event_id,
consumer_name)`` runs the handler only once.  Neither function commits — the
consumer's transaction owns durability.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.revisions import RevisionVector

logger = logging.getLogger(__name__)

LEASE_SECONDS = 60


@dataclass(frozen=True)
class DerivationEvent:
    """A row claimed from ``derivation_outbox``."""

    event_id: str
    aggregate_type: str
    aggregate_id: int
    event_type: str
    changed_fields: tuple[str, ...]
    source_revision: RevisionVector
    occurred_at: datetime
    priority: int

    @classmethod
    def from_row(cls, row: Any) -> "DerivationEvent":
        return cls(
            event_id=str(row["event_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=int(row["aggregate_id"]),
            event_type=str(row["event_type"]),
            changed_fields=tuple(json.loads(row["changed_fields"])),
            source_revision=RevisionVector(**json.loads(row["source_revision_json"])),
            occurred_at=row["occurred_at"],
            priority=int(row["priority"]),
        )


DerivationEventHandler = Callable[[DerivationEvent], Awaitable[Any]]


@dataclass(frozen=True)
class ConsumeResult:
    """Outcome of attempting to consume one event."""

    status: str
    applied: bool


def should_apply_event(event_vector: RevisionVector, current_vector: RevisionVector) -> bool:
    """Return True only when the event's snapshot matches the current vector.

    Any dimension mismatch — including an older snapshot — means the event is
    stale and must not overwrite a newer projection.
    """
    return event_vector == current_vector


async def claim_outbox_events(
    db: AsyncSession,
    consumer_name: str,
    worker_id: str,
    now: datetime,
    limit: int,
) -> list[DerivationEvent]:
    """Lease the oldest unprocessed published rows and return them as events.

    One locking ``SELECT ... FOR UPDATE SKIP LOCKED`` picks the oldest rows the
    consumer has not yet processed (the receipt ``LEFT JOIN`` filters already
    consumed events) and atomically reserves them for this worker, so two
    workers can never claim the same event: rows another transaction already
    locked are skipped, and once a lease is committed the ``lease_until``
    predicate excludes the row until the lease expires.  A per-row single-table
    ``UPDATE`` then records the lease.  Multi-table ``UPDATE ... ORDER BY ...
    LIMIT`` is illegal in MySQL (ERROR 1221), so ordering/limiting live in the
    SELECT and the lease write is a plain per-row UPDATE — both statements are
    valid MySQL 8 SQL.  The function never commits; the caller's transaction
    owns durability.
    """
    lease_until = now + timedelta(seconds=LEASE_SECONDS)
    result = await db.execute(
        text(
            "SELECT e.event_id, e.aggregate_type, e.aggregate_id, e.event_type, "
            "       e.changed_fields, e.source_revision_json, e.occurred_at, "
            "       e.priority, e.lease_until "
            "FROM derivation_outbox AS e "
            "LEFT JOIN derivation_consumer_receipt AS r "
            "  ON r.event_id = e.event_id AND r.consumer_name = :consumer_name "
            "WHERE r.event_id IS NULL "
            "  AND e.published_at IS NOT NULL "
            "  AND (e.lease_owner IS NULL OR e.lease_until IS NULL OR e.lease_until < :now) "
            "ORDER BY e.published_at ASC, e.priority ASC, e.occurred_at ASC "
            "LIMIT :limit "
            "FOR UPDATE SKIP LOCKED"
        ),
        {
            "consumer_name": consumer_name,
            "now": now,
            "limit": limit,
        },
    )
    rows = result.mappings().all()
    for row in rows:
        await db.execute(
            text(
                "UPDATE derivation_outbox "
                "SET lease_owner = :worker_id, lease_until = :lease_until "
                "WHERE event_id = :event_id"
            ),
            {
                "worker_id": worker_id,
                "lease_until": lease_until,
                "event_id": row["event_id"],
            },
        )
    return [DerivationEvent.from_row(row) for row in rows]


async def consume_outbox_event(
    db: AsyncSession,
    event: DerivationEvent,
    consumer_name: str,
    handler: DerivationEventHandler,
) -> ConsumeResult:
    """Insert a consumer receipt and run the handler exactly once per event."""
    inserted = await db.execute(
        text(
            "INSERT IGNORE INTO derivation_consumer_receipt "
            "(event_id, consumer_name, processed_at) "
            "VALUES (:event_id, :consumer_name, UTC_TIMESTAMP())"
        ),
        {"event_id": event.event_id, "consumer_name": consumer_name},
    )
    if inserted.rowcount == 1:
        await handler(event)
        return ConsumeResult(status="applied", applied=True)
    return ConsumeResult(status="duplicate", applied=False)


# ----------------------------------------------------------------------
# Task 9：M04 删除/字段删除的投影失效消费者
# ----------------------------------------------------------------------
#
# 删除事务（Task 8 delete_ai_profile / delete_ai_profile_field）内已完成「同步
# 不可读」标记；本消费循环负责异步派生失效的闭环：
# - ai_profile_deleted / ai_preference_deleted：把该用户全部 active 投影按当前
#   版本向量标 invalidated，派生结果表若已建（ai_search_result /
#   ai_compatibility_snapshot）一并标 stale；不存在则留待 Task 10/11。
# - ai_profile_field_deleted：字段级删除只改变该主体 revision，失效对应投影；
#   search result / compat snapshot 的字段级重建由 Task 10/11 消费者处理。
#
# 交接约束（Task 8 review I-2）：必须先把本模块的注册表覆盖为真实 handler
# （下方 register_cleanup_handler 调用），再启用消费循环；否则历史删除事件会
# 被占位收据消费，真实清理永不执行。重复消费由 derivation_consumer_receipt
# 拦截；旧事件（版本落后）返回 superseded，不覆盖新投影。
CleanupHandler = Callable[[AsyncSession, DerivationEvent], Awaitable[Any]]
CLEANUP_HANDLERS: dict[str, CleanupHandler] = {}


def register_cleanup_handler(event_type: str, handler: CleanupHandler) -> None:
    """Register (or replace) the cleanup handler for an event type.

    The handler receives ``(db, event)`` and runs inside the consumer's
    transaction, so its effect and the ``derivation_consumer_receipt`` insert
    commit atomically — a failure rolls back both and the event stays unprocessed.
    """
    CLEANUP_HANDLERS[event_type] = handler


async def _load_current_revision_for_event(
    db: AsyncSession, event: DerivationEvent
) -> RevisionVector:
    result = await db.execute(
        text(
            "SELECT profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision "
            "FROM user_revision_state WHERE user_id = :user_id"
        ),
        {"user_id": event.aggregate_id},
    )
    row = _first_mapping_row(result)
    if row is None:
        return RevisionVector()
    return RevisionVector(
        profile=int(row["profile_revision"] or 0),
        preference=int(row["preference_revision"] or 0),
        privacy=int(row["privacy_revision"] or 0),
        relationship=int(row["relationship_revision"] or 0),
        policy=int(row["policy_revision"] or 0),
    )


def _first_mapping_row(result: Any) -> dict[str, Any] | None:
    mappings = getattr(result, "mappings", None)
    if not callable(mappings):
        return None
    return mappings().first()


async def _mark_derived_results_stale(db: AsyncSession, user_id: int) -> None:
    """Best-effort stale marking of derived result tables that already exist.

    ai_search_result / ai_compatibility_snapshot belong to Task 10/11; marking
    them here when the tables exist keeps the delete propagation closed end to
    end, while a missing table (pre-Task 10/11) is a no-op, not a failure.
    """
    try:
        await db.execute(
            text("UPDATE ai_search_result SET stale = 1 WHERE target_user_id = :user_id"),
            {"user_id": user_id},
        )
    except Exception:  # noqa: BLE001 - table may not exist yet (Task 10)
        logger.debug("ai_search_result not present, skip stale marking", exc_info=True)
    try:
        await db.execute(
            text(
                "UPDATE ai_compatibility_snapshot SET status = 'stale', "
                "invalidated_at = UTC_TIMESTAMP() "
                "WHERE (viewer_user_id = :user_id OR target_user_id = :user_id) "
                "AND status NOT IN ('stale', 'blocked')"
            ),
            {"user_id": user_id},
        )
    except Exception:  # noqa: BLE001 - table may not exist yet (Task 11)
        logger.debug("ai_compatibility_snapshot not present, skip stale marking", exc_info=True)


async def _profile_deleted_cleanup(db: AsyncSession, event: DerivationEvent) -> None:
    """Whole-profile/whole-preference deletion: invalidate all own projections."""
    await run_cleanup_for_user(
        db, event.aggregate_id, event.event_type, event.source_revision
    )


async def _profile_field_deleted_cleanup(db: AsyncSession, event: DerivationEvent) -> None:
    """Field-level deletion: invalidate stale projections of the affected subject.

    The event carries no ``subject``; the changed field belongs either to the
    personal facts or the ideal-partner preference, so every projection whose
    stored vector no longer matches the event snapshot is invalidated.  The next
    publish bumps the subject revision and rebuilds the matching projection.
    """
    await run_cleanup_for_user(
        db, event.aggregate_id, event.event_type, event.source_revision
    )


async def run_cleanup_for_user(
    db: AsyncSession, user_id: int, reason: str, source_revision: RevisionVector
) -> None:
    """Invalidate the user's active projections and stale their derived results.

    Shared by the derivation-outbox cleanup consumer (via the ``ai_*_deleted``
    handlers above) and the ``cleanup`` ai_task worker handler (Task 8 delete
    propagation), so both asynchronous deletion paths run the same physical
    cleanup exactly once per event/receipt.  Does not commit.
    """
    from app.services.ai.features import invalidate_projection

    await invalidate_projection(db, user_id, reason, source_revision)
    await _mark_derived_results_stale(db, user_id)


# 覆盖 Task 8 的占位 handler（先覆盖，后启用消费循环）。
register_cleanup_handler("ai_profile_deleted", _profile_deleted_cleanup)
register_cleanup_handler("ai_preference_deleted", _profile_deleted_cleanup)
register_cleanup_handler("ai_profile_field_deleted", _profile_field_deleted_cleanup)

# 本消费循环的收据消费者名。
_CLEANUP_CONSUMER = "cleanup"


def _bind_handler(
    handler: CleanupHandler, db: AsyncSession
) -> DerivationEventHandler:
    @functools.wraps(handler)
    async def wrapped(event: DerivationEvent) -> Any:
        return await handler(db, event)

    return wrapped


async def run_cleanup_consumer_round(
    db: AsyncSession,
    worker_id: str,
    now: datetime,
    limit: int,
) -> dict[str, int]:
    """Consume one batch of derivation-outbox deletion events for the cleanup consumer.

    Ordering guarantees (spec §5.6 / Task 8 review I-2):
    1. Only rows without an existing ``cleanup`` receipt are claimed, so a
       duplicate delivery never runs a handler twice.
    2. The event's ``source_revision`` is compared against the user's current
       vector: a stale event writes a ``superseded`` receipt and never touches a
       newer projection; a current event runs the registered cleanup handler
       (projection invalidation + derived-result stale marking).
    Returns ``{"claimed", "applied", "superseded", "duplicate", "skipped"}``.
    The caller's transaction owns durability — no commit here.
    """
    stats = {"claimed": 0, "applied": 0, "superseded": 0, "duplicate": 0, "skipped": 0}
    events = await claim_outbox_events(
        db, _CLEANUP_CONSUMER, worker_id, now, limit
    )
    stats["claimed"] = len(events)
    for event in events:
        handler = CLEANUP_HANDLERS.get(event.event_type)
        if handler is None:
            stats["skipped"] += 1
            continue
        current = await _load_current_revision_for_event(db, event)
        if not should_apply_event(event.source_revision, current):
            # 旧事件：写收据防重复投递，标 superseded，不覆盖新投影。
            await _write_receipt(db, event.event_id, _CLEANUP_CONSUMER)
            stats["superseded"] += 1
            continue
        result = await consume_outbox_event(
            db, event, _CLEANUP_CONSUMER, _bind_handler(handler, db)
        )
        if result.applied:
            stats["applied"] += 1
        else:
            stats["duplicate"] += 1
    return stats


async def _write_receipt(db: AsyncSession, event_id: str, consumer_name: str) -> None:
    await db.execute(
        text(
            "INSERT IGNORE INTO derivation_consumer_receipt "
            "(event_id, consumer_name, processed_at) "
            "VALUES (:event_id, :consumer_name, UTC_TIMESTAMP())"
        ),
        {"event_id": event_id, "consumer_name": consumer_name},
    )
