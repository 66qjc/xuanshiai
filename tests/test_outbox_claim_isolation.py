"""Outbox claim/consume transaction isolation tests.

Plan Task 4 / G2-B Step 1: verify that ``run_cleanup_consumer_round`` commits
the claim lease *before* opening independent sessions to consume events.

The original defect (AI-P0-06): ``claim_outbox_events`` holds ``FOR UPDATE``
locks in the caller's session without committing; then ``_consume_one_independent``
opens a fresh session that tries to UPDATE the same rows, causing a self-lock
wait.

Contract: the caller's session must see a ``commit()`` call between
``claim_outbox_events`` returning and the first ``_consume_one_independent``
call.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.derivation_outbox import (
    DerivationEvent,
    run_cleanup_consumer_round,
)
from app.services.revisions import RevisionVector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_id: str = "evt-1") -> DerivationEvent:
    return DerivationEvent(
        event_id=event_id,
        aggregate_type="user",
        aggregate_id=1,
        event_type="profile_updated",
        changed_fields=("profile_updated",),
        source_revision=RevisionVector(profile=1),
        occurred_at=datetime(2026, 8, 16, tzinfo=UTC),
        priority=10,
    )


class _ClaimResult:
    """Mock result from claim_outbox_events SELECT."""

    def mappings(self):
        return self

    def all(self):
        return [
            {
                "event_id": "evt-1",
                "aggregate_type": "user",
                "aggregate_id": 1,
                "event_type": "profile_updated",
                "changed_fields": json.dumps(["profile_updated"]),
                "source_revision_json": json.dumps(
                    {"profile": 1, "preference": 0, "privacy": 0, "relationship": 0, "policy": 0}
                ),
                "occurred_at": datetime(2026, 8, 16, tzinfo=UTC),
                "priority": 10,
                "lease_until": None,
                "payload_minimal": None,
                "status": "pending",
                "attempt_count": 0,
            }
        ]


class _RevisionResult:
    """Mock result from revision lookup."""

    def mappings(self):
        return self

    def first(self):
        return {
            "profile_revision": 1,
            "preference_revision": 0,
            "privacy_revision": 0,
            "relationship_revision": 0,
            "policy_revision": 0,
        }


class _TrackingSession:
    """Async session that tracks commit/rollback ordering relative to calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT" in sql.upper() and "derivation_outbox" in sql:
            self.calls.append("claim_select")
            return _ClaimResult()
        if "UPDATE derivation_outbox" in sql:
            self.calls.append("claim_update")
            return MagicMock(rowcount=1)
        if "user_revision_state" in sql:
            self.calls.append("revision_lookup")
            return _RevisionResult()
        return MagicMock()

    async def commit(self):
        self.calls.append("commit")
        self.committed = True

    async def rollback(self):
        self.calls.append("rollback")
        self.rolled_back = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_cleanup_round_commits_claim_before_consuming() -> None:
    """The caller's session must commit the lease before independent consumption.

    This test fails if ``run_cleanup_consumer_round`` does not commit the claim
    transaction before opening independent sessions — proving the self-lock defect.
    """
    tracker = _TrackingSession()

    # Mock claim_outbox_events to return events from the tracker session
    fake_events = [_make_event("evt-1")]

    # Track whether _consume_one_independent was called before commit
    consume_before_commit = []

    async def fake_consume_independent(event, consumer_name, stats):
        if not tracker.committed:
            consume_before_commit.append(event.event_id)
        # Simulate a successful consume
        stats["applied"] += 1

    with (
        patch(
            "app.services.derivation_outbox.claim_outbox_events",
            new_callable=AsyncMock,
            return_value=fake_events,
        ),
        patch(
            "app.services.derivation_outbox._consume_one_independent",
            side_effect=fake_consume_independent,
        ),
    ):
        await run_cleanup_consumer_round(
            tracker,  # type: ignore[arg-type]
            worker_id="worker-a",
            now=datetime(2026, 8, 16, tzinfo=UTC),
            limit=10,
        )

    # The claim must have been committed before any consume call
    assert len(consume_before_commit) == 0, (
        f"_consume_one_independent was called for events {consume_before_commit} "
        f"before the claim session was committed — this is the self-lock defect"
    )
    # And a commit must have happened
    assert tracker.committed, "claim session was never committed"


@pytest.mark.asyncio
async def test_run_cleanup_round_commits_claim_in_caller_session() -> None:
    """Verify that commit happens on the caller's session, not just inside
    _consume_one_independent's independent sessions."""
    tracker = _TrackingSession()
    fake_events = [_make_event("evt-1"), _make_event("evt-2")]

    async def fake_consume_independent(event, consumer_name, stats):
        stats["applied"] += 1

    with (
        patch(
            "app.services.derivation_outbox.claim_outbox_events",
            new_callable=AsyncMock,
            return_value=fake_events,
        ),
        patch(
            "app.services.derivation_outbox._consume_one_independent",
            side_effect=fake_consume_independent,
        ),
    ):
        stats = await run_cleanup_consumer_round(
            tracker,  # type: ignore[arg-type]
            worker_id="worker-a",
            now=datetime(2026, 8, 16, tzinfo=UTC),
            limit=10,
        )

    assert stats["claimed"] == 2
    assert stats["applied"] == 2
    # The caller's session must have been committed (lease durability)
    assert tracker.committed, (
        "claim session was not committed — lease writes are not durable"
    )
    # commit must appear in the call sequence
    assert "commit" in tracker.calls


@pytest.mark.asyncio
async def test_run_cleanup_round_claim_commit_precedes_consume_calls() -> None:
    """The commit call on the claim session must come before any
    _consume_one_independent invocation."""
    tracker = _TrackingSession()
    fake_events = [_make_event("evt-1")]

    call_log: list[str] = []

    async def tracking_consume(event, consumer_name, stats):
        call_log.append(f"consume:{event.event_id}")
        stats["applied"] += 1

    # Wrap tracker.commit to log when it's called
    original_commit = tracker.commit

    async def logging_commit():
        call_log.append("claim_commit")
        await original_commit()

    tracker.commit = logging_commit  # type: ignore[assignment]

    with (
        patch(
            "app.services.derivation_outbox.claim_outbox_events",
            new_callable=AsyncMock,
            return_value=fake_events,
        ),
        patch(
            "app.services.derivation_outbox._consume_one_independent",
            side_effect=tracking_consume,
        ),
    ):
        await run_cleanup_consumer_round(
            tracker,  # type: ignore[arg-type]
            worker_id="worker-a",
            now=datetime(2026, 8, 16, tzinfo=UTC),
            limit=10,
        )

    # claim_commit must appear before any consume:evt call
    commit_idx = call_log.index("claim_commit")
    consume_indices = [i for i, c in enumerate(call_log) if c.startswith("consume:")]
    assert all(commit_idx < ci for ci in consume_indices), (
        f"consume calls happened before commit: call_log={call_log}"
    )
