"""Revision-vector and derivation-outbox contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.schemas.auth import ProfileUpdateRequest
from app.schemas.social import BlockRequest, PrivacyUpdateRequest
from app.services import profile as profile_service
from app.services import social as social_service
from app.services.derivation_outbox import (
    OUTBOX_MAX_ATTEMPTS,
    DerivationEvent,
    claim_outbox_events,
    consume_outbox_event,
    run_cleanup_consumer_round,
    should_apply_event,
)
from app.services.revisions import (
    RevisionKind,
    RevisionVector,
    increment_revision_and_enqueue,
)


class _WriteResult:
    def __init__(self, *, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _MappingResult:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _MappingResult:
        return self

    def one(self) -> dict[str, Any]:
        assert self._row is not None
        return self._row

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row


class _MappingRowsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingRowsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class RevisionRecordingSession:
    def __init__(self, *, receipt_inserted: bool = True) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.receipt_inserted = receipt_inserted

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _WriteResult | _MappingResult:
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        if "FROM user_revision_state" in sql:
            return _MappingResult(
                {
                    "profile_revision": 1,
                    "preference_revision": 0,
                    "privacy_revision": 0,
                    "relationship_revision": 0,
                    "policy_revision": 1,
                }
            )
        if "INSERT" in sql and "derivation_consumer_receipt" in sql:
            return _WriteResult(rowcount=1 if self.receipt_inserted else 0)
        return _WriteResult()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        # 独立事务消费失败时回滚：fake session 无挂起写入需撤销。
        pass

    async def __aenter__(self) -> "RevisionRecordingSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _event(
    revision: RevisionVector | None = None, *, attempt_count: int = 0
) -> DerivationEvent:
    return DerivationEvent(
        event_id="2c5998d2-58fc-4a5f-a184-48ac8f0f72f3",
        aggregate_type="user",
        aggregate_id=10,
        event_type="profile_updated",
        changed_fields=("avatar",),
        source_revision=revision
        or RevisionVector(
            profile=1,
            preference=0,
            privacy=0,
            relationship=0,
            policy=1,
        ),
        occurred_at=datetime(2026, 8, 8, tzinfo=UTC),
        priority=50,
        attempt_count=attempt_count,
    )


def test_older_event_cannot_overwrite_newer_projection() -> None:
    current = RevisionVector(profile=8, preference=4, privacy=3, relationship=2, policy=1)
    event = RevisionVector(profile=7, preference=4, privacy=3, relationship=2, policy=1)

    assert should_apply_event(event, current) is False


def test_event_is_current_only_when_the_full_revision_vector_matches() -> None:
    event = RevisionVector(profile=8, preference=4, privacy=3, relationship=2, policy=1)

    assert should_apply_event(event, event) is True
    assert should_apply_event(
        event,
        RevisionVector(profile=8, preference=5, privacy=3, relationship=2, policy=1),
    ) is False


@pytest.mark.asyncio
async def test_revision_and_outbox_share_the_callers_transaction() -> None:
    db = RevisionRecordingSession()

    revision = await increment_revision_and_enqueue(
        db,
        user_id=10,
        kind=RevisionKind.PROFILE,
        changed_fields=("avatar",),
        event_type="profile_updated",
    )

    assert revision == RevisionVector(
        profile=1,
        preference=0,
        privacy=0,
        relationship=0,
        policy=1,
    )
    assert db.commits == 0
    outbox_params = next(
        params
        for sql, params in db.calls
        if "INSERT INTO derivation_outbox" in sql
    )
    assert json.loads(outbox_params["payload_minimal"]) == {
        "changed_fields": ["avatar"],
        "source_revision": revision.as_dict(),
        "subject_id": 10,
    }


@pytest.mark.asyncio
async def test_consumer_executes_current_event_once() -> None:
    db = RevisionRecordingSession()
    handled: list[str] = []

    async def handler(event: DerivationEvent) -> None:
        handled.append(event.event_id)

    result = await consume_outbox_event(db, _event(), "projection", handler)

    assert result.status == "succeeded"
    assert result.applied is True
    assert handled == ["2c5998d2-58fc-4a5f-a184-48ac8f0f72f3"]
    assert db.commits == 0


@pytest.mark.asyncio
async def test_consumer_writes_terminal_outbox_status_and_receipt_outcome() -> None:
    db = RevisionRecordingSession()

    async def handler(event: DerivationEvent) -> None:
        return None

    result = await consume_outbox_event(db, _event(), "projection", handler)

    assert result.status == "succeeded"
    assert result.outcome == "processed"
    receipt_sql = next(
        sql for sql, _ in db.calls if "derivation_consumer_receipt" in sql
    )
    assert "outcome" in receipt_sql
    assert any(
        "status = 'succeeded'" in sql and "payload_minimal = NULL" in sql
        for sql, _ in db.calls
    )


@pytest.mark.asyncio
async def test_consumer_does_not_execute_an_existing_receipt_twice() -> None:
    db = RevisionRecordingSession(receipt_inserted=False)
    handled: list[str] = []

    async def handler(event: DerivationEvent) -> None:
        handled.append(event.event_id)

    result = await consume_outbox_event(db, _event(), "projection", handler)

    assert result.status == "duplicate"
    assert result.applied is False
    assert handled == []


@pytest.mark.asyncio
async def test_unknown_event_is_dead_lettered_and_never_claimed_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnknownEventSession(RevisionRecordingSession):
        def __init__(self) -> None:
            super().__init__()
            self.status = "pending"
            self.receipts: set[tuple[str, str]] = set()

        async def execute(self, statement: object, params: dict[str, Any] | None = None):
            sql = str(statement)
            values = dict(params or {})
            self.calls.append((sql, values))
            if "FROM derivation_outbox" in sql and "LEFT JOIN derivation_consumer_receipt" in sql:
                return _MappingRowsResult([
                    {
                        "event_id": "unknown-event",
                        "aggregate_type": "user",
                        "aggregate_id": 10,
                        "event_type": "future_event_type",
                        "changed_fields": '["future"]',
                        "source_revision_json": json.dumps(_event().source_revision.as_dict()),
                        "occurred_at": datetime(2026, 8, 8, tzinfo=UTC),
                        "priority": 50,
                        "lease_until": None,
                        "payload_minimal": json.dumps({"resource_id": "r-1"}),
                    }
                ] if self.status == "pending" else [])
            if "UPDATE derivation_outbox" in sql:
                if "dead_letter" in sql:
                    self.status = "dead_letter"
                return _WriteResult()
            if "INSERT" in sql and "derivation_consumer_receipt" in sql:
                key = (str(values["event_id"]), str(values["consumer_name"]))
                if key in self.receipts:
                    return _WriteResult(rowcount=0)
                self.receipts.add(key)
                return _WriteResult()
            return _WriteResult()

    db = UnknownEventSession()
    # 每事件独立事务消费：_consume_one_independent 通过 session_factory() 开新
    # session。patch 成复用同一 fake session，使 dead_letter 的 UPDATE 落到
    # 同一 status 上，第二轮 claim 才能读到 status != 'pending' 不再领取。
    from app.db import session as session_module

    def _fake_factory() -> UnknownEventSession:
        return db

    monkeypatch.setattr(session_module, "session_factory", _fake_factory)
    first = await run_cleanup_consumer_round(
        db, "worker-1", datetime(2026, 8, 8, tzinfo=UTC), limit=1
    )
    second = await run_cleanup_consumer_round(
        db, "worker-1", datetime(2026, 8, 8, 0, 1, tzinfo=UTC), limit=1
    )

    assert first["dead_letter"] == 1
    assert second["claimed"] == 0
    assert db.status == "dead_letter"
    assert any("DERIVATION_UNKNOWN_EVENT_TYPE" in str(params) for _, params in db.calls)


@pytest.mark.asyncio
async def test_transient_outbox_failure_dead_letters_after_finite_attempts() -> None:
    db = RevisionRecordingSession()
    calls = 0

    async def handler(event: DerivationEvent) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("transient provider failure")

    results = [
        await consume_outbox_event(
            db, _event(attempt_count=attempt), "projection", handler
        )
        for attempt in range(1, OUTBOX_MAX_ATTEMPTS + 1)
    ]

    assert calls == OUTBOX_MAX_ATTEMPTS
    assert results[-1].status == "dead_letter"
    assert results[-1].outcome == "dead_letter"


@pytest.mark.asyncio
async def test_claim_outbox_events_leases_unprocessed_rows_for_one_consumer() -> None:
    class ClaimingSession(RevisionRecordingSession):
        async def execute(
            self, statement: object, params: dict[str, Any] | None = None
        ) -> _WriteResult | _MappingRowsResult:
            sql = str(statement)
            values = dict(params or {})
            self.calls.append((sql, values))
            if "FROM derivation_outbox" in sql:
                return _MappingRowsResult(
                    [
                        {
                            "event_id": "2c5998d2-58fc-4a5f-a184-48ac8f0f72f3",
                            "aggregate_type": "user",
                            "aggregate_id": 10,
                            "event_type": "profile_updated",
                            "changed_fields": '["avatar"]',
                            "source_revision_json": (
                                '{"profile":1,"preference":0,"privacy":0,'
                                '"relationship":0,"policy":1}'
                            ),
                            "occurred_at": datetime(2026, 8, 8, tzinfo=UTC),
                            "priority": 50,
                        }
                    ]
                )
            return _WriteResult()

    db = ClaimingSession()

    events = await claim_outbox_events(
        db,
        consumer_name="projection",
        worker_id="worker-1",
        now=datetime(2026, 8, 8, tzinfo=UTC),
        limit=1,
    )

    assert events == [
        DerivationEvent(
            event_id="2c5998d2-58fc-4a5f-a184-48ac8f0f72f3",
            aggregate_type="user",
            aggregate_id=10,
            event_type="profile_updated",
            changed_fields=("avatar",),
            source_revision=RevisionVector(
                profile=1,
                preference=0,
                privacy=0,
                relationship=0,
                policy=1,
            ),
            occurred_at=datetime(2026, 8, 8, tzinfo=UTC),
            priority=50,
            status="processing",
            attempt_count=1,
        )
    ]
    assert any(
        "LEFT JOIN derivation_consumer_receipt" in sql for sql, _ in db.calls
    )
    assert any(
        "lease_until" in sql and "derivation_consumer_receipt" in sql
        for sql, _ in db.calls
    )


@pytest.mark.asyncio
async def test_profile_mutation_enqueues_before_its_existing_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MutationSession:
        def __init__(self) -> None:
            self.order: list[str] = []

        async def execute(
            self, statement: object, _params: dict[str, Any] | None = None
        ) -> _WriteResult:
            self.order.append(str(statement))
            return _WriteResult()

        async def commit(self) -> None:
            self.order.append("COMMIT")

    db = MutationSession()
    events: list[tuple[int, RevisionKind, tuple[str, ...], str, int]] = []

    async def fake_recalculate(_db: object, _user_id: int) -> float:
        return 100.0

    async def fake_profile(_db: object, _user_id: int) -> dict[str, Any]:
        return {"user_id": 10}

    async def fake_increment(
        _db: object,
        user_id: int,
        kind: RevisionKind,
        changed_fields: tuple[str, ...],
        event_type: str,
        priority: int = 50,
    ) -> RevisionVector:
        db.order.append("ENQUEUE")
        events.append((user_id, kind, changed_fields, event_type, priority))
        return RevisionVector(profile=1, preference=0, privacy=0, relationship=0, policy=1)

    monkeypatch.setattr(profile_service, "recalculate_completion", fake_recalculate)
    monkeypatch.setattr(profile_service, "get_profile", fake_profile)
    monkeypatch.setattr(profile_service, "increment_revision_and_enqueue", fake_increment)

    await profile_service.update_profile(db, 10, ProfileUpdateRequest(height=180))

    assert events == [
        (10, RevisionKind.PROFILE, ("height",), "profile_updated", 50)
    ]
    assert db.order[-2:] == ["ENQUEUE", "COMMIT"]


@pytest.mark.asyncio
async def test_privacy_mutation_enqueues_high_priority_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MutationSession:
        def __init__(self) -> None:
            self.order: list[str] = []

        async def execute(
            self, statement: object, _params: dict[str, Any] | None = None
        ) -> _WriteResult:
            self.order.append(str(statement))
            return _WriteResult()

        async def commit(self) -> None:
            self.order.append("COMMIT")

    db = MutationSession()
    events: list[tuple[int, RevisionKind, tuple[str, ...], str, int]] = []

    async def fake_privacy(_db: object, _user_id: int) -> object:
        return object()

    async def fake_increment(
        _db: object,
        user_id: int,
        kind: RevisionKind,
        changed_fields: tuple[str, ...],
        event_type: str,
        priority: int = 50,
    ) -> RevisionVector:
        db.order.append("ENQUEUE")
        events.append((user_id, kind, changed_fields, event_type, priority))
        return RevisionVector(profile=0, preference=0, privacy=1, relationship=0, policy=1)

    monkeypatch.setattr(social_service, "get_privacy", fake_privacy)
    monkeypatch.setattr(social_service, "increment_revision_and_enqueue", fake_increment)

    await social_service.update_privacy(
        db,
        10,
        PrivacyUpdateRequest(show_profile=False),
    )

    assert events == [
        (10, RevisionKind.PRIVACY, ("show_profile",), "privacy_updated", 10)
    ]
    assert db.order[-2:] == ["ENQUEUE", "COMMIT"]


@pytest.mark.asyncio
async def test_block_invalidation_covers_both_members_of_the_relationship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MutationSession:
        def __init__(self) -> None:
            self.order: list[str] = []

        async def execute(
            self, statement: object, _params: dict[str, Any] | None = None
        ) -> _WriteResult:
            self.order.append(str(statement))
            return _WriteResult()

        async def commit(self) -> None:
            self.order.append("COMMIT")

    db = MutationSession()
    events: list[tuple[int, RevisionKind, tuple[str, ...], str, int]] = []

    async def fake_ensure_target(_db: object, _user_id: int, _target_id: int) -> None:
        return None

    async def fake_increment(
        _db: object,
        user_id: int,
        kind: RevisionKind,
        changed_fields: tuple[str, ...],
        event_type: str,
        priority: int = 50,
    ) -> RevisionVector:
        db.order.append("ENQUEUE")
        events.append((user_id, kind, changed_fields, event_type, priority))
        return RevisionVector(profile=0, preference=0, privacy=0, relationship=1, policy=1)

    monkeypatch.setattr(social_service, "_ensure_target", fake_ensure_target)
    monkeypatch.setattr(social_service, "increment_revision_and_enqueue", fake_increment)

    await social_service.set_block(
        db,
        user_id=10,
        target_id=11,
        request=BlockRequest(reason="no-contact"),
        enabled=True,
    )

    assert events == [
        (10, RevisionKind.RELATIONSHIP, ("block",), "relationship_blocked", 10),
        (11, RevisionKind.RELATIONSHIP, ("block",), "relationship_blocked", 10),
    ]
    assert db.order[-3:] == ["ENQUEUE", "ENQUEUE", "COMMIT"]
