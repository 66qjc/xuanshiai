"""Worker lease safety regressions."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.workers import ai_worker as worker_mod


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_round_claims_next_task_only_after_previous_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    tasks = [
        SimpleNamespace(
            task_id=f"task-{index}",
            task_type="profile_extract",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        for index in (1, 2)
    ]

    class _SessionFactory:
        def __call__(self) -> _Session:
            return _Session()

    async def fake_reap(*args: object, **kwargs: object) -> list[str]:
        return []

    async def fake_claim(
        db: object, worker_id: str, now: datetime, limit: int
    ) -> list[SimpleNamespace]:
        assert limit == 1
        index = len([event for event in events if event.startswith("claim-")]) + 1
        events.append(f"claim-{index}")
        return tasks[index - 1 : index]

    async def fake_process(
        db: object, task: SimpleNamespace, worker_id: str, **kwargs: object
    ) -> str:
        events.append(f"process-{task.task_id.removeprefix('task-')}")
        return "completed"

    monkeypatch.setattr(worker_mod, "session_factory", _SessionFactory())
    monkeypatch.setattr(worker_mod, "TASK_HANDLERS", {"profile_extract": object()})
    monkeypatch.setattr(worker_mod, "reap_expired_leases", fake_reap)
    monkeypatch.setattr(worker_mod, "claim_tasks", fake_claim)
    monkeypatch.setattr(worker_mod, "_process", fake_process)

    claimed, completed, failed = await worker_mod._run_round("worker-1", batch_size=2)

    assert events == ["claim-1", "process-1", "claim-2", "process-2"]
    assert (claimed, completed, failed) == (2, 2, 0)
