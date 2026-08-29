"""Worker finalize gate ordering tests.

Plan Task 5 / G2-C (AI-P0-07): verify that the finalize gate
(feature flag, consent, revision check in ``complete_task``) runs
*before* the handler's business side effects are committed.

The original defect: ``_run_with_heartbeat.invoke_handler`` runs the
handler in its own ``handler_db`` session and unconditionally calls
``await handler_db.commit()`` right after the handler returns
(``app/workers/ai_worker.py``). Only afterwards does ``_process`` call
``complete_task`` in a *separate* ``finalize_db`` session, which is
where the consent/revision gate lives. If consent is revoked in the
window between the handler commit and the finalize gate check, the
task is marked ``superseded`` — but the handler's business writes
(e.g. a new draft row) are already committed and visible.

Contract under test: when ``complete_task`` is invoked (i.e. the
finalize gate is about to run), the handler's business session must
NOT have committed yet. The gate and the business commit must be
ordered so the gate precedes (or is atomic with) the commit, on both
the superseded and the succeeded paths.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import ResourceClosedError

from app.schemas.ai_common import AiTaskStatus
from app.services.ai.tasks import AiTaskRecord
from app.services.revisions import RevisionVector


def _make_task(
    *,
    task_id: str = "task-1",
    status: AiTaskStatus | str = AiTaskStatus.RUNNING,
    owner: int = 42,
    lease_owner: str = "worker-a",
) -> AiTaskRecord:
    if isinstance(status, str):
        status = AiTaskStatus(status)
    return AiTaskRecord(
        id=1,
        task_id=task_id,
        owner_user_id=owner,
        task_type="profile_extract",
        scene="profile_extract",
        idempotency_key="key-1",
        request_digest="hash-1",
        status=status,
        stage=None,
        progress_percent=None,
        attempt_count=1,
        max_attempts=3,
        next_run_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        lease_owner=lease_owner,
        lease_until=datetime(2026, 8, 16, 12, 0, tzinfo=UTC),
        consent_snapshot_json={
            "scope": "profile_text_extract",
            "version": "profile-text-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
        },
        source_revision_json=RevisionVector(profile=1).as_dict(),
        payload_summary={"session_id": "sess-1", "turn_id": "turn-1"},
        error_code=None,
        error_message=None,
        result_ref=None,
        created_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        started_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
        finished_at=None,
    )


class _TrackedSession:
    """A minimal async-session double that counts commits.

    Each instance behaves as one independent DB session (handler_db vs
    finalize_db), matching how ``_process`` uses ``session_provider``.
    """

    def __init__(
        self,
        label: str,
        store: "_TrackedStore",
        *,
        fail_commit: bool = False,
    ) -> None:
        self.label = label
        self.store = store
        self.fail_commit = fail_commit
        self.commit_count = 0
        self.rollback_count = 0
        self.begin_nested_count = 0
        self.current_nested: _TrackedNestedTransaction | None = None
        self.staged_business: list[str] = []
        self.staged_task_updates: list[dict[str, Any]] = []

    def stage_business(self, value: str) -> None:
        self.staged_business.append(value)

    def stage_task_update(self, value: dict[str, Any]) -> None:
        self.staged_task_updates.append(value)

    async def execute(self, statement: Any, params: Any = None) -> MagicMock:
        if "INSERT INTO ai_profile_draft" in str(statement):
            self.stage_business("draft-1")
        return MagicMock()

    async def commit(self) -> None:
        self.commit_count += 1
        if self.fail_commit:
            raise RuntimeError("handler commit failed")
        self.store.business_writes.extend(self.staged_business)
        self.store.task_updates.extend(self.staged_task_updates)
        self.staged_business.clear()
        self.staged_task_updates.clear()
        if self.current_nested is not None:
            self.current_nested.is_active = False

    async def rollback(self) -> None:
        self.rollback_count += 1
        self.staged_business.clear()
        self.staged_task_updates.clear()

    def begin_nested(self) -> "_TrackedNestedTransaction":
        self.begin_nested_count += 1
        self.current_nested = _TrackedNestedTransaction(self)
        return self.current_nested

    async def __aenter__(self) -> _TrackedSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _TrackedSessionProvider:
    """Yields a fresh ``_TrackedSession`` on each ``()`` call."""

    def __init__(self, *, fail_commit_label: str | None = None) -> None:
        self.sessions: list[_TrackedSession] = []
        self.store = _TrackedStore()
        self.fail_commit_label = fail_commit_label

    def __call__(self) -> _TrackedSession:
        label = f"session-{len(self.sessions)}"
        session = _TrackedSession(
            label,
            self.store,
            fail_commit=label == self.fail_commit_label,
        )
        self.sessions.append(session)
        return session


class _TrackedStore:
    def __init__(self) -> None:
        self.business_writes: list[str] = []
        self.task_updates: list[dict[str, Any]] = []


class _TrackedNestedTransaction:
    def __init__(self, session: _TrackedSession) -> None:
        self.session = session
        self.rolled_back = False
        self.committed = False
        self.is_active = True

    async def start(self, is_ctxmanager: bool = False) -> "_TrackedNestedTransaction":
        return self

    async def rollback(self) -> None:
        if not self.is_active:
            raise ResourceClosedError("This transaction is closed")
        self.rolled_back = True
        self.is_active = False
        # The handler savepoint only contains business writes at the point
        # where the supersede callback runs.  Completion writes are added to
        # the outer transaction after this rollback.
        self.session.staged_business.clear()

    async def commit(self) -> None:
        if not self.is_active:
            raise ResourceClosedError("This transaction is closed")
        self.committed = True
        self.is_active = False


def _handler_session(provider: _TrackedSessionProvider) -> _TrackedSession:
    assert len(provider.sessions) >= 2, "handler session was not opened"
    return provider.sessions[1]


def _build_process_patches(
    *,
    fake_handler: Any,
    fake_complete_task: Any,
    task: AiTaskRecord,
):
    return (
        patch("app.workers.ai_worker.TASK_HANDLERS", {"profile_extract": fake_handler}),
        patch("app.workers.ai_worker.complete_task", side_effect=fake_complete_task),
        patch("app.workers.ai_worker.start_task", return_value=task),
    )


# ---------------------------------------------------------------------------
# Test 1 — handler business commit must not precede the finalize gate
# (superseded path: consent revoked between handler run and finalize)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_session_not_committed_when_gate_supersedes() -> None:
    """When ``complete_task`` returns a ``superseded`` task (consent
    revoked / revision changed between handler run and finalize), the
    handler session must not have committed its business writes.

    This is the AI-P0-07 contract: the gate and the business write
    must be atomic. If ``invoke_handler`` commits before the gate runs
    in ``complete_task``, revoking consent in that window leaks the
    handler's business side effects (draft rows, session status flips).
    """
    provider = _TrackedSessionProvider()
    task = _make_task()
    handler_session_ref: dict[str, _TrackedSession | None] = {"db": None}
    handler_commit_count_at_gate: list[int] = []

    async def fake_handler(db: Any, task: Any, worker_id: str) -> Any:
        # Capture the handler's own session so the finalize step can
        # inspect whether it has already committed.
        handler_session_ref["db"] = db
        # Simulate a business write (draft insert / session update).
        await db.execute("INSERT INTO ai_profile_draft ...")
        return ("profile-draft:draft-1", RevisionVector(profile=1))

    async def fake_complete_task(
        db: Any,
        task_id: str,
        worker_id: str,
        result_ref: str,
        revisions: Any,
        *,
        before_supersede: Any = None,
        before_not_applied: Any = None,
    ) -> AiTaskRecord:
        handler_db = handler_session_ref["db"]
        assert handler_db is not None, "handler did not run before complete_task"
        assert db is handler_db, "completion must share the handler transaction"
        handler_commit_count_at_gate.append(handler_db.commit_count)
        if before_supersede is not None:
            await before_supersede()
        db.stage_task_update({"task_id": task_id, "status": "superseded"})
        # Gate finds consent revoked -> superseded (terminal, no result write).
        return _make_task(task_id=task_id, status=AiTaskStatus.SUPERSEDED)

    with ExitStack() as _stack:
        for _p in _build_process_patches(
            fake_handler=fake_handler,
            fake_complete_task=fake_complete_task,
            task=task,
        ):
            _stack.enter_context(_p)
        from app.workers.ai_worker import _process

        result = await _process(
            MagicMock(), task, worker_id="worker-a", session_provider=provider
        )

    assert result == "completed", f"unexpected _process result: {result}"
    assert handler_commit_count_at_gate, "complete_task (the finalize gate) was never called"
    assert handler_commit_count_at_gate[0] == 0, (
        f"invoke_handler committed the handler's business writes "
        f"({handler_commit_count_at_gate[0]} commit(s)) BEFORE complete_task "
        "ran the consent/revision gate. If consent is revoked in that window, "
        "the handler's business side effects are already visible — "
        "this is the AI-P0-07 defect."
    )
    assert provider.store.business_writes == [], (
        "superseded completion must discard the handler business write"
    )
    assert provider.store.task_updates == [
        {"task_id": "task-1", "status": "superseded"}
    ], "the superseded task terminal update must commit"
    assert _handler_session(provider).begin_nested_count == 1


# ---------------------------------------------------------------------------
# Test 2 — on the happy path, the finalize gate must run before the
# handler's business writes become durable (gate precedes commit).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_runs_before_handler_commit_on_success() -> None:
    """On the happy path (gate passes, task -> succeeded), the finalize
    gate must be evaluated before the handler's business writes become
    durable. Concretely: when ``complete_task`` is entered, the handler
    session's commit count must still be 0. The fix must ensure the
    gate runs before, or atomically with, the business commit.
    """
    provider = _TrackedSessionProvider()
    task = _make_task()
    handler_session_ref: dict[str, _TrackedSession | None] = {"db": None}
    handler_commit_count_at_gate: list[int] = []

    async def fake_handler(db: Any, task: Any, worker_id: str) -> Any:
        handler_session_ref["db"] = db
        await db.execute("INSERT INTO ai_profile_draft ...")
        # Note: we do NOT call db.commit() here — invoke_handler does.
        return ("profile-draft:draft-1", RevisionVector(profile=1))

    async def fake_complete_task(
        db: Any,
        task_id: str,
        worker_id: str,
        result_ref: str,
        revisions: Any,
        *,
        before_supersede: Any = None,
        before_not_applied: Any = None,
    ) -> AiTaskRecord:
        handler_db = handler_session_ref["db"]
        assert handler_db is not None, "handler did not run before complete_task"
        assert db is handler_db, "completion must share the handler transaction"
        handler_commit_count_at_gate.append(handler_db.commit_count)
        db.stage_task_update({"task_id": task_id, "status": "succeeded", "result_ref": result_ref})
        return _make_task(task_id=task_id, status=AiTaskStatus.SUCCEEDED)

    with ExitStack() as _stack:
        for _p in _build_process_patches(
            fake_handler=fake_handler,
            fake_complete_task=fake_complete_task,
            task=task,
        ):
            _stack.enter_context(_p)
        from app.workers.ai_worker import _process

        result = await _process(
            MagicMock(), task, worker_id="worker-a", session_provider=provider
        )

    assert result == "completed"
    assert handler_commit_count_at_gate, "complete_task was never called"
    assert handler_commit_count_at_gate[0] == 0, (
        f"Handler session committed business writes "
        f"({handler_commit_count_at_gate[0]} commit(s)) before the finalize gate "
        "ran in complete_task. The gate and the business commit must be "
        "ordered so the gate precedes (or is atomic with) the commit."
    )
    assert provider.store.business_writes == ["draft-1"]
    assert provider.store.task_updates == [
        {"task_id": "task-1", "status": "succeeded", "result_ref": "profile-draft:draft-1"}
    ]
    assert _handler_session(provider).begin_nested_count == 1


@pytest.mark.asyncio
async def test_handler_commit_failure_does_not_leave_succeeded_task() -> None:
    """A failed outer handler commit rolls back both business and task writes."""
    provider = _TrackedSessionProvider(fail_commit_label="session-1")
    task = _make_task()
    handler_session_ref: dict[str, _TrackedSession | None] = {"db": None}

    async def fake_handler(db: Any, task: Any, worker_id: str) -> Any:
        handler_session_ref["db"] = db
        await db.execute("INSERT INTO ai_profile_draft ...")
        return ("profile-draft:draft-1", RevisionVector(profile=1))

    async def fake_complete_task(
        db: Any,
        task_id: str,
        worker_id: str,
        result_ref: str,
        revisions: Any,
        *,
        before_supersede: Any = None,
        before_not_applied: Any = None,
    ) -> AiTaskRecord:
        assert db is handler_session_ref["db"]
        db.stage_task_update({"task_id": task_id, "status": "succeeded", "result_ref": result_ref})
        return _make_task(task_id=task_id, status=AiTaskStatus.SUCCEEDED)

    with ExitStack() as _stack:
        for _p in _build_process_patches(
            fake_handler=fake_handler,
            fake_complete_task=fake_complete_task,
            task=task,
        ):
            _stack.enter_context(_p)
        from app.workers.ai_worker import _process

        with pytest.raises(RuntimeError, match="handler commit failed"):
            await _process(
                MagicMock(), task, worker_id="worker-a", session_provider=provider
            )

    handler_db = _handler_session(provider)
    assert handler_db.rollback_count >= 1
    assert provider.store.business_writes == []
    assert provider.store.task_updates == []


@pytest.mark.asyncio
async def test_existing_terminal_task_discards_late_handler_business_write() -> None:
    """A terminal replay must not authorize this worker's staged output."""
    provider = _TrackedSessionProvider()
    task = _make_task()

    async def fake_handler(db: Any, task: Any, worker_id: str) -> Any:
        await db.execute("INSERT INTO ai_profile_draft ...")
        return ("profile-draft:late", RevisionVector(profile=1))

    async def fake_complete_task(
        db: Any,
        task_id: str,
        worker_id: str,
        result_ref: str,
        revisions: Any,
        *,
        before_supersede: Any = None,
        before_not_applied: Any = None,
    ) -> AiTaskRecord:
        if before_not_applied is not None:
            await before_not_applied()
        return _make_task(task_id=task_id, status=AiTaskStatus.SUCCEEDED)

    with ExitStack() as _stack:
        for _p in _build_process_patches(
            fake_handler=fake_handler,
            fake_complete_task=fake_complete_task,
            task=task,
        ):
            _stack.enter_context(_p)
        from app.workers.ai_worker import _process

        result = await _process(
            MagicMock(), task, worker_id="worker-a", session_provider=provider
        )

    assert result == "completed"
    assert provider.store.business_writes == []
    assert provider.store.task_updates == []
    assert _handler_session(provider).rollback_count >= 1


@pytest.mark.asyncio
async def test_handler_self_commit_then_none_does_not_rollback_closed_savepoint() -> None:
    """Legacy non-retryable handlers may commit failure state then return None."""
    provider = _TrackedSessionProvider()
    task = _make_task()

    async def fake_handler(db: Any, task: Any, worker_id: str) -> Any:
        db.stage_task_update({"task_id": task.task_id, "status": "failed"})
        await db.commit()
        return None

    async def fake_fail_task(
        db: Any,
        task_id: str,
        worker_id: str,
        *,
        error_code: str,
        retryable: bool,
    ) -> AiTaskRecord:
        return _make_task(task_id=task_id, status=AiTaskStatus.FAILED)

    with (
        patch("app.workers.ai_worker.TASK_HANDLERS", {"profile_extract": fake_handler}),
        patch("app.workers.ai_worker.start_task", return_value=task),
        patch("app.workers.ai_worker.fail_task", side_effect=fake_fail_task),
    ):
        from app.workers.ai_worker import _process

        result = await _process(
            MagicMock(), task, worker_id="worker-a", session_provider=provider
        )

    assert result == "failed"
    assert provider.store.task_updates == [
        {"task_id": "task-1", "status": "failed"}
    ]
