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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.revisions import RevisionVector

logger = logging.getLogger(__name__)

# 租约时长统一来源：与 ai_task.claim_tasks 一致使用 settings.ai_lease_seconds。
# TODO(heartbeat): 当前 run_cleanup_consumer_round 是一次性批量消费，无心跳续租；
# 若未来改为长租约消费者进程，需在独立消费者循环中实现 lease 续租（参考
# tasks.py 的 lease 续租路径），避免长耗时 handler 超过租约后被重复 claim。
def _lease_seconds() -> int:
    return int(settings.ai_lease_seconds)


OUTBOX_MAX_ATTEMPTS = 3
OUTBOX_RETRY_BACKOFF_SECONDS = 30
_UNKNOWN_EVENT_ERROR = "DERIVATION_UNKNOWN_EVENT_TYPE"
_HANDLER_ERROR = "DERIVATION_HANDLER_FAILED"


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
    payload: dict[str, Any] | None = None
    status: str = "pending"
    attempt_count: int = 0

    @classmethod
    def from_row(cls, row: Any) -> DerivationEvent:
        return cls(
            event_id=str(row["event_id"]),
            aggregate_type=str(row["aggregate_type"]),
            aggregate_id=int(row["aggregate_id"]),
            event_type=str(row["event_type"]),
            changed_fields=tuple(json.loads(row["changed_fields"])),
            source_revision=RevisionVector(**json.loads(row["source_revision_json"])),
            occurred_at=row["occurred_at"],
            priority=int(row["priority"]),
            payload=(
                json.loads(row["payload_minimal"])
                if row.get("payload_minimal")
                else None
            ),
            status=str(row.get("status") or "pending"),
            attempt_count=int(row.get("attempt_count") or 0),
        )


DerivationEventHandler = Callable[[DerivationEvent], Awaitable[Any]]


@dataclass(frozen=True)
class ConsumeResult:
    """Outcome of attempting to consume one event."""

    status: str
    applied: bool
    outcome: str = "processed"


@dataclass(frozen=True)
class OutboxHandlerResult:
    """Optional explicit result from a handler."""

    outcome: str = "processed"


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
    lease_until = now + timedelta(seconds=_lease_seconds())
    result = await db.execute(
        text(
            "SELECT e.event_id, e.aggregate_type, e.aggregate_id, e.event_type, "
            "       e.changed_fields, e.source_revision_json, e.occurred_at, "
            "       e.priority, e.lease_until, e.payload_minimal, e.status, e.attempt_count "
            "FROM derivation_outbox AS e "
            "LEFT JOIN derivation_consumer_receipt AS r "
            "  ON r.event_id = e.event_id AND r.consumer_name = :consumer_name "
            "WHERE r.event_id IS NULL "
            "  AND e.published_at IS NOT NULL AND e.published_at <= :now "
            "  AND e.status IN ('pending', 'processing') "
            "  AND (e.status = 'pending' OR e.lease_until IS NULL OR e.lease_until < :now) "
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
    claimed_events: list[DerivationEvent] = []
    for row in rows:
        await db.execute(
            text(
                "UPDATE derivation_outbox "
                "SET status = 'processing', attempt_count = attempt_count + 1, "
                "lease_owner = :worker_id, lease_until = :lease_until "
                "WHERE event_id = :event_id "
                "AND status IN ('pending', 'processing') "
                "AND (lease_until IS NULL OR lease_until < :now)"
            ),
            {
                "worker_id": worker_id,
                "lease_until": lease_until,
                "event_id": row["event_id"],
                "now": now,
            },
        )
        claimed_events.append(
            DerivationEvent.from_row(
                {
                    **dict(row),
                    "status": "processing",
                    "attempt_count": int(row.get("attempt_count") or 0) + 1,
                }
            )
        )
    return claimed_events


async def consume_outbox_event(
    db: AsyncSession,
    event: DerivationEvent,
    consumer_name: str,
    handler: DerivationEventHandler,
) -> ConsumeResult:
    """Consume one leased event with a terminal receipt and finite retry.

    Receipt insertion is deliberately part of the caller's transaction.  A
    handler error removes the provisional receipt and returns the event to
    ``pending`` until the bounded attempt budget is exhausted.
    """
    started = time.perf_counter()
    inserted = await db.execute(
        text(
            "INSERT IGNORE INTO derivation_consumer_receipt "
            "(event_id, consumer_name, event_type, outcome, duration_ms, processed_at) "
            "VALUES (:event_id, :consumer_name, :event_type, 'processed', 0, UTC_TIMESTAMP())"
        ),
        {
            "event_id": event.event_id,
            "consumer_name": consumer_name,
            "event_type": event.event_type,
        },
    )
    if inserted.rowcount == 1:
        try:
            handler_result = await handler(event)
            outcome = _handler_outcome(handler_result)
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            await _mark_outbox_succeeded(
                db, event, consumer_name, outcome, duration_ms
            )
            return ConsumeResult(status="succeeded", applied=True, outcome=outcome)
        except Exception as exc:  # noqa: BLE001 - bounded retry is the contract
            await db.execute(
                text(
                    "DELETE FROM derivation_consumer_receipt "
                    "WHERE event_id = :event_id AND consumer_name = :consumer_name"
                ),
                {"event_id": event.event_id, "consumer_name": consumer_name},
            )
            return await _record_outbox_failure(db, event, consumer_name, exc)
    return ConsumeResult(status="duplicate", applied=False, outcome="noop")


def _handler_outcome(value: Any) -> str:
    outcome = getattr(value, "outcome", value if isinstance(value, str) else "processed")
    return outcome if outcome in {"processed", "noop"} else "processed"


async def _mark_outbox_succeeded(
    db: AsyncSession,
    event: DerivationEvent,
    consumer_name: str,
    outcome: str,
    duration_ms: int,
) -> None:
    await db.execute(
        text(
            "UPDATE derivation_consumer_receipt SET event_type = :event_type, "
            "outcome = :outcome, duration_ms = :duration_ms, processed_at = UTC_TIMESTAMP() "
            "WHERE event_id = :event_id AND consumer_name = :consumer_name"
        ),
        {
            "event_id": event.event_id,
            "consumer_name": consumer_name,
            "event_type": event.event_type,
            "outcome": outcome,
            "duration_ms": duration_ms,
        },
    )
    await db.execute(
        text(
            "UPDATE derivation_outbox SET status = 'succeeded', "
            "last_error_code = NULL, dead_letter_at = NULL, lease_owner = NULL, "
            "lease_until = NULL, payload_minimal = NULL "
            "WHERE event_id = :event_id"
        ),
        {"event_id": event.event_id},
    )


async def _record_outbox_failure(
    db: AsyncSession, event: DerivationEvent, consumer_name: str, error: Exception
) -> ConsumeResult:
    error_code = (
        getattr(error, "code", None)
        or _HANDLER_ERROR
    )
    attempt_count = max(1, int(event.attempt_count or 0))
    if attempt_count >= OUTBOX_MAX_ATTEMPTS:
        await db.execute(
            text(
                "UPDATE derivation_outbox SET status = 'dead_letter', "
                "last_error_code = :error_code, dead_letter_at = UTC_TIMESTAMP(), "
                "lease_owner = NULL, lease_until = NULL, payload_minimal = NULL "
                "WHERE event_id = :event_id"
            ),
            {"event_id": event.event_id, "error_code": error_code},
        )
        await db.execute(
            text(
                "INSERT INTO derivation_consumer_receipt "
                "(event_id, consumer_name, event_type, outcome, duration_ms, processed_at) "
                "VALUES (:event_id, :consumer_name, :event_type, 'dead_letter', 0, UTC_TIMESTAMP()) "
                "ON DUPLICATE KEY UPDATE outcome = 'dead_letter', event_type = VALUES(event_type)"
            ),
            {
                "event_id": event.event_id,
                "consumer_name": consumer_name,
                "event_type": event.event_type,
            },
        )
        return ConsumeResult(status="dead_letter", applied=False, outcome="dead_letter")
    await db.execute(
        text(
            "UPDATE derivation_outbox SET status = 'pending', last_error_code = :error_code, "
            "lease_owner = NULL, lease_until = NULL, "
            "published_at = DATE_ADD(UTC_TIMESTAMP(), INTERVAL :backoff SECOND) "
            "WHERE event_id = :event_id"
        ),
        {
            "event_id": event.event_id,
            "error_code": error_code,
            "backoff": OUTBOX_RETRY_BACKOFF_SECONDS * (2 ** (attempt_count - 1)),
        },
    )
    return ConsumeResult(status="retry", applied=False, outcome="noop")


async def _write_receipt(
    db: AsyncSession,
    event_id: str,
    consumer_name: str,
    *,
    event_type: str,
    outcome: str,
    duration_ms: int = 0,
) -> bool:
    inserted = await db.execute(
        text(
            "INSERT IGNORE INTO derivation_consumer_receipt "
            "(event_id, consumer_name, event_type, outcome, duration_ms, processed_at) "
            "VALUES (:event_id, :consumer_name, :event_type, :outcome, :duration_ms, UTC_TIMESTAMP())"
        ),
        {
            "event_id": event_id,
            "consumer_name": consumer_name,
            "event_type": event_type,
            "outcome": outcome,
            "duration_ms": duration_ms,
        },
    )
    if inserted.rowcount == 0:
        return False
    await db.execute(
        text(
            "UPDATE derivation_consumer_receipt SET event_type = :event_type, "
            "outcome = :outcome, duration_ms = :duration_ms, processed_at = UTC_TIMESTAMP() "
            "WHERE event_id = :event_id AND consumer_name = :consumer_name"
        ),
        {
            "event_id": event_id,
            "consumer_name": consumer_name,
            "event_type": event_type,
            "outcome": outcome,
            "duration_ms": duration_ms,
        },
    )
    return True


async def _mark_outbox_noop(
    db: AsyncSession,
    event: DerivationEvent,
    consumer_name: str,
) -> ConsumeResult:
    inserted = await _write_receipt(
        db,
        event.event_id,
        consumer_name,
        event_type=event.event_type,
        outcome="noop",
    )
    if not inserted:
        return ConsumeResult(status="duplicate", applied=False, outcome="noop")
    await _mark_outbox_succeeded(db, event, consumer_name, "noop", 0)
    return ConsumeResult(status="succeeded", applied=False, outcome="noop")


async def _dead_letter_unknown_event(
    db: AsyncSession,
    event: DerivationEvent,
    consumer_name: str,
) -> ConsumeResult:
    inserted = await _write_receipt(
        db,
        event.event_id,
        consumer_name,
        event_type=event.event_type,
        outcome="dead_letter",
    )
    if not inserted:
        return ConsumeResult(status="duplicate", applied=False, outcome="noop")
    await db.execute(
        text(
            "UPDATE derivation_outbox SET status = 'dead_letter', "
            "last_error_code = :error_code, dead_letter_at = UTC_TIMESTAMP(), "
            "lease_owner = NULL, lease_until = NULL, payload_minimal = NULL "
            "WHERE event_id = :event_id"
        ),
        {"event_id": event.event_id, "error_code": _UNKNOWN_EVENT_ERROR},
    )
    return ConsumeResult(status="dead_letter", applied=False, outcome="dead_letter")


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


class CleanupSubjectInvalid(ValueError):
    """Raised when profile cleanup is missing a valid subject scope."""


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
    A table-missing error (MySQL 1146 / SQLAlchemy NoSuchTableError) is logged
    at debug; any other failure is logged at warning so a real update error
    (e.g. connection lost, schema drift on an existing table) is not silently
    swallowed.
    """
    await _mark_stale_best_effort(
        db,
        "UPDATE ai_search_result r JOIN ai_search_snapshot s "
        "ON s.snapshot_id = r.snapshot_id "
        "SET r.stale = 1, r.updated_at = UTC_TIMESTAMP() "
        "WHERE r.target_user_id = :user_id OR s.user_id = :user_id",
        {"user_id": user_id},
        table="ai_search_result",
    )
    await _mark_stale_best_effort(
        db,
        "UPDATE ai_compatibility_snapshot SET status = 'stale', "
        "invalidated_at = UTC_TIMESTAMP() "
        "WHERE (viewer_user_id = :user_id OR target_user_id = :user_id) "
        "AND status NOT IN ('stale', 'blocked')",
        {"user_id": user_id},
        table="ai_compatibility_snapshot",
    )


def _is_missing_table_error(exc: Exception) -> bool:
    """Return True when ``exc`` indicates the target table does not exist."""
    # MySQL ER_NO_SUCH_TABLE (1146). aiomysql/PyMySQL expose the native code.
    code = getattr(getattr(exc, "orig", exc), "args", ())
    if isinstance(code, tuple) and code and str(code[0]) == "1146":
        return True
    if "1146" in str(getattr(exc, "orig", "")):
        return True
    name = type(exc).__name__
    return name in {"NoSuchTableError", "ProgrammingError"} and "1146" in str(exc)


async def _mark_stale_best_effort(
    db: AsyncSession,
    statement: str,
    params: dict[str, Any],
    *,
    table: str,
) -> None:
    from sqlalchemy.exc import OperationalError, ProgrammingError
    from sqlalchemy.exc import NoSuchTableError

    try:
        await db.execute(text(statement), params)
    except (NoSuchTableError,) as exc:
        logger.debug("%s not present, skip stale marking", table, exc_info=True)
    except (OperationalError, ProgrammingError) as exc:
        if _is_missing_table_error(exc):
            logger.debug("%s not present, skip stale marking", table, exc_info=True)
        else:
            logger.warning(
                "%s stale marking failed user_id=%s; needs retry", table, params.get("user_id"), exc_info=True
            )
    except Exception as exc:  # noqa: BLE001 - unknown driver error, surface as warning
        logger.warning(
            "%s stale marking failed user_id=%s; needs retry", table, params.get("user_id"), exc_info=True
        )


async def purge_ai_resources(
    db: AsyncSession,
    user_id: int,
    *,
    scope: str,
    resource_id: str | None = None,
    subject: str | None = None,
    field_key: str | None = None,
) -> None:
    """Physically remove scoped AI resources after synchronous invalidation.

    The operation is intentionally idempotent and deletes children before
    parents.  Profile revision headers remain as audit metadata while their
    field values and source evidence are scrubbed.
    """
    scope = str(scope)
    if scope == "field":
        params: dict[str, Any] = {"user_id": user_id, "field_key": field_key}
        if not subject or not field_key:
            raise ValueError("field cleanup requires subject and field_key")
        params["subject"] = subject
        await db.execute(
            text(
                "UPDATE ai_profile_revision_field f "
                "JOIN ai_profile_revision r ON r.id = f.revision_id "
                "SET f.value_json = NULL, f.display_value = NULL, "
                "f.confidence = NULL, f.source_type = NULL, "
                "f.source_turn_ids = NULL, f.source_span = NULL "
                "WHERE r.user_id = :user_id AND r.subject = :subject "
                "AND f.field_key = :field_key"
            ),
            params,
        )
        await db.execute(
            text(
                "UPDATE ai_profile_draft_field f "
                "JOIN ai_profile_draft d ON d.draft_id = f.draft_id "
                "SET f.value_json = NULL, f.display_value = NULL, "
                "f.source_type = NULL, f.source_turn_ids = NULL, "
                "f.source_span = NULL, f.confidence = 0, "
                "f.content_hash = NULL, f.confirmation_status = 'deleted' "
                "WHERE d.user_id = :user_id AND d.subject = :subject "
                "AND f.field_key = :field_key"
            ),
            params,
        )
        await db.execute(
            text(
                "DELETE FROM ai_profile_summary "
                "WHERE user_id = :user_id AND subject = :subject"
            ),
            params,
        )
        await db.execute(
            text(
                "DELETE FROM ai_feature_projection "
                "WHERE subject_user_id = :user_id AND status = 'invalidated'"
            ),
            {"user_id": user_id},
        )
        await _purge_ai_redis_cache(user_id)
        return
    if scope in {"profile", "consent_profile", "user"}:
        profile_filters = ["s.user_id = :user_id"]
        profile_params: dict[str, Any] = {"user_id": user_id}
        revision_filters = ["r.user_id = :user_id"]
        if subject:
            profile_filters.append("s.subject = :subject")
            revision_filters.append("r.subject = :subject")
            profile_params["subject"] = subject
        revision_field_filters = list(revision_filters)
        if field_key:
            revision_field_filters.append("f.field_key = :field_key")
            profile_params["field_key"] = field_key
        await db.execute(
            text(
                "DELETE t FROM ai_profile_turn t "
                "JOIN ai_profile_session s ON s.session_id = t.session_id "
                f"WHERE {' AND '.join(profile_filters)}"
            ),
            profile_params,
        )
        await db.execute(
            text(
                "DELETE FROM ai_profile_session WHERE user_id = :user_id"
                + (" AND subject = :subject" if subject else "")
            ),
            profile_params,
        )
        await db.execute(
            text(
                "DELETE f FROM ai_profile_draft_field f "
                "JOIN ai_profile_draft d ON d.draft_id = f.draft_id "
                "WHERE d.user_id = :user_id"
                + (" AND d.subject = :subject" if subject else "")
            ),
            profile_params,
        )
        await db.execute(
            text(
                "DELETE FROM ai_profile_summary WHERE user_id = :user_id"
                + (" AND subject = :subject" if subject else "")
            ),
            profile_params,
        )
        await db.execute(
            text(
                "DELETE FROM ai_profile_draft WHERE user_id = :user_id"
                + (" AND subject = :subject" if subject else "")
            ),
            profile_params,
        )
        await db.execute(
            text(
                "UPDATE ai_profile_revision_field f "
                "JOIN ai_profile_revision r ON r.id = f.revision_id "
                "SET f.value_json = NULL, f.display_value = NULL, "
                "f.confidence = NULL, f.source_type = NULL, "
                "f.source_turn_ids = NULL, f.source_span = NULL "
                "WHERE "
                + " AND ".join(revision_field_filters)
            ),
            profile_params,
        )
        if field_key:
            await db.execute(
                text(
                    "UPDATE ai_profile_draft_field f "
                    "JOIN ai_profile_draft d ON d.draft_id = f.draft_id "
                    "SET f.value_json = NULL, f.display_value = NULL, "
                    "f.source_type = NULL, f.source_turn_ids = NULL, "
                    "f.source_span = NULL, f.confidence = 0, "
                    "f.content_hash = NULL, f.confirmation_status = 'deleted' "
                    "WHERE d.user_id = :user_id AND f.field_key = :field_key"
                    + (" AND d.subject = :subject" if subject else "")
                ),
                profile_params,
            )
        await db.execute(
            text(
                "DELETE FROM ai_feature_projection WHERE subject_user_id = :user_id "
                "AND status = 'invalidated'"
            ),
            {"user_id": user_id},
        )

    if scope in {"profile", "consent_profile", "search", "consent_search", "user"}:
        snapshot_filter = ""
        params: dict[str, Any] = {"user_id": user_id}
        draft_filter = "d.user_id = :user_id"
        draft_params: dict[str, Any] = {"user_id": user_id}
        if resource_id:
            snapshot_filter = " AND s.snapshot_id = :resource_id"
            params["resource_id"] = str(resource_id)
            draft_row = _first_mapping_row(
                await db.execute(
                    text(
                        "SELECT draft_id FROM ai_search_snapshot "
                        "WHERE snapshot_id = :resource_id AND user_id = :user_id"
                    ),
                    params,
                )
            )
            draft_id = draft_row.get("draft_id") if draft_row else None
            if draft_id:
                draft_filter = "d.draft_id = :draft_id"
                draft_params = {"draft_id": draft_id}
        await db.execute(
            text(
                "DELETE r FROM ai_search_result r "
                "JOIN ai_search_snapshot s ON s.snapshot_id = r.snapshot_id "
                "WHERE s.user_id = :user_id" + snapshot_filter
            ),
            params,
        )
        await db.execute(
            text(
                "DELETE c FROM ai_search_condition c "
                "JOIN ai_search_draft d ON d.draft_id = c.draft_id "
                f"WHERE {draft_filter}"
            ),
            draft_params,
        )
        await db.execute(
            text(
                "DELETE FROM ai_search_snapshot WHERE user_id = :user_id"
                + (" AND snapshot_id = :resource_id" if resource_id else "")
            ),
            params,
        )
        await db.execute(
            text(
                "DELETE FROM ai_search_draft WHERE user_id = :user_id"
                + ("" if not resource_id else " AND draft_id = :draft_id")
            ),
            (
                {"user_id": user_id}
                if not resource_id or "draft_id" not in draft_params
                else {"user_id": user_id, "draft_id": draft_params["draft_id"]}
            ),
        )

    if scope in {
        "profile",
        "consent_profile",
        "compatibility",
        "consent_compatibility",
        "user",
    }:
        await db.execute(
            text(
                "DELETE FROM ai_compatibility_snapshot "
                "WHERE viewer_user_id = :user_id OR target_user_id = :user_id"
            ),
            {"user_id": user_id},
        )

    await _purge_ai_redis_cache(user_id)
    try:
        from app.services.ai.tasks import tombstone_owner_tasks

        await tombstone_owner_tasks(db, user_id, task_type="cleanup")
    except Exception:
        logger.warning("ai_cleanup_task_tombstone_failed user_id=%s", user_id, exc_info=True)


async def _purge_ai_redis_cache(user_id: int) -> None:
    """Best-effort deletion of user-scoped AI cache keys."""
    from app.core.redis import redis_client

    patterns = (
        f"ai:search:parse:{user_id}:*",
        f"ai:cache:{user_id}:*",
        f"ai:*:{user_id}:*",
    )
    try:
        for pattern in patterns:
            keys = [key async for key in redis_client.scan_iter(match=pattern)]
            if keys:
                await redis_client.delete(*keys)
    except Exception:  # noqa: BLE001 - Redis is a cache, MySQL remains authoritative
        logger.warning("ai_cleanup_redis_unavailable user_id=%s", user_id)


async def _profile_deleted_cleanup(db: AsyncSession, event: DerivationEvent) -> None:
    """Whole-profile/whole-preference deletion: invalidate all own projections."""
    await run_cleanup_for_user(
        db,
        event.aggregate_id,
        event.event_type,
        event.source_revision,
        subject=(event.payload or {}).get("subject"),
    )
    await purge_ai_resources(
        db,
        event.aggregate_id,
        scope="profile",
        subject=(event.payload or {}).get("subject"),
    )


async def _profile_field_deleted_cleanup(db: AsyncSession, event: DerivationEvent) -> None:
    """Field-level deletion: invalidate stale projections of the affected subject.

    The subject is carried in the minimal event payload.  It scopes projection
    invalidation so personal deletion cannot invalidate ideal-partner preference
    (or vice versa).
    """
    await run_cleanup_for_user(
        db,
        event.aggregate_id,
        event.event_type,
        event.source_revision,
        subject=(event.payload or {}).get("subject"),
    )
    await purge_ai_resources(
        db,
        event.aggregate_id,
        scope="field",
        subject=(event.payload or {}).get("subject"),
        field_key=(event.payload or {}).get("field_key"),
    )


async def _known_mutation_cleanup(db: AsyncSession, event: DerivationEvent) -> None:
    """Consume ordinary revision events without deleting historical content."""
    await _mark_derived_results_stale(db, event.aggregate_id)


async def _consent_revoked_cleanup(db: AsyncSession, event: DerivationEvent) -> None:
    """Physically clean the revoked consent scope; the operation is idempotent."""
    scope = ""
    for changed in event.changed_fields:
        if changed.startswith("ai_consent_revoked:"):
            scope = changed.split(":", 1)[1]
            break
    cleanup_scope = {
        "profile_text_extract": "consent_profile",
        "search_parse": "consent_search",
        "compatibility_shadow": "consent_compatibility",
    }.get(scope)
    if cleanup_scope:
        await purge_ai_resources(db, event.aggregate_id, scope=cleanup_scope)
    else:
        await _mark_derived_results_stale(db, event.aggregate_id)


async def _user_deleted_cleanup(db: AsyncSession, event: DerivationEvent) -> None:
    await purge_ai_resources(db, event.aggregate_id, scope="user")


async def run_cleanup_for_user(
    db: AsyncSession,
    user_id: int,
    reason: str,
    source_revision: RevisionVector,
    subject: str | None = None,
) -> None:
    """Invalidate the user's active projections and stale their derived results.

    Shared by the derivation-outbox cleanup consumer (via the ``ai_*_deleted``
    handlers above) and the ``cleanup`` ai_task worker handler (Task 8 delete
    propagation), so both asynchronous deletion paths run the same physical
    cleanup exactly once per event/receipt.  Does not commit.
    """
    from app.schemas.ai_common import ProjectionKind
    from app.services.ai.features import invalidate_projection

    projection_kind = {
        "personal": ProjectionKind.PERSONAL_SEARCHABLE,
        "ideal_partner": ProjectionKind.IDEAL_PARTNER_PREFERENCE,
    }.get(str(subject or "").strip())
    if projection_kind is None:
        raise CleanupSubjectInvalid(
            "profile cleanup requires subject=personal or subject=ideal_partner"
        )
    await invalidate_projection(
        db, user_id, reason, source_revision, projection_kind=projection_kind
    )
    await _mark_derived_results_stale(db, user_id)


# 覆盖 Task 8 的占位 handler（先覆盖，后启用消费循环）。
register_cleanup_handler("ai_profile_deleted", _profile_deleted_cleanup)
register_cleanup_handler("ai_preference_deleted", _profile_deleted_cleanup)
register_cleanup_handler("ai_profile_field_deleted", _profile_field_deleted_cleanup)
register_cleanup_handler("ai_consent_revoked", _consent_revoked_cleanup)
register_cleanup_handler("user_deleted", _user_deleted_cleanup)
register_cleanup_handler("account_deleted", _user_deleted_cleanup)
for _event_type in (
    "profile_updated",
    "profile_avatar_updated",
    "profile_primary_photo_updated",
    "preference_updated",
    "privacy_updated",
    "relationship_blocked",
    "relationship_unblocked",
    "account_state_changed",
    "ai_profile_published",
    "ai_consent_granted",
):
    register_cleanup_handler(_event_type, _known_mutation_cleanup)

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

    The claim is durable in the caller's transaction (the caller commits it).
    Each event is then consumed in its own independent transaction so a handler
    failure on one event cannot roll back the whole batch — one event failing
    leaves the others already-committed.  ``consume_outbox_event`` never commits
    by itself; the per-event session owns the commit/rollback for that event.
    """
    stats = {
        "claimed": 0,
        "applied": 0,
        "superseded": 0,
        "duplicate": 0,
        "dead_letter": 0,
        "skipped": 0,
    }
    events = await claim_outbox_events(
        db, _CLEANUP_CONSUMER, worker_id, now, limit
    )
    stats["claimed"] = len(events)
    # The claim writes belong to the caller's transaction; they are not committed
    # here.  Each event is consumed in a fresh, independent session so that a
    # failure on one event rolls back only that event's work, not the batch.
    for event in events:
        try:
            await _consume_one_independent(event, _CLEANUP_CONSUMER, stats)
        except Exception:  # noqa: BLE001 - isolate per-event failures
            logger.warning(
                "outbox_consume_failed event_id=%s event_type=%s",
                event.event_id,
                event.event_type,
                exc_info=True,
            )
            stats["dead_letter"] += 1
    return stats


async def _consume_one_independent(
    event: DerivationEvent,
    consumer_name: str,
    stats: dict[str, int],
) -> None:
    """Consume a single outbox event in its own transaction.

    Opens a fresh session from the module-level ``session_factory`` so that the
    event's receipt insert, handler side effects and outbox state update commit
    (or roll back) together and independently of every other event in the batch.
    """
    from app.db.session import session_factory

    if session_factory is None:
        raise RuntimeError("数据库驱动未安装，无法消费 derivation-outbox 事件")
    async with session_factory() as event_db:
        try:
            handler = CLEANUP_HANDLERS.get(event.event_type)
            if handler is None:
                result = await _dead_letter_unknown_event(
                    event_db, event, consumer_name
                )
                if result.status == "dead_letter":
                    stats["dead_letter"] += 1
                else:
                    stats["duplicate"] += 1
                await event_db.commit()
                return
            current = await _load_current_revision_for_event(event_db, event)
            if not should_apply_event(event.source_revision, current):
                # 旧事件：写 noop 收据并收口终态，不覆盖新投影。
                await _mark_outbox_noop(event_db, event, consumer_name)
                stats["superseded"] += 1
                await event_db.commit()
                return
            result = await consume_outbox_event(
                event_db,
                event,
                consumer_name,
                _bind_handler(handler, event_db),
            )
            if result.applied:
                stats["applied"] += 1
            elif result.status == "dead_letter":
                stats["dead_letter"] += 1
            else:
                stats["duplicate"] += 1
            await event_db.commit()
        except Exception:
            await event_db.rollback()
            raise
