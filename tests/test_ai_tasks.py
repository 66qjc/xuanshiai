"""AI-CORE Task 6 acceptance contract: task state machine, leases, cancel and recovery.

The three Step 1 tests are mirrored verbatim from the task brief.  ``task_store``
is an in-memory fake session that routes the service's SQL by substring and
enforces the same unique-constraint and conditional-update semantics, so the
whole state machine can be exercised without a real database.  The API tests
override ``get_current_user``/``get_db`` and drive the routes through the
registered TestClient.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.schemas.ai_common import AiTaskStatus
from app.services.ai.tasks import (
    AiTaskRecord,
    TaskError,
    assert_transition,
    claim_tasks,
    complete_task,
    enqueue_task,
    fail_task,
    heartbeat_lease,
    reap_expired_leases,
    request_cancel,
    start_task,
)
from app.services.revisions import RevisionVector
from app.workers import ai_worker as worker_mod
from app.workers.ai_worker import main as worker_main

client = TestClient(app)


# ----------------------------------------------------------------------
# 内存 task store（task_store fixture）
# ----------------------------------------------------------------------

_FULL_REVISION = {
    "profile": 1,
    "preference": 0,
    "privacy": 0,
    "relationship": 0,
    "policy": 1,
}


def _to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return value


class _MappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _WriteResult:
    def __init__(self, *, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class FakeAiSession:
    """Routes the service SQL by substring onto one in-memory TaskStore."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _MappingResult | _WriteResult:
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        # 后门禁复查（缺陷 2 修复）无条件执行：complete_task 在写结果前会
        # 重新读取 user_revision_state 与（当 consent 快照带 scope 时）
        # ai_consent_grant，对比版本向量与授权是否仍匹配。FakeAiSession 必
        # 须为这两条只读 SELECT 返回合理的 mock 行，否则触发 unhandled sql。
        if "FROM user_revision_state" in sql:
            # 默认返回与 _FULL_REVISION 一致的版本向量，使版本匹配的完成
            # 用例走 succeeded；supersede 用例靠调用方传入不同的 revisions
            # 参数触发，而不是靠这里返回不同的 current。
            return _MappingResult(
                [
                    {
                        "profile_revision": 1,
                        "preference_revision": 0,
                        "privacy_revision": 0,
                        "relationship_revision": 0,
                        "policy_revision": 1,
                    }
                ]
            )
        if "FROM ai_consent_grant" in sql:
            # consent 快照匹配。scrub 用例的 consent_snapshot_json=
            # {"scope": "profile_text_extract"} 不含 version/policy_revision，
            # 因此 consent_matches 要求 current_consent 非 None 且 version
            # 等于空串、policy_revision 检查因 snapshot 无 policy_revision
            # 被跳过。返回一行 version="" 满足匹配。
            return _MappingResult([{"version": "", "policy_revision": None, "granted_at": None}])
        if "INSERT INTO ai_task" in sql:
            inserted = self._store.insert(values)
            return _WriteResult(rowcount=1 if inserted else 0)
        if "FROM ai_task" in sql and "status IN ('queued', 'retry_wait')" in sql:
            return _MappingResult(
                self._store.claimable_rows(values["now"], int(values["limit"]))
            )
        if "FROM ai_task" in sql and "status IN ('leased', 'running')" in sql:
            return _MappingResult(
                self._store.expired_rows(values["now"], int(values["limit"]))
            )
        if "FROM ai_task" in sql and "WHERE task_id = :task_id" in sql:
            row = self._store.tasks.get(values["task_id"])
            return _MappingResult([row] if row else [])
        if "FROM ai_task" in sql and "owner_user_id = :owner_user_id" in sql:
            row = self._store.find_by_idempotency(
                int(values["owner_user_id"]),
                str(values["task_type"]),
                str(values["idempotency_key"]),
            )
            return _MappingResult([row] if row else [])
        if sql.startswith("UPDATE ai_task"):
            applied = self._store.apply_update(sql, values)
            return _WriteResult(rowcount=1 if applied else 0)
        raise AssertionError(f"unhandled sql: {sql}")

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class TaskStore:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.session = FakeAiSession(self)

    # ---- query helpers shared by the fake session ----------------------

    def find_by_idempotency(
        self, owner_user_id: int, task_type: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        for row in self.tasks.values():
            if (
                row["owner_user_id"] == owner_user_id
                and row["task_type"] == task_type
                and row["idempotency_key"] == idempotency_key
            ):
                return row
        return None

    def claimable_rows(self, now: datetime, limit: int) -> list[dict[str, Any]]:
        eligible = [
            row
            for row in self.tasks.values()
            if row["status"] in ("queued", "retry_wait")
            and (row["next_run_at"] is None or row["next_run_at"] <= now)
            and (
                row["lease_owner"] is None
                or row["lease_until"] is None
                or row["lease_until"] < now
            )
        ]
        eligible.sort(key=lambda row: row["created_at"])
        return eligible[:limit]

    def expired_rows(self, now: datetime, limit: int) -> list[dict[str, Any]]:
        eligible = [
            row
            for row in self.tasks.values()
            if row["status"] in ("leased", "running")
            and row["lease_until"] is not None
            and row["lease_until"] < now
        ]
        eligible.sort(key=lambda row: row["lease_until"])
        return eligible[:limit]

    def insert(self, params: dict[str, Any]) -> bool:
        existing = self.find_by_idempotency(
            int(params["owner_user_id"]),
            str(params["task_type"]),
            str(params["idempotency_key"]),
        )
        if existing is not None:
            # 模拟 uk_ai_task_owner_type_key 唯一约束冲突。
            raise IntegrityError("INSERT INTO ai_task", params, Exception("Duplicate entry"))
        now = datetime.now(UTC).replace(tzinfo=None)
        task_id = str(params["task_id"])
        self.tasks[task_id] = {
            "id": self._next_id,
            "task_id": task_id,
            "owner_user_id": int(params["owner_user_id"]),
            "task_type": str(params["task_type"]),
            "scene": str(params.get("scene") or params["task_type"]),
            "idempotency_key": str(params["idempotency_key"]),
            "request_digest": params.get("request_digest"),
            "status": "queued",
            "stage": None,
            "attempt_count": 0,
            "max_attempts": int(params.get("max_attempts") or settings.ai_max_attempts),
            "next_run_at": None,
            "lease_owner": None,
            "lease_until": None,
            "consent_snapshot_json": params.get("consent_snapshot_json"),
            "source_revision_json": params.get("source_revision_json"),
            "payload_summary": None,
            "error_code": None,
            "error_message": None,
            "result_ref": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
        }
        self._next_id += 1
        return True

    def apply_update(self, sql: str, params: dict[str, Any]) -> bool:
        row = self.tasks.get(params.get("task_id"))
        if row is None:
            return False
        if "SET status = 'leased'" in sql:
            row["status"] = "leased"
            row["lease_owner"] = params.get("worker_id")
            row["lease_until"] = params.get("lease_until")
        elif "SET status = 'running'" in sql:
            if row["status"] != "leased" or row["lease_owner"] != params.get("worker_id"):
                return False
            row["status"] = "running"
            if row["started_at"] is None:
                row["started_at"] = params.get("now")
        elif "SET status = 'cancelled'" in sql:
            row["status"] = "cancelled"
            row["finished_at"] = params.get("now")
        elif "SET status = 'superseded'" in sql:
            row["status"] = "superseded"
            row["finished_at"] = params.get("now")
            row["lease_owner"] = None
            row["lease_until"] = None
        elif "SET status = 'succeeded'" in sql:
            row["status"] = "succeeded"
            row["result_ref"] = params.get("result_ref")
            row["finished_at"] = params.get("now")
        elif "SET status = 'retry_wait'" in sql:
            row["status"] = "retry_wait"
            row["attempt_count"] = int(params.get("attempt_count") or 0)
            row["next_run_at"] = params.get("next_run_at")
            row["error_code"] = params.get("error_code")
            row["error_message"] = params.get("error_message")
            row["lease_owner"] = None
            row["lease_until"] = None
        elif "SET status = 'failed'" in sql:
            row["status"] = "failed"
            row["error_code"] = params.get("error_code")
            row["error_message"] = params.get("error_message")
            row["finished_at"] = params.get("now")
            row["lease_owner"] = None
            row["lease_until"] = None
        elif sql.startswith("UPDATE ai_task SET lease_until"):
            if row["status"] not in ("running", "leased") or row["lease_owner"] != params.get(
                "worker_id"
            ):
                return False
            row["lease_until"] = params.get("lease_until")
        else:
            raise AssertionError(f"unhandled update: {sql}")
        if "payload_summary = NULL" in sql:
            row["payload_summary"] = None
            row["consent_snapshot_json"] = None
            row["source_revision_json"] = None
            row["result_ref"] = None
        row["updated_at"] = datetime.now(UTC).replace(tzinfo=None)
        return True

    # ---- fixture surface (brief Step 1 semantics) ----------------------

    async def seed(self, **kwargs: Any) -> AiTaskRecord:
        task_id = kwargs.pop("task_id", None) or uuid.uuid4().hex
        now = datetime.now(UTC).replace(tzinfo=None)
        row: dict[str, Any] = {
            "id": self._next_id,
            "task_id": task_id,
            "owner_user_id": int(kwargs.pop("owner_user_id", 10)),
            "task_type": str(kwargs.pop("task_type", "profile_extract")),
            "scene": str(kwargs.pop("scene", "profile_text_extract")),
            "idempotency_key": str(kwargs.pop("idempotency_key", "")),
            "request_digest": kwargs.pop("request_digest", None),
            "status": str(kwargs.pop("status", "queued")),
            "stage": kwargs.pop("stage", None),
            "attempt_count": int(kwargs.pop("attempt_count", 0)),
            "max_attempts": int(kwargs.pop("max_attempts", settings.ai_max_attempts)),
            "next_run_at": _to_dt(kwargs.pop("next_run_at", None)),
            "lease_owner": kwargs.pop("lease_owner", None),
            "lease_until": _to_dt(kwargs.pop("lease_until", None)),
            "consent_snapshot_json": kwargs.pop("consent_snapshot_json", None),
            "source_revision_json": kwargs.pop("source_revision_json", None),
            "payload_summary": kwargs.pop("payload_summary", None),
            "error_code": kwargs.pop("error_code", None),
            "error_message": kwargs.pop("error_message", None),
            "result_ref": kwargs.pop("result_ref", None),
            "created_at": _to_dt(kwargs.pop("created_at", now)),
            "updated_at": _to_dt(kwargs.pop("updated_at", now)),
            "started_at": _to_dt(kwargs.pop("started_at", None)),
            "finished_at": _to_dt(kwargs.pop("finished_at", None)),
        }
        self.tasks[task_id] = row
        self._next_id += 1
        return AiTaskRecord.from_row(row)

    async def get(self, task_id: str) -> AiTaskRecord | None:
        row = self.tasks.get(task_id)
        return AiTaskRecord.from_row(row) if row else None

    async def reap(self, now: Any) -> list[str]:
        return await reap_expired_leases(self.session, _to_dt(now), limit=100)


@pytest.fixture
def task_store() -> TaskStore:
    return TaskStore()


@pytest.fixture(autouse=True)
def _enable_ai_features(monkeypatch: pytest.MonkeyPatch) -> None:
    """门禁复查无条件执行后，complete_task 会调用 require_ai_feature。

    默认配置里 ai_profile_enabled=False，会导致 profile_extract 任务在完成
    时被判 feature disabled 而走 supersede（甚至 leased->superseded 非法转
    换）。本模块的完成类用例语义上假定 AI 功能开启，故统一打开三个 flag。
    """
    monkeypatch.setattr(settings, "ai_master_enabled", True)
    monkeypatch.setattr(settings, "ai_profile_enabled", True)
    monkeypatch.setattr(settings, "ai_search_enabled", True)
    monkeypatch.setattr(settings, "ai_compatibility_shadow_enabled", True)


# ----------------------------------------------------------------------
# Step 1: 状态机
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("source", "target"),
    [("queued", "leased"), ("leased", "running"),
     ("running", "succeeded"), ("running", "retry_wait"),
     ("retry_wait", "leased"), ("running", "superseded")],
)
def test_ai_task_allows_only_registered_transitions(source: str, target: str) -> None:
    assert_transition(AiTaskStatus(source), AiTaskStatus(target))


def test_cancellation_is_legal_from_every_cancellable_window() -> None:
    for source in ("queued", "leased", "running", "retry_wait"):
        assert_transition(AiTaskStatus(source), AiTaskStatus.CANCELLED)


def test_succeeded_task_cannot_return_to_running() -> None:
    with pytest.raises(ValueError, match="illegal ai_task transition"):
        assert_transition(AiTaskStatus.SUCCEEDED, AiTaskStatus.RUNNING)


@pytest.mark.parametrize(
    ("source", "target"),
    [("succeeded", "retry_wait"), ("failed", "leased"), ("cancelled", "running"),
     ("retry_wait", "succeeded"), ("queued", "running"), ("superseded", "failed")],
)
def test_illegal_transitions_raise_with_the_contract_marker(
    source: str, target: str,
) -> None:
    with pytest.raises(ValueError, match="illegal ai_task transition"):
        assert_transition(AiTaskStatus(source), AiTaskStatus(target))


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_once(task_store) -> None:
    task = await task_store.seed(status="leased", lease_until="2026-08-07T07:59:00Z")
    recovered = await task_store.reap(now="2026-08-07T08:00:00Z")
    assert recovered == [task.task_id]
    assert (await task_store.get(task.task_id)).status == "retry_wait"


# ----------------------------------------------------------------------
# 幂等入队
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enqueue_same_key_same_payload_replays_existing_task(task_store) -> None:
    db = task_store.session
    first = await enqueue_task(
        db,
        owner_user_id=10,
        task_type="profile_extract",
        idempotency_key="key-1",
        request_hash="digest-a",
        revisions=RevisionVector(profile=1, policy=1),
        consent={"scope": "profile_text_extract"},
    )
    second = await enqueue_task(
        db,
        owner_user_id=10,
        task_type="profile_extract",
        idempotency_key="key-1",
        request_hash="digest-a",
        revisions=RevisionVector(profile=1, policy=1),
        consent={"scope": "profile_text_extract"},
    )

    assert second.task_id == first.task_id
    assert first.status is AiTaskStatus.QUEUED
    assert len(task_store.tasks) == 1


@pytest.mark.asyncio
async def test_enqueue_same_key_different_payload_raises_conflict(task_store) -> None:
    db = task_store.session
    await enqueue_task(
        db,
        owner_user_id=10,
        task_type="profile_extract",
        idempotency_key="key-1",
        request_hash="digest-a",
    )

    with pytest.raises(TaskError) as excinfo:
        await enqueue_task(
            db,
            owner_user_id=10,
            task_type="profile_extract",
            idempotency_key="key-1",
            request_hash="digest-b",
        )
    assert excinfo.value.code == "TASK_IDEMPOTENCY_CONFLICT"
    assert excinfo.value.status_code == 409
    assert len(task_store.tasks) == 1


@pytest.mark.asyncio
async def test_twenty_concurrent_enqueues_create_one_task(task_store) -> None:
    db = task_store.session

    tasks = await asyncio.gather(
        *[
            enqueue_task(
                db,
                owner_user_id=10,
                task_type="profile_extract",
                idempotency_key="shared-key",
                request_hash="digest-a",
            )
            for _ in range(20)
        ]
    )

    assert {task.task_id for task in tasks} == {tasks[0].task_id}
    assert len(task_store.tasks) == 1


@pytest.mark.asyncio
async def test_enqueue_never_commits_the_callers_transaction(task_store) -> None:
    db = task_store.session
    await enqueue_task(
        db,
        owner_user_id=10,
        task_type="profile_extract",
        idempotency_key="key-c",
        request_hash="digest-a",
    )
    assert db.commits == 0


# ----------------------------------------------------------------------
# 并发领取
# ----------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute)


@pytest.mark.asyncio
async def test_claim_only_claims_due_queued_or_retry_wait_tasks(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    await task_store.seed(status="queued", next_run_at=None)
    await task_store.seed(status="retry_wait", next_run_at="2026-08-07T07:30:00Z")
    await task_store.seed(status="queued", next_run_at="2026-08-07T09:00:00Z")
    await task_store.seed(
        status="running",
        lease_owner="worker-x",
        lease_until=now,
        next_run_at=None,
    )

    claimed = await claim_tasks(db, "worker-1", now, limit=10)

    assert len(claimed) == 2
    assert all(task.status is AiTaskStatus.LEASED for task in claimed)
    assert all(task.lease_owner == "worker-1" for task in claimed)
    assert all(task.lease_until is not None for task in claimed)
    assert db.commits == 0


@pytest.mark.asyncio
async def test_two_workers_never_claim_the_same_task(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    await task_store.seed(status="queued", next_run_at=None)

    first = await claim_tasks(db, "worker-1", now, limit=10)
    second = await claim_tasks(db, "worker-2", now, limit=10)

    assert len(first) == 1
    assert len(second) == 0
    assert first[0].lease_owner == "worker-1"


@pytest.mark.asyncio
async def test_claim_uses_for_update_skip_locked(task_store) -> None:
    db = task_store.session
    await task_store.seed(status="queued", next_run_at=None)

    await claim_tasks(db, "worker-1", _utc(2026, 8, 7, 8, 0), limit=10)

    claim_sql = next(sql for sql, _ in db.calls if "FROM ai_task" in sql and "status IN ('queued'" in sql)
    assert "FOR UPDATE SKIP LOCKED" in claim_sql


# ----------------------------------------------------------------------
# 启动与心跳
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_task_moves_leased_to_running_with_lease_ownership(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until=datetime.now(UTC) + timedelta(minutes=10),
    )

    running = await start_task(db, task.task_id, "worker-1")

    assert running.status is AiTaskStatus.RUNNING


@pytest.mark.asyncio
async def test_start_task_rejects_foreign_lease_owner(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(
        status="leased", lease_owner="worker-1", lease_until="2026-08-07T08:10:00Z"
    )

    with pytest.raises(TaskError) as excinfo:
        await start_task(db, task.task_id, "worker-2")
    assert excinfo.value.code == "TASK_NOT_FOUND"
    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_start_task_rejects_expired_lease(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until="2026-08-07T07:59:00Z",
    )

    with pytest.raises(TaskError) as excinfo:
        await start_task(db, task.task_id, "worker-1")

    assert excinfo.value.code == "TASK_NOT_FOUND"
    assert excinfo.value.status_code == 404
    stored = await task_store.get(task.task_id)
    assert stored is not None
    assert stored.status is AiTaskStatus.LEASED


@pytest.mark.asyncio
async def test_heartbeat_lease_renews_running_task_owned_by_worker(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until="2026-08-07T08:01:00Z",
    )

    await heartbeat_lease(db, task.task_id, "worker-1", now)

    renewed = await task_store.get(task.task_id)
    assert renewed is not None
    assert renewed.lease_until == now + timedelta(seconds=settings.ai_lease_seconds)


@pytest.mark.asyncio
async def test_heartbeat_does_not_renew_a_lease_not_owned(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until="2026-08-07T08:01:00Z",
    )

    await heartbeat_lease(db, task.task_id, "worker-2", now)

    unchanged = await task_store.get(task.task_id)
    assert unchanged is not None
    assert unchanged.lease_until == _utc(2026, 8, 7, 8, 1)


# ----------------------------------------------------------------------
# Worker：长任务心跳续租与未注册 handler 终态（review I-1/I-2）
# ----------------------------------------------------------------------


@pytest.fixture
def preserved_handlers() -> dict[str, Any]:
    """Snapshot ``TASK_HANDLERS`` and restore it after the test.

    Tests that ``clear()``/overwrite ``TASK_HANDLERS`` must take this fixture so
    the global registry is restored afterwards (final review I-1): a naked
    ``clear()`` leaks an empty registry to every later test in the same process,
    deterministically failing the handler-registration tests in
    test_ai_profile_sessions.py / test_ai_search.py.
    """
    saved = dict(worker_mod.TASK_HANDLERS)
    try:
        yield saved
    finally:
        worker_mod.TASK_HANDLERS.clear()
        worker_mod.TASK_HANDLERS.update(saved)


@pytest.mark.asyncio
async def test_worker_heartbeats_lease_while_handler_runs(
    task_store, monkeypatch: pytest.MonkeyPatch, preserved_handlers,
) -> None:
    db = task_store.session
    now = datetime.now(UTC).replace(tzinfo=None)
    task = await task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until=now + timedelta(seconds=settings.ai_lease_seconds),
        source_revision_json=dict(_FULL_REVISION),
    )
    initial = (await task_store.get(task.task_id)).lease_until

    async def slow_handler(_db: Any, _task: AiTaskRecord, _worker_id: str) -> Any:
        await asyncio.sleep(0.05)
        return ("res:heartbeat", RevisionVector(profile=1, policy=1))

    monkeypatch.setattr(worker_mod, "_heartbeat_interval", lambda: 0.01)
    worker_mod.TASK_HANDLERS["profile_extract"] = slow_handler
    outcome = await worker_mod._process(db, task, "worker-1")

    final = await task_store.get(task.task_id)
    assert outcome == "completed"
    assert final is not None
    assert final.status is AiTaskStatus.SUCCEEDED
    # handler 期间心跳把 lease_until 续到 now+lease_seconds，必大于 claim 时的初值；
    # 若 Worker 未调用 heartbeat_lease，lease_until 会保持不变。
    assert final.lease_until is not None
    assert final.lease_until > initial


@pytest.mark.asyncio
async def test_worker_fails_task_terminal_when_no_handler_registered(
    task_store, preserved_handlers,
) -> None:
    worker_mod.TASK_HANDLERS.clear()
    db = task_store.session
    task = await task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until=datetime.now(UTC) + timedelta(minutes=1),
        task_type="type_without_handler",
    )

    outcome = await worker_mod._process(db, task, "worker-1")

    final = await task_store.get(task.task_id)
    assert outcome == "failed"
    assert final is not None
    assert final.status is AiTaskStatus.FAILED
    assert final.error_code == "AI_FEATURE_DISABLED"
    assert final.lease_owner is None
    assert final.lease_until is None


# ----------------------------------------------------------------------
# 取消
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_cancel_moves_cancellable_task_to_cancelled(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(status="queued", owner_user_id=10)

    cancelled = await request_cancel(db, task.task_id, 10)

    assert cancelled.status is AiTaskStatus.CANCELLED
    assert cancelled.finished_at is not None


@pytest.mark.asyncio
async def test_request_cancel_rejects_terminal_task(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(status="succeeded", owner_user_id=10, result_ref="res:1")

    with pytest.raises(TaskError) as excinfo:
        await request_cancel(db, task.task_id, 10)
    assert excinfo.value.code == "TASK_NOT_CANCELLABLE"
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_request_cancel_hides_foreign_and_missing_tasks(task_store) -> None:
    db = task_store.session
    foreign = await task_store.seed(status="queued", owner_user_id=20)

    with pytest.raises(TaskError) as excinfo:
        await request_cancel(db, foreign.task_id, 10)
    assert excinfo.value.code == "TASK_NOT_FOUND"
    assert excinfo.value.status_code == 404

    with pytest.raises(TaskError) as excinfo:
        await request_cancel(db, "missing-task", 10)
    assert excinfo.value.code == "TASK_NOT_FOUND"


# ----------------------------------------------------------------------
# 完成与失败（结果写入前版本复核）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_task_writes_result_only_when_version_matches(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        source_revision_json=dict(_FULL_REVISION),
    )

    completed = await complete_task(
        db,
        task.task_id,
        "worker-1",
        "res:1",
        revisions=RevisionVector(profile=1, policy=1),
    )

    assert completed.status is AiTaskStatus.SUCCEEDED
    assert completed.result_ref is None


@pytest.mark.asyncio
async def test_terminal_task_scrubs_payload_and_resource_reference(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        payload_summary={"resource_id": "raw-resource-1", "answer": "must not remain"},
        consent_snapshot_json={"scope": "profile_text_extract"},
        source_revision_json=dict(_FULL_REVISION),
    )

    completed = await complete_task(
        db,
        task.task_id,
        "worker-1",
        "res:1",
        revisions=RevisionVector(profile=1, policy=1),
    )

    stored = task_store.tasks[task.task_id]
    assert completed.status is AiTaskStatus.SUCCEEDED
    assert stored["payload_summary"] is None
    assert stored["consent_snapshot_json"] is None
    assert stored["source_revision_json"] is None
    assert stored["result_ref"] is None


@pytest.mark.asyncio
async def test_complete_task_supersedes_when_version_changed(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        source_revision_json=dict(_FULL_REVISION),
        consent_snapshot_json={
            "scope": "profile_text_extract",
            "version": "",
        },
    )
    hook_calls = 0

    async def before_supersede() -> None:
        nonlocal hook_calls
        hook_calls += 1

    superseded = await complete_task(
        db,
        task.task_id,
        "worker-1",
        "res-old",
        revisions=RevisionVector(profile=2, policy=1),
        before_supersede=before_supersede,
    )

    assert superseded.status is AiTaskStatus.SUPERSEDED
    assert superseded.result_ref is None
    assert hook_calls == 1
    completion_sql = [sql for sql, _params in db.calls]
    assert any(
        "FROM user_revision_state" in sql and "FOR UPDATE" in sql
        for sql in completion_sql
    )
    assert any(
        "FROM ai_consent_grant" in sql and "FOR UPDATE" in sql
        for sql in completion_sql
    )


@pytest.mark.asyncio
async def test_complete_task_does_not_write_until_running(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(status="leased", lease_owner="worker-1")

    result = await complete_task(db, task.task_id, "worker-1", "res:1")

    assert result.status is AiTaskStatus.LEASED
    assert result.result_ref is None


@pytest.mark.asyncio
async def test_fail_task_retryable_moves_to_retry_wait_with_backoff(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until=now + timedelta(seconds=60),
    )

    retried = await fail_task(
        db,
        task.task_id,
        "worker-1",
        error_code="AI_TEMPORARILY_UNAVAILABLE",
        retryable=True,
    )

    assert retried.status is AiTaskStatus.RETRY_WAIT
    assert retried.attempt_count == 1
    assert retried.error_code == "AI_TEMPORARILY_UNAVAILABLE"
    assert retried.next_run_at is not None
    assert retried.next_run_at > now
    assert retried.lease_owner is None
    assert retried.lease_until is None


@pytest.mark.asyncio
async def test_fail_task_non_retryable_moves_to_failed(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(status="running", lease_owner="worker-1")

    failed = await fail_task(
        db,
        task.task_id,
        "worker-1",
        error_code="AI_INPUT_INVALID",
        retryable=False,
    )

    assert failed.status is AiTaskStatus.FAILED
    assert failed.error_code == "AI_INPUT_INVALID"
    assert failed.error_message == "provider 输出未通过 Schema 校验"


@pytest.mark.asyncio
async def test_fail_task_exhausts_attempts_into_failed(task_store) -> None:
    db = task_store.session
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        attempt_count=3,
        max_attempts=3,
    )

    exhausted = await fail_task(
        db,
        task.task_id,
        "worker-1",
        error_code="AI_TEMPORARILY_UNAVAILABLE",
        retryable=True,
    )

    assert exhausted.status is AiTaskStatus.FAILED
    assert exhausted.attempt_count == 3


@pytest.mark.asyncio
async def test_reap_does_not_reclaim_a_live_lease(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    await task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until=now + timedelta(seconds=60),
    )

    recovered = await reap_expired_leases(db, now, limit=10)

    assert recovered == []


@pytest.mark.asyncio
async def test_reap_increments_attempt_count_on_recovery(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until=_utc(2026, 8, 7, 7, 59),
        attempt_count=1,
        max_attempts=3,
    )

    recovered = await reap_expired_leases(db, now, limit=10)

    assert recovered == [task.task_id]
    final = await task_store.get(task.task_id)
    assert final is not None
    assert final.status is AiTaskStatus.RETRY_WAIT
    assert final.attempt_count == 2
    assert final.lease_owner is None
    assert final.lease_until is None


@pytest.mark.asyncio
async def test_reap_fails_running_task_when_attempts_exhausted(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    task = await task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until=_utc(2026, 8, 7, 7, 59),
        attempt_count=3,
        max_attempts=3,
    )

    recovered = await reap_expired_leases(db, now, limit=10)

    assert recovered == [task.task_id]
    final = await task_store.get(task.task_id)
    assert final is not None
    assert final.status is AiTaskStatus.FAILED
    assert final.error_code == "AI_TEMPORARILY_UNAVAILABLE"
    assert final.lease_owner is None
    assert final.lease_until is None


@pytest.mark.asyncio
async def test_reap_caps_exhausted_leased_task_in_retry_wait(task_store) -> None:
    db = task_store.session
    now = _utc(2026, 8, 7, 8, 0)
    task = await task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until=_utc(2026, 8, 7, 7, 59),
        attempt_count=3,
        max_attempts=3,
    )

    recovered = await reap_expired_leases(db, now, limit=10)

    assert recovered == [task.task_id]
    final = await task_store.get(task.task_id)
    assert final is not None
    # 缺陷 34 修复：leased 任务在 attempt_count >= max_attempts 时不再回退
    # retry_wait（会与 leased 形成死循环），reaper 走特殊路径直接终态 failed
    # 并告警 ai_task_reap_leased_at_attempts_cap。
    assert final.status is AiTaskStatus.FAILED
    assert final.attempt_count == 3
    assert final.lease_owner is None
    assert final.lease_until is None


# ----------------------------------------------------------------------
# 通用任务 API
# ----------------------------------------------------------------------

def _override_auth(task_store: TaskStore, owner_id: int = 10) -> None:
    async def fake_current_user() -> CurrentUser:
        return CurrentUser(
            id=owner_id,
            session_id=9,
            phone="13800000000",
            status=1,
            realname_status=2,
        )

    def fake_db():
        yield task_store.session

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_db] = fake_db


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def test_get_task_api_returns_poll_state_shape() -> None:
    store = TaskStore()
    row = {
        "task_id": "at-api-1",
        "owner_user_id": 10,
        "task_type": "profile_extract",
        "scene": "profile_text_extract",
        "idempotency_key": "k",
        "request_digest": "d",
        "status": "queued",
        "stage": None,
        "attempt_count": 0,
        "max_attempts": 3,
        "next_run_at": None,
        "lease_owner": None,
        "lease_until": None,
        "consent_snapshot_json": None,
        "source_revision_json": None,
        "payload_summary": None,
        "error_code": None,
        "error_message": None,
        "result_ref": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "started_at": None,
        "finished_at": None,
    }
    store.tasks["at-api-1"] = {**row, "id": 1}
    _override_auth(store)
    try:
        response = client.get("/api/v1/ai/tasks/at-api-1")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "at-api-1"
    assert body["status"] == "queued"
    assert body["stage"] is None
    assert body["poll_after_ms"] >= 0
    assert "expires_at" in body
    assert body["result_ref"] is None


def test_get_task_api_returns_safe_result_ref_when_succeeded() -> None:
    store = TaskStore()
    row = {
        "task_id": "at-api-2",
        "owner_user_id": 10,
        "task_type": "profile_extract",
        "scene": "profile_text_extract",
        "idempotency_key": "k",
        "request_digest": "d",
        "status": "succeeded",
        "stage": "completed",
        "attempt_count": 1,
        "max_attempts": 3,
        "next_run_at": None,
        "lease_owner": None,
        "lease_until": None,
        "consent_snapshot_json": None,
        "source_revision_json": None,
        "payload_summary": None,
        "error_code": None,
        "error_message": None,
        "result_ref": "res:profile-extract-1",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "started_at": None,
        "finished_at": datetime.now(UTC),
    }
    store.tasks["at-api-2"] = {**row, "id": 2}
    _override_auth(store)
    try:
        response = client.get("/api/v1/ai/tasks/at-api-2")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["result_ref"] == "res:profile-extract-1"


def test_get_task_api_hides_foreign_and_missing_tasks() -> None:
    store = TaskStore()
    row = {
        "task_id": "at-foreign",
        "owner_user_id": 20,
        "task_type": "profile_extract",
        "scene": "profile_text_extract",
        "idempotency_key": "k",
        "request_digest": "d",
        "status": "queued",
        "stage": None,
        "attempt_count": 0,
        "max_attempts": 3,
        "next_run_at": None,
        "lease_owner": None,
        "lease_until": None,
        "consent_snapshot_json": None,
        "source_revision_json": None,
        "payload_summary": None,
        "error_code": None,
        "error_message": None,
        "result_ref": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "started_at": None,
        "finished_at": None,
    }
    store.tasks["at-foreign"] = {**row, "id": 3}
    _override_auth(store, owner_id=10)
    try:
        response = client.get("/api/v1/ai/tasks/at-foreign")
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "TASK_NOT_FOUND"

        missing = client.get("/api/v1/ai/tasks/at-missing")
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "TASK_NOT_FOUND"
    finally:
        _clear_overrides()


def test_cancel_task_api_returns_202_cancel_requested() -> None:
    store = TaskStore()
    row = {
        "task_id": "at-cancel-1",
        "owner_user_id": 10,
        "task_type": "profile_extract",
        "scene": "profile_text_extract",
        "idempotency_key": "k",
        "request_digest": "d",
        "status": "queued",
        "stage": None,
        "attempt_count": 0,
        "max_attempts": 3,
        "next_run_at": None,
        "lease_owner": None,
        "lease_until": None,
        "consent_snapshot_json": None,
        "source_revision_json": None,
        "payload_summary": None,
        "error_code": None,
        "error_message": None,
        "result_ref": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "started_at": None,
        "finished_at": None,
    }
    store.tasks["at-cancel-1"] = {**row, "id": 4}
    _override_auth(store)
    try:
        response = client.post("/api/v1/ai/tasks/at-cancel-1/cancel")
    finally:
        _clear_overrides()

    assert response.status_code == 202
    body = response.json()
    assert body["task_id"] == "at-cancel-1"
    assert body["status"] == "cancelled"
    assert body["cancel_requested"] is True


def test_cancel_task_api_returns_409_for_terminal_task() -> None:
    store = TaskStore()
    row = {
        "task_id": "at-cancel-2",
        "owner_user_id": 10,
        "task_type": "profile_extract",
        "scene": "profile_text_extract",
        "idempotency_key": "k",
        "request_digest": "d",
        "status": "failed",
        "stage": None,
        "attempt_count": 1,
        "max_attempts": 3,
        "next_run_at": None,
        "lease_owner": None,
        "lease_until": None,
        "consent_snapshot_json": None,
        "source_revision_json": None,
        "payload_summary": None,
        "error_code": "AI_INPUT_INVALID",
        "error_message": "provider 输出未通过 Schema 校验",
        "result_ref": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "started_at": None,
        "finished_at": datetime.now(UTC),
    }
    store.tasks["at-cancel-2"] = {**row, "id": 5}
    _override_auth(store)
    try:
        response = client.post("/api/v1/ai/tasks/at-cancel-2/cancel")
    finally:
        _clear_overrides()

    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["code"] == "TASK_NOT_CANCELLABLE"
    assert body["detail"]["request_id"]
    assert body["detail"]["retryable"] is False


# ----------------------------------------------------------------------
# Worker dry-run
# ----------------------------------------------------------------------

def test_worker_dry_run_is_safe_and_prints_zero_counts(capsys: pytest.CaptureFixture) -> None:
    code = worker_main(["--once", "--dry-run"])

    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == "claimed=0 completed=0 failed=0"


# ----------------------------------------------------------------------
# WP-S1 / F9：搜索任务百分比进度
# ----------------------------------------------------------------------

def test_task_record_from_row_maps_progress_percent():
    """进度字段（方案 WP-S1）：from_row 必须透传 progress_percent。"""
    from app.services.ai.tasks import AiTaskRecord

    row = {
        "id": 1, "task_id": "t1", "owner_user_id": 1, "task_type": "search_execute",
        "scene": "ai", "idempotency_key": "k1", "request_digest": None,
        "status": "running", "stage": "filtering", "progress_percent": 30,
        "attempt_count": 0, "max_attempts": 3, "next_run_at": None,
        "lease_owner": None, "lease_until": None, "consent_snapshot_json": None,
        "source_revision_json": None, "payload_summary": None,
        "error_code": None, "error_message": None, "result_ref": None,
        "created_at": None, "updated_at": None, "started_at": None,
        "finished_at": None,
    }
    record = AiTaskRecord.from_row(row)
    assert record.progress_percent == 30
