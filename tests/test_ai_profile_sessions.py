"""Task 7 acceptance contract: M04 text sessions, turns and confirmed-safe extraction.

The three Step 1 tests are mirrored verbatim from the task brief.  ``profile_store``
is an in-memory fake store: a ``FakeProfileSession`` routes the service SQL by
substring onto in-memory sessions/turns/drafts/consents/revision state plus the
Task 6 task machine, so session ownership, idempotent turn submission, suggested
drafts and worker extraction can be exercised without a real database.  The API
tests override ``get_current_user``/``get_db`` and drive the registered routes
through the TestClient.
"""

from __future__ import annotations

import json
import uuid
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import app.services.ai.profile as profile_mod
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.schemas.ai_profile import (
    ProfileDraftRead,
    ProfileDraftFieldRead,
    ProfileSubject,
)
from app.services.ai.base import ExtractedField, StructuredExtractResult
from app.services.ai.gateway import InvokeOutcome
from app.services.ai.tasks import AiTaskRecord, TaskError
from app.services.ai.profile import (
    AIInputError,
    ProfileSessionNotFound,
    ProfileSessionStale,
    create_profile_session,
    extract_profile_turn,
    next_profile_question,
    normalize_profile_answer,
    progress_value,
    submit_profile_turn,
)
from app.services.ai.profile import (
    AI_FIELD_ALLOWLIST,
    ProfileSession,
    ProfileSessionStatus,
)
from app.workers import ai_worker as worker_mod

client = TestClient(app)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return value


# ----------------------------------------------------------------------
# 内存结果辅助
# ----------------------------------------------------------------------


class _MappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_MappingResult":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalar(self) -> Any:
        if not self._rows:
            return None
        return next(iter(self._rows[0].values()))


class _WriteResult:
    def __init__(self, *, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class TaskStore:
    """Minimal in-memory ai_task fact store (mirrors Task 6 contract)."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self._next_id = 1

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

    def insert(self, params: dict[str, Any]) -> bool:
        existing = self.find_by_idempotency(
            int(params["owner_user_id"]),
            str(params["task_type"]),
            str(params["idempotency_key"]),
        )
        if existing is not None:
            raise IntegrityError(
                "INSERT INTO ai_task", params, Exception("Duplicate entry")
            )
        now = _now()
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
        elif "SET payload_summary" in sql:
            row["payload_summary"] = params.get("payload_summary")
        else:
            raise AssertionError(f"unhandled task update: {sql}")
        row["updated_at"] = _now()
        return True

    async def seed(self, **kwargs: Any) -> AiTaskRecord:
        task_id = kwargs.pop("task_id", None) or uuid.uuid4().hex
        now = _now()
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

    async def get(self, task_id: str) -> dict[str, Any] | None:
        return self.tasks.get(task_id)


class FakeProfileSession:
    """Routes service SQL by substring onto one ProfileStore.

    ``commit()`` 记录当前内存态快照作为已提交基线；``rollback()`` 还原到最近
    一次 commit 的快照，撤销快照之后的所有「插入/更新」副作用——与真实 DB 的
    「未提交写入在回滚时撤销」语义一致（此前 rollback 只计数不还原，掩盖了
    stale 标记不落库的缺陷）。尚无任何 commit 时（例如并发竞态测试），rollback
    为无操作：共享 session 无法区分各「请求」的写入归属，且败方失败语句本就没
    有产生副作用，还原基线反而会误删赢家的数据。
    """

    def __init__(self, store: "ProfileStore") -> None:
        self._store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0
        self._committed_snapshot: dict[str, Any] | None = None

    def _snapshot_store(self) -> dict[str, Any]:
        return {
            "sessions": {sid: dict(row) for sid, row in self._store.sessions.items()},
            "turns": [dict(row) for row in self._store.turns],
            "drafts": [dict(row) for row in self._store.drafts],
            "draft_fields": [dict(row) for row in self._store.draft_fields],
            "consents": [dict(row) for row in self._store.consents],
            "revision_rows": {
                uid: dict(row) for uid, row in self._store.revision_rows.items()
            },
            "tasks": {
                tid: dict(row) for tid, row in self._store.task_store.tasks.items()
            },
            "task_next_id": self._store.task_store._next_id,
        }

    def _restore_store(self, snapshot: dict[str, Any]) -> None:
        self._store.sessions = {sid: dict(row) for sid, row in snapshot["sessions"].items()}
        self._store.turns = [dict(row) for row in snapshot["turns"]]
        self._store.drafts = [dict(row) for row in snapshot["drafts"]]
        self._store.draft_fields = [dict(row) for row in snapshot["draft_fields"]]
        self._store.consents = [dict(row) for row in snapshot["consents"]]
        self._store.revision_rows = {
            uid: dict(row) for uid, row in snapshot["revision_rows"].items()
        }
        self._store.task_store.tasks = {
            tid: dict(row) for tid, row in snapshot["tasks"].items()
        }
        self._store.task_store._next_id = snapshot["task_next_id"]

    async def flush(self) -> None:
        self.flushes += 1

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _MappingResult | _WriteResult:
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        # ---- ai_task (Task 6 contract, same semantics) ----
        if "INSERT INTO ai_task" in sql:
            return _WriteResult(rowcount=1 if self._store.task_store.insert(values) else 0)
        if "UPDATE ai_task" in sql and "payload_summary" in sql:
            self._store.task_store.apply_update(sql, values)
            return _WriteResult(rowcount=1)
        if "FROM ai_task" in sql and "status IN ('queued', 'retry_wait')" in sql:
            eligible = [
                row
                for row in self._store.task_store.tasks.values()
                if row["status"] in ("queued", "retry_wait")
                and (row["next_run_at"] is None or row["next_run_at"] <= values["now"])
                and (
                    row["lease_owner"] is None
                    or row["lease_until"] is None
                    or row["lease_until"] < values["now"]
                )
            ]
            eligible.sort(key=lambda row: row["created_at"])
            return _MappingResult(eligible[: int(values["limit"])])
        if "FROM ai_task" in sql and "status IN ('leased', 'running')" in sql:
            eligible = [
                row
                for row in self._store.task_store.tasks.values()
                if row["status"] in ("leased", "running")
                and row["lease_until"] is not None
                and row["lease_until"] < values["now"]
            ]
            eligible.sort(key=lambda row: row["lease_until"])
            return _MappingResult(eligible[: int(values["limit"])])
        if "FROM ai_task" in sql and "WHERE task_id = :task_id" in sql:
            row = self._store.task_store.tasks.get(values["task_id"])
            return _MappingResult([row] if row else [])
        if "FROM ai_task" in sql and "owner_user_id = :owner_user_id" in sql:
            row = self._store.task_store.find_by_idempotency(
                int(values["owner_user_id"]),
                str(values["task_type"]),
                str(values["idempotency_key"]),
            )
            return _MappingResult([row] if row else [])
        if sql.startswith("UPDATE ai_task"):
            applied = self._store.task_store.apply_update(sql, values)
            return _WriteResult(rowcount=1 if applied else 0)
        # ---- profile tables ----
        if "INSERT INTO ai_profile_session" in sql:
            self._store.insert_session(values)
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_profile_turn" in sql:
            self._store.insert_turn(values)
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_profile_draft_field" in sql:
            self._store.insert_draft_field(values)
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_profile_draft" in sql:
            self._store.insert_draft(values)
            return _WriteResult(rowcount=1)
        if "UPDATE ai_profile_session" in sql:
            self._store.apply_session_update(sql, values)
            return _WriteResult(rowcount=1)
        if "FROM ai_profile_turn" in sql and "COUNT(*)" in sql:
            return _MappingResult(
                [{"COUNT(*)": self._store.count_turns(values["session_id"])}]
            )
        if "FROM ai_profile_turn" in sql:
            row = self._store.find_turn(values["session_id"], values["client_turn_id"])
            return _MappingResult([row] if row else [])
        if "FROM ai_consent_grant" in sql:
            row = self._store.find_consent(
                int(values["user_id"]), str(values["scope"]), str(values["version"])
            )
            return _MappingResult([row] if row else [])
        if "FROM user_revision_state" in sql:
            row = self._store.revision_rows.get(int(values["user_id"]))
            return _MappingResult([row] if row else [])
        if "FROM ai_profile_session" in sql and "active_status = 1" in sql:
            row = self._store.find_active(int(values["user_id"]), str(values["subject"]))
            return _MappingResult([row] if row else [])
        if "FROM ai_profile_session" in sql:
            row = self._store.sessions.get(str(values["session_id"]))
            return _MappingResult([row] if row else [])
        if "FROM ai_profile_draft_field" in sql:
            return _MappingResult(self._store.field_keys(str(values["session_id"])))
        raise AssertionError(f"unhandled sql: {sql}")

    async def commit(self) -> None:
        self.commits += 1
        self._committed_snapshot = self._snapshot_store()

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self._committed_snapshot is not None:
            # 还原到最近一次 commit 的快照，撤销其后所有未提交的插入/更新。
            self._restore_store(self._committed_snapshot)


class ProfileStore:
    """In-memory profile store with the Task 7 fixture surface."""

    NotFound = ProfileSessionNotFound
    Stale = ProfileSessionStale

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.turns: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []
        self.draft_fields: list[dict[str, Any]] = []
        self.consents: list[dict[str, Any]] = []
        self.revision_rows: dict[int, dict[str, Any]] = {}
        self.task_store = TaskStore()
        self.session = FakeProfileSession(self)
        self.db = self.session
        # 预置 user 10 的 profile_text_extract 授权与初始 revision 状态，
        # 与 create_profile_session 的前置条件一致。
        self.seed_consent(10, "profile-text-v1")
        self.revision_rows.setdefault(
            10,
            {
                "profile_revision": 0,
                "preference_revision": 0,
                "privacy_revision": 0,
                "relationship_revision": 0,
                "policy_revision": 0,
            },
        )

    # ---- seed helpers ---------------------------------------------------

    def seed_consent(self, user_id: int, version: str) -> None:
        self.consents.append(
            {
                "user_id": int(user_id),
                "scope": "profile_text_extract",
                "version": version,
                "policy_revision": "ai-policy-2026-08-07-v1",
                "granted_at": _now() - timedelta(days=1),
            }
        )

    async def seed_session(
        self,
        owner_user_id: int = 10,
        subject: str = "personal",
        status: str = "draft",
        session_id: str | None = None,
        consent_version: str = "profile-text-v1",
        profile_revision: int = 1,
        preference_revision: int = 0,
        expires_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = _now()
        sid = session_id or f"ps_{uuid.uuid4().hex[:12]}"
        row = {
            "session_id": sid,
            "user_id": int(owner_user_id),
            "subject": subject,
            "input_mode": "text",
            "status": status,
            "active_status": 1,
            "consent_version": consent_version,
            "policy_revision": "ai-policy-2026-08-07-v1",
            "current_question_id": None,
            "profile_revision": int(profile_revision),
            "preference_revision": int(preference_revision),
            "expires_at": expires_at or now + timedelta(days=7),
            "ended_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.sessions[sid] = row
        self.revision_rows[int(owner_user_id)] = {
            "profile_revision": int(profile_revision),
            "preference_revision": int(preference_revision),
            "privacy_revision": 0,
            "relationship_revision": 0,
            "policy_revision": 0,
        }
        if not self.find_consent(int(owner_user_id), "profile_text_extract", consent_version):
            self.seed_consent(int(owner_user_id), consent_version)
        return row

    async def seed_turn(
        self,
        session_id: str,
        client_turn_id: str,
        answer_text: str,
        user_id: int = 10,
    ) -> dict[str, Any]:
        row = {
            "turn_id": uuid.uuid4().hex,
            "session_id": session_id,
            "client_turn_id": client_turn_id,
            "user_id": int(user_id),
            "turn_no": self.count_turns(session_id) + 1,
            "role": "user",
            "answer_text": answer_text,
            "status": "saved",
            "source_type": "user_answer",
            "created_at": _now(),
        }
        self.turns.append(row)
        return row

    # ---- internal mutations ---------------------------------------------

    def insert_session(self, params: dict[str, Any]) -> dict[str, Any]:
        # 模拟 uk_ai_profile_session_active(user_id, subject, active_status)
        # 唯一约束：同 user+subject 只允许一个活动会话。
        existing_active = self.find_active(
            int(params["user_id"]), str(params["subject"])
        )
        if existing_active is not None:
            raise IntegrityError(
                "INSERT INTO ai_profile_session", params, Exception("Duplicate entry")
            )
        now = _now()
        row = {
            "session_id": str(params["session_id"]),
            "user_id": int(params["user_id"]),
            "subject": str(params["subject"]),
            "input_mode": "text",
            "status": "draft",
            "active_status": 1,
            "consent_version": str(params["consent_version"]),
            "policy_revision": str(params["policy_revision"]),
            "current_question_id": None,
            "profile_revision": int(params["profile_revision"]),
            "preference_revision": int(params["preference_revision"]),
            "expires_at": params.get("expires_at"),
            "ended_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.sessions[row["session_id"]] = row
        self.revision_rows.setdefault(
            int(params["user_id"]),
            {
                "profile_revision": int(params["profile_revision"]),
                "preference_revision": int(params["preference_revision"]),
                "privacy_revision": 0,
                "relationship_revision": 0,
                "policy_revision": 0,
            },
        )
        return row

    def insert_turn(self, params: dict[str, Any]) -> dict[str, Any]:
        # 模拟 uk_ai_profile_turn_session_client(session_id, client_turn_id)
        # 唯一约束：同会话同 client_turn_id 只允许一条 turn。
        if self.find_turn(
            str(params["session_id"]), str(params["client_turn_id"])
        ) is not None:
            raise IntegrityError(
                "INSERT INTO ai_profile_turn", params, Exception("Duplicate entry")
            )
        row = {
            "turn_id": str(params["turn_id"]),
            "session_id": str(params["session_id"]),
            "client_turn_id": str(params["client_turn_id"]),
            "user_id": int(params["user_id"]),
            "turn_no": int(params["turn_no"]),
            "role": "user",
            "answer_text": str(params["answer_text"]),
            "status": "saved",
            "source_type": "user_answer",
            "created_at": _now(),
        }
        self.turns.append(row)
        return row

    def insert_draft(self, params: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        row = {
            "draft_id": str(params["draft_id"]),
            "user_id": int(params["user_id"]),
            "subject": str(params["subject"]),
            "session_id": str(params["session_id"]),
            "status": "draft",
            "expected_revision": 0,
            "consent_snapshot_json": params.get("consent_snapshot_json"),
            "policy_revision": str(params["policy_revision"]),
            "prompt_version": str(params["prompt_version"]),
            "schema_version": str(params["schema_version"]),
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.drafts.append(row)
        return row

    def insert_draft_field(self, params: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        row = {
            "draft_id": str(params["draft_id"]),
            "field_key": str(params["field_key"]),
            "subject": str(params["subject"]),
            "value": (
                json.loads(params["value_json"]) if params.get("value_json") else None
            ),
            "display_value": params.get("display_value"),
            "source_type": str(params.get("source_type") or "user_answer"),
            "source_turn_ids": params.get("source_turn_ids"),
            "confidence": float(params.get("confidence") or 0.0),
            "visibility": params.get("visibility"),
            "consent_scope": params.get("consent_scope"),
            "schema_version": str(params.get("schema_version") or "profile-extract-v1"),
            "prompt_version": params.get("prompt_version"),
            "content_hash": params.get("content_hash"),
            "confirmation_status": str(params.get("confirmation_status") or "suggested"),
            "created_at": now,
            "updated_at": now,
        }
        self.draft_fields.append(row)
        return row

    def apply_session_update(self, sql: str, params: dict[str, Any]) -> bool:
        row = self.sessions.get(params.get("session_id"))
        if row is None:
            return False
        if "status = 'stale'" in sql:
            row["status"] = "stale"
            row["active_status"] = 0
            row["ended_at"] = _now()
        elif "status = 'cancelled'" in sql:
            row["status"] = "cancelled"
            row["active_status"] = 0
            row["ended_at"] = _now()
        elif "SET status = :status" in sql:
            row["status"] = str(params["status"])
        else:
            raise AssertionError(f"unhandled session update: {sql}")
        row["updated_at"] = _now()
        return True

    # ---- query helpers ---------------------------------------------------

    def find_consent(self, user_id: int, scope: str, version: str) -> dict[str, Any] | None:
        for row in self.consents:
            if (
                row["user_id"] == user_id
                and row["scope"] == scope
                and row["version"] == version
            ):
                return row
        return None

    def find_active(self, user_id: int, subject: str) -> dict[str, Any] | None:
        for row in self.sessions.values():
            if row["user_id"] == user_id and row["subject"] == subject and row["active_status"] == 1:
                return row
        return None

    def find_turn(self, session_id: str, client_turn_id: str) -> dict[str, Any] | None:
        for row in self.turns:
            if row["session_id"] == session_id and row["client_turn_id"] == client_turn_id:
                return row
        return None

    def count_turns(self, session_id: str) -> int:
        return sum(1 for row in self.turns if row["session_id"] == session_id)

    def field_keys(self, session_id: str) -> list[dict[str, Any]]:
        draft_ids = {d["draft_id"] for d in self.drafts if d["session_id"] == session_id}
        return [
            {"field_key": f["field_key"], "confirmation_status": f["confirmation_status"]}
            for f in self.draft_fields
            if f["draft_id"] in draft_ids and f["confirmation_status"] != "deleted"
        ]

    def fields_for_draft(self, draft_id: str) -> list[dict[str, Any]]:
        return [dict(f) for f in self.draft_fields if f["draft_id"] == draft_id]

    # ---- Task 7 fixture surface (brief semantics) -----------------------

    async def run_mock_extraction(self, answer_text: str) -> ProfileDraftRead:
        session = await self.seed_session(owner_user_id=10, subject="personal", status="extracting")
        turn = await self.seed_turn(session["session_id"], "turn-001", answer_text)
        task = await self.task_store.seed(
            status="leased",
            lease_owner="worker-1",
            lease_until=_now() + timedelta(seconds=60),
            task_type="profile_extract",
            idempotency_key="extract-key-001",
            request_digest="digest",
            consent_snapshot_json={
                "scope": "profile_text_extract",
                "version": "profile-text-v1",
                "policy_revision": "ai-policy-2026-08-07-v1",
            },
            source_revision_json={
                "profile": 1,
                "preference": 0,
                "privacy": 0,
                "relationship": 0,
                "policy": 0,
            },
            payload_summary={
                "session_id": session["session_id"],
                "turn_id": turn["turn_id"],
                "client_turn_id": turn["client_turn_id"],
                "subject": "personal",
            },
        )
        outcome = await worker_mod._process(self.db, task, "worker-1")
        assert outcome == "completed"
        draft = await self.read_draft_for(session["session_id"])
        assert draft is not None
        return draft

    async def read_draft_for(self, session_id: str) -> ProfileDraftRead | None:
        drafts = [d for d in self.drafts if d["session_id"] == session_id]
        if not drafts:
            return None
        draft = drafts[-1]
        fields = []
        for f in self.fields_for_draft(draft["draft_id"]):
            fields.append(
                ProfileDraftFieldRead(
                    field_key=f["field_key"],
                    subject=ProfileSubject(f["subject"]),
                    value=f["value"],
                    display_value=f["display_value"],
                    confidence=f["confidence"],
                    needs_confirmation=True,
                    confirmation_status=f["confirmation_status"],
                    content_hash=f["content_hash"],
                )
            )
        return ProfileDraftRead(
            draft_id=draft["draft_id"],
            subject=ProfileSubject(draft["subject"]),
            status="draft",
            expected_revision=0,
            policy_revision=draft["policy_revision"],
            schema_version="profile-extract-v1",
            fields=fields,
            created_at=draft["created_at"],
            updated_at=draft["updated_at"],
        )

    async def published_fields(
        self, user_id: int = 10, subject: str = "personal"
    ) -> list[dict[str, Any]]:
        # Task 7 没有发布路径：任何字段都停留在 suggested，永不成为已发布字段。
        return [
            dict(f)
            for f in self.draft_fields
            if f["subject"] == subject and f["confirmation_status"] == "confirmed"
        ]

    async def count_tasks(self, turn_id: str) -> int:
        def _turn_id(payload: Any) -> Any:
            if isinstance(payload, dict):
                return payload.get("turn_id")
            if isinstance(payload, str):
                try:
                    return json.loads(payload).get("turn_id")
                except ValueError:
                    return None
            return None

        return sum(
            1
            for row in self.task_store.tasks.values()
            if row["payload_summary"] and _turn_id(row["payload_summary"]) == turn_id
        )

    async def read_session(self, session_id: str, owner_user_id: int) -> dict[str, Any]:
        row = self.sessions.get(session_id)
        if row is None or int(row["user_id"]) != int(owner_user_id):
            raise self.NotFound()
        return row

    async def get(self, session_id: str) -> dict[str, Any] | None:
        return self.sessions.get(session_id)


@pytest.fixture
def profile_store() -> ProfileStore:
    store = ProfileStore()
    prior = worker_mod.TASK_HANDLERS.get("profile_extract")
    worker_mod.TASK_HANDLERS["profile_extract"] = extract_profile_turn
    yield store
    if prior is None:
        worker_mod.TASK_HANDLERS.pop("profile_extract", None)
    else:
        worker_mod.TASK_HANDLERS["profile_extract"] = prior


# ----------------------------------------------------------------------
# Step 1: 简报逐字测试
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_client_turn_id_replays_without_a_second_task(profile_store) -> None:
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    first = await submit_profile_turn(
        profile_store.db, session.session_id, 10, "turn-001", "周末喜欢看展", "turn-key-001"
    )
    replay = await submit_profile_turn(
        profile_store.db, session.session_id, 10, "turn-001", "周末喜欢看展", "turn-key-001"
    )
    assert replay.turn_id == first.turn_id
    assert await profile_store.count_tasks(first.turn_id) == 1


@pytest.mark.asyncio
async def test_extraction_stays_suggested_until_confirmation(profile_store) -> None:
    draft = await profile_store.run_mock_extraction("我喜欢旅行和看展")
    assert draft.fields[0].confirmation_status == "suggested"
    assert await profile_store.published_fields(user_id=10, subject="personal") == []


@pytest.mark.asyncio
async def test_other_user_cannot_read_session(profile_store) -> None:
    session = await profile_store.seed_session(owner_user_id=10, subject="personal")
    with pytest.raises(profile_store.NotFound):
        await profile_store.read_session(session["session_id"], owner_user_id=11)


# ----------------------------------------------------------------------
# Step 3: 会话、回答与抽取边界
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_session_requires_consent(profile_store) -> None:
    with pytest.raises(Exception) as excinfo:
        await create_profile_session(
            profile_store.db, 99, ProfileSubject.PERSONAL, "profile-text-v1", "key-002"
        )
    assert excinfo.value.code == "AI_CONSENT_REQUIRED"


@pytest.mark.asyncio
async def test_create_session_same_user_subject_reuses_active_session(profile_store) -> None:
    first = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    second = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-999"
    )
    assert second.session_id == first.session_id
    assert len(profile_store.sessions) == 1


@pytest.mark.asyncio
async def test_normalize_answer_validation() -> None:
    assert normalize_profile_answer("  周末喜欢看展  ") == "周末喜欢看展"
    with pytest.raises(AIInputError):
        normalize_profile_answer("   ")
    with pytest.raises(AIInputError):
        normalize_profile_answer("x" * 2001)
    assert len(normalize_profile_answer("x" * 2000)) == 2000


@pytest.mark.asyncio
async def test_twenty_duplicate_turns_keep_one_turn_and_one_task(profile_store) -> None:
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    submissions = []
    for index in range(20):
        submissions.append(
            await submit_profile_turn(
                profile_store.db,
                session.session_id,
                10,
                "turn-001",
                "周末喜欢看展",
                f"turn-key-{index:03d}",
            )
        )
    assert len({s.turn_id for s in submissions}) == 1
    assert await profile_store.count_tasks(submissions[0].turn_id) == 1
    assert profile_store.count_turns(session.session_id) == 1


@pytest.mark.asyncio
async def test_submit_turn_rejects_foreign_session(profile_store) -> None:
    session = await profile_store.seed_session(owner_user_id=20, subject="personal")
    with pytest.raises(ProfileSessionNotFound):
        await submit_profile_turn(
            profile_store.db, session["session_id"], 10, "turn-001", "周末喜欢看展", "turn-key-001"
        )


@pytest.mark.asyncio
async def test_submit_turn_rejects_finished_session(profile_store) -> None:
    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="cancelled"
    )
    with pytest.raises(ProfileSessionNotFound):
        await submit_profile_turn(
            profile_store.db, session["session_id"], 10, "turn-001", "周末喜欢看展", "turn-key-001"
        )


@pytest.mark.asyncio
async def test_submit_turn_marks_session_stale_when_profile_revision_changes(profile_store) -> None:
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    await submit_profile_turn(
        profile_store.db, session.session_id, 10, "turn-001", "周末喜欢看展", "turn-key-001"
    )
    # 前两次正常请求已提交（路由成功分支），作为 rollback 的已提交基线。
    await profile_store.db.commit()
    profile_store.revision_rows[10]["profile_revision"] = 2
    with pytest.raises(ProfileSessionStale):
        await submit_profile_turn(
            profile_store.db, session.session_id, 10, "turn-002", "身高172", "turn-key-002"
        )
    # 模拟 get_db 异常路径退出回滚：_mark_stale 已自行提交，stale 标记必须保留。
    await profile_store.db.rollback()
    assert (await profile_store.get(session.session_id))["status"] == "stale"


@pytest.mark.asyncio
async def test_submit_turn_marks_session_stale_when_expired(profile_store) -> None:
    session = await profile_store.seed_session(
        owner_user_id=10,
        subject="personal",
        expires_at=_now() - timedelta(days=1),
    )
    # seed 视为已提交基线（上一个请求的事务），让 rollback 有可还原的状态。
    await profile_store.db.commit()
    with pytest.raises(ProfileSessionStale):
        await submit_profile_turn(
            profile_store.db, session["session_id"], 10, "turn-001", "周末喜欢看展", "turn-key-001"
        )
    # 模拟 get_db 异常路径退出回滚：stale 标记必须仍落库（status=stale, 非 active）。
    await profile_store.db.rollback()
    assert (await profile_store.get(session["session_id"]))["status"] == "stale"
    assert (await profile_store.get(session["session_id"]))["active_status"] == 0


@pytest.mark.asyncio
async def test_create_stale_marking_committed_and_recreate_succeeds(profile_store) -> None:
    """I-1b：过期复用路径的 stale 标记必须自行提交，且之后能重新创建会话。

    此前 ``_mark_stale`` 只执行 UPDATE 不 commit，创建路由在 409 异常路径退出
    事务时回滚，stale 永不落库 → 过期会话保持 active，同 user+subject 每次
    create 都走 ``_mark_stale``+raise、永远无法重新创建。本测试先提交 seed
    基线，再触发过期 create 的 409，随后显式 rollback 模拟 get_db 上下文退出，
    断言 stale 标记仍落库且同一 subject 的重新创建成功。
    """
    seed = await profile_store.seed_session(
        owner_user_id=10,
        subject="personal",
        expires_at=_now() - timedelta(days=1),
    )
    # seed 视为上一个已提交请求，作为 rollback 的已提交基线。
    await profile_store.db.commit()

    with pytest.raises(ProfileSessionStale):
        await create_profile_session(
            profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
        )

    # 模拟 get_db 的 async with 在异常路径退出 → session.close() 回滚未提交事务。
    await profile_store.db.rollback()

    row = await profile_store.get(seed["session_id"])
    assert row is not None
    assert row["status"] == "stale"
    assert row["active_status"] == 0
    # stale 已落库：同 user+subject 的重新创建成功，得到全新会话。
    recreated = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-002"
    )
    assert recreated.session_id != seed["session_id"]
    assert len(profile_store.sessions) == 2


@pytest.mark.asyncio
async def test_turn_text_is_saved_verbatim_before_task_enqueue(profile_store) -> None:
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    submission = await submit_profile_turn(
        profile_store.db,
        session.session_id,
        10,
        "turn-001",
        "  周末喜欢看展  ",
        "turn-key-001",
    )
    row = profile_store.find_turn(session.session_id, "turn-001")
    assert row is not None
    assert row["answer_text"] == "周末喜欢看展"
    assert submission.turn_id == row["turn_id"]
    assert profile_store.db.flushes == 1


# ----------------------------------------------------------------------
# 抽取：suggested 草稿、来源、失败只改任务状态
# ----------------------------------------------------------------------


def _stub_gateway(outcome: InvokeOutcome) -> type:
    class _StubGateway:
        def __init__(self, *, timeout_seconds: float = 30.0) -> None:
            self.timeout_seconds = timeout_seconds

        async def structured_extract(self, context: Any, request: Any) -> InvokeOutcome:
            return outcome

    return _StubGateway


async def _run_worker(
    profile_store: ProfileStore,
    monkeypatch: pytest.MonkeyPatch,
    gateway_type: type,
) -> dict[str, Any]:
    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="extracting"
    )
    turn = await profile_store.seed_turn(session["session_id"], "turn-001", "我喜欢旅行和看展")
    task = await profile_store.task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until=_now() + timedelta(seconds=60),
        task_type="profile_extract",
        idempotency_key="extract-key-001",
        request_digest="digest",
        consent_snapshot_json={
            "scope": "profile_text_extract",
            "version": "profile-text-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
        },
        source_revision_json={
            "profile": 1,
            "preference": 0,
            "privacy": 0,
            "relationship": 0,
            "policy": 0,
        },
        payload_summary={
            "session_id": session["session_id"],
            "turn_id": turn["turn_id"],
            "client_turn_id": turn["client_turn_id"],
            "subject": "personal",
        },
    )
    monkeypatch.setattr(profile_mod, "AIGateway", gateway_type)
    outcome = await worker_mod._process(profile_store.db, task, "worker-1")
    final = await profile_store.task_store.get(task.task_id)
    assert final is not None
    return {"outcome": outcome, "task": final, "session": session["session_id"]}


@pytest.mark.asyncio
async def test_schema_invalid_only_changes_task_status(profile_store, monkeypatch) -> None:
    result = await _run_worker(
        profile_store,
        monkeypatch,
        _stub_gateway(
            InvokeOutcome(
                error_code="AI_INPUT_INVALID",
                error_message="provider 输出未通过 Schema 校验",
                retryable=False,
            )
        ),
    )
    assert result["task"]["status"] == "failed"
    assert result["task"]["error_code"] == "AI_INPUT_INVALID"
    assert profile_store.drafts == []


@pytest.mark.asyncio
async def test_timeout_only_changes_task_status_to_retry_wait(profile_store, monkeypatch) -> None:
    result = await _run_worker(
        profile_store,
        monkeypatch,
        _stub_gateway(
            InvokeOutcome(
                error_code="AI_TEMPORARILY_UNAVAILABLE",
                error_message="AI 服务暂时不可用",
                retryable=True,
                retry_after_ms=2000,
            )
        ),
    )
    assert result["task"]["status"] == "retry_wait"
    assert result["task"]["error_code"] == "AI_TEMPORARILY_UNAVAILABLE"
    assert profile_store.drafts == []


@pytest.mark.asyncio
async def test_extraction_rejects_authentication_fields_not_in_allowlist(
    profile_store, monkeypatch
) -> None:
    outcome = InvokeOutcome(
        result=StructuredExtractResult(
            schema_version="profile-extract-v1",
            fields=(
                ExtractedField(
                    field_key="realname_status",
                    subject="personal",
                    value=2,
                    source_quote="已实名",
                    confidence=0.99,
                    confirmation_status="suggested",
                ),
                ExtractedField(
                    field_key="interest_tags",
                    subject="personal",
                    value=["旅行"],
                    source_quote="我喜欢旅行",
                    confidence=0.91,
                    confirmation_status="suggested",
                ),
            ),
        )
    )
    result = await _run_worker(profile_store, monkeypatch, _stub_gateway(outcome))
    assert result["task"]["status"] == "succeeded"
    field_keys = [f["field_key"] for f in profile_store.draft_fields]
    assert "realname_status" not in field_keys
    assert "interest_tags" in field_keys
    assert all(f["confirmation_status"] == "suggested" for f in profile_store.draft_fields)


@pytest.mark.asyncio
async def test_extraction_writes_full_source_evidence(profile_store) -> None:
    draft = await profile_store.run_mock_extraction("周末喜欢旅行和看展")
    first = draft.fields[0]
    assert first.confirmation_status == "suggested"
    stored = profile_store.fields_for_draft(draft.draft_id)
    assert len(stored) >= 1
    for field in stored:
        assert field["source_turn_ids"] is not None
        assert field["schema_version"] == "profile-extract-v1"
        assert field["prompt_version"] == "profile-extract-prompt-v1"
        assert field["content_hash"]
        assert field["confirmation_status"] == "suggested"


@pytest.mark.asyncio
async def test_extraction_advances_session_to_awaiting_confirmation(profile_store) -> None:
    await profile_store.run_mock_extraction("周末喜欢旅行和看展")
    session_row = next(iter(profile_store.sessions.values()))
    assert session_row["status"] == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_worker_registers_profile_extract_handler() -> None:
    assert worker_mod.TASK_HANDLERS.get("profile_extract") is extract_profile_turn


# ----------------------------------------------------------------------
# 下一问与进度
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_question_is_computed_from_missing_fields(profile_store) -> None:
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    assert session.current_question is not None
    assert session.current_question.id == "interest_lifestyle_v1"
    assert session.current_question.text == "最近让你投入的事情是什么？"


def test_next_question_never_repeats_confirmed_fields() -> None:
    session = ProfileSession(
        session_id="ps_x",
        owner_user_id=10,
        subject=ProfileSubject.PERSONAL,
        status=ProfileSessionStatus.DRAFT,
        input_mode="text",
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
        current_question=None,
        revision_vector=None,  # type: ignore[arg-type]
        consent_snapshot={},
        field_keys=frozenset({"interest_tags", "city_code"}),
        confirmed_keys=frozenset(),
        profile_revision=0,
        preference_revision=0,
        expires_at=None,
        created_at=None,
        updated_at=None,
    )
    question = next_profile_question(session)
    assert question is not None
    assert question.id not in {"interest_lifestyle_v1", "city_residence_v1"}


def test_progress_value_is_real_confirmed_field_coverage() -> None:
    assert progress_value(frozenset()) == 0.0
    assert progress_value(frozenset({"interest_tags"})) == 1.0 / len(AI_FIELD_ALLOWLIST)
    assert progress_value(frozenset(AI_FIELD_ALLOWLIST)) == 1.0


# ----------------------------------------------------------------------
# 状态流转
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_resume_cycle(profile_store) -> None:
    from app.services.ai.profile import (
        pause_profile_session,
        resume_profile_session,
    )

    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    paused = await pause_profile_session(profile_store.db, session.session_id, 10)
    assert paused.status is ProfileSessionStatus.PAUSED
    resumed = await resume_profile_session(profile_store.db, session.session_id, 10)
    assert resumed.status is ProfileSessionStatus.DRAFT


@pytest.mark.asyncio
async def test_pause_illegal_state_returns_not_found(profile_store) -> None:
    from app.services.ai.profile import pause_profile_session

    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="cancelled"
    )
    with pytest.raises(ProfileSessionNotFound):
        await pause_profile_session(profile_store.db, session["session_id"], 10)


@pytest.mark.asyncio
async def test_resume_stale_returns_stale(profile_store) -> None:
    from app.services.ai.profile import resume_profile_session

    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="paused"
    )
    session["status"] = "stale"
    session["active_status"] = 0
    with pytest.raises(ProfileSessionStale):
        await resume_profile_session(profile_store.db, session["session_id"], 10)


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_creates_cleanup_task(profile_store) -> None:
    from app.services.ai.profile import delete_profile_session

    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    submission = await delete_profile_session(
        profile_store.db, session.session_id, 10, "delete-key-001"
    )
    assert submission.task_id
    assert submission.status.value == "queued"
    row = await profile_store.get(session.session_id)
    assert row is not None
    assert row["active_status"] == 0
    assert row["status"] == "cancelled"
    second = await delete_profile_session(
        profile_store.db, session.session_id, 10, "delete-key-001"
    )
    assert second.task_id == submission.task_id


# ----------------------------------------------------------------------
# API（OpenAPI / 归属 / 错误形状）
# ----------------------------------------------------------------------

_DEFAULT_REVISION = {
    "profile": 1,
    "preference": 0,
    "privacy": 0,
    "relationship": 0,
    "policy": 0,
}


def _seed_api_session(store: ProfileStore, owner_id: int = 10, status: str = "draft") -> dict[str, Any]:
    return asyncio.run(
        store.seed_session(owner_user_id=owner_id, subject="personal", status=status)
    )


def _override_auth(store: ProfileStore, owner_id: int = 10) -> None:
    async def fake_current_user() -> CurrentUser:
        return CurrentUser(
            id=owner_id,
            session_id=9,
            phone="13800000000",
            status=1,
            realname_status=2,
        )

    def fake_db():
        yield store.db

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_db] = fake_db


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _enable_profile_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_master_enabled", True)
    monkeypatch.setattr(settings, "ai_profile_enabled", True)


def test_openapi_includes_profile_sessions_path() -> None:
    paths = app.openapi()["paths"]
    assert "/api/v1/ai/profile-sessions" in paths
    assert "/api/v1/ai/profile-sessions/{session_id}/turns" in paths


def test_create_session_api_returns_201(monkeypatch, profile_store) -> None:
    _enable_profile_feature(monkeypatch)
    _override_auth(profile_store)
    try:
        response = client.post(
            "/api/v1/ai/profile-sessions",
            headers={"Idempotency-Key": "session-key-001"},
            json={"subject": "personal", "consent_version": "profile-text-v1"},
        )
    finally:
        _clear_overrides()
    assert response.status_code == 201
    body = response.json()
    assert body["subject"] == "personal"
    assert body["status"] == "draft"
    assert body["progress"]["basis"] == "confirmed_field_coverage"
    assert body["progress"]["value"] == 0.0
    assert body["current_question"]["id"] == "interest_lifestyle_v1"
    assert body["current_question"]["text"] == "最近让你投入的事情是什么？"


def test_create_session_api_requires_idempotency_key(monkeypatch, profile_store) -> None:
    _enable_profile_feature(monkeypatch)
    _override_auth(profile_store)
    try:
        response = client.post(
            "/api/v1/ai/profile-sessions",
            json={"subject": "personal", "consent_version": "profile-text-v1"},
        )
    finally:
        _clear_overrides()
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "AI_INPUT_INVALID"


def test_create_session_api_expired_active_session_returns_409(monkeypatch, profile_store) -> None:
    """I-1a：创建路由对「过期但仍 active」的复用会话返回 409 而非 500。

    过期会话走 ``_reuse_active_session`` 的 ``_mark_stale``+raise 路径，路由层
    必须捕获 ``ProfileSessionStale`` 映射为 409 PROFILE_SESSION_STALE；stale
    标记已提交落库后，同一 subject 的再次创建成功并得到全新会话。
    """
    _enable_profile_feature(monkeypatch)
    session = asyncio.run(
        profile_store.seed_session(
            owner_user_id=10,
            subject="personal",
            expires_at=_now() - timedelta(days=1),
        )
    )
    _override_auth(profile_store)
    try:
        first = client.post(
            "/api/v1/ai/profile-sessions",
            headers={"Idempotency-Key": "session-key-002"},
            json={"subject": "personal", "consent_version": "profile-text-v1"},
        )
        assert first.status_code == 409
        body = first.json()
        assert body["detail"]["code"] == "PROFILE_SESSION_STALE"
        assert body["detail"]["request_id"]
        second = client.post(
            "/api/v1/ai/profile-sessions",
            headers={"Idempotency-Key": "session-key-003"},
            json={"subject": "personal", "consent_version": "profile-text-v1"},
        )
    finally:
        _clear_overrides()
    assert second.status_code == 201
    assert second.json()["session_id"] != session["session_id"]
    assert second.json()["status"] == "draft"


def test_get_session_api_returns_200_only_for_owner(monkeypatch, profile_store) -> None:
    _enable_profile_feature(monkeypatch)
    session = _seed_api_session(profile_store)
    _override_auth(profile_store, owner_id=10)
    try:
        response = client.get(f"/api/v1/ai/profile-sessions/{session['session_id']}")
    finally:
        _clear_overrides()
    assert response.status_code == 200
    assert response.json()["session_id"] == session["session_id"]

    _override_auth(profile_store, owner_id=11)
    try:
        foreign = client.get(f"/api/v1/ai/profile-sessions/{session['session_id']}")
    finally:
        _clear_overrides()
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["code"] == "PROFILE_SESSION_NOT_FOUND"


def test_submit_turn_api_returns_202_with_task(monkeypatch, profile_store) -> None:
    _enable_profile_feature(monkeypatch)
    session = _seed_api_session(profile_store)
    _override_auth(profile_store)
    try:
        response = client.post(
            f"/api/v1/ai/profile-sessions/{session['session_id']}/turns",
            headers={"Idempotency-Key": "turn-key-0001"},
            json={"client_turn_id": "turn-001", "answer_text": "周末喜欢看展"},
        )
    finally:
        _clear_overrides()
    assert response.status_code == 202
    body = response.json()
    assert body["turn_id"]
    assert body["replayed"] is False
    assert body["task_id"]
    assert body["task_status"] == "queued"
    assert body["poll_after_ms"] >= 0


def test_submit_turn_api_replays_without_second_task(monkeypatch, profile_store) -> None:
    _enable_profile_feature(monkeypatch)
    session = _seed_api_session(profile_store)
    _override_auth(profile_store)
    try:
        first = client.post(
            f"/api/v1/ai/profile-sessions/{session['session_id']}/turns",
            headers={"Idempotency-Key": "turn-key-0001"},
            json={"client_turn_id": "turn-001", "answer_text": "周末喜欢看展"},
        )
        second = client.post(
            f"/api/v1/ai/profile-sessions/{session['session_id']}/turns",
            headers={"Idempotency-Key": "turn-key-0002"},
            json={"client_turn_id": "turn-001", "answer_text": "周末喜欢看展"},
        )
    finally:
        _clear_overrides()
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["turn_id"] == first.json()["turn_id"]
    assert second.json()["replayed"] is True
    assert second.json()["task_id"] is None
    count = asyncio.run(
        profile_store.count_tasks(first.json()["turn_id"])
    )
    assert count == 1


def test_pause_resume_delete_api(monkeypatch, profile_store) -> None:
    _enable_profile_feature(monkeypatch)
    session = _seed_api_session(profile_store)
    _override_auth(profile_store)
    try:
        paused = client.post(
            f"/api/v1/ai/profile-sessions/{session['session_id']}/pause",
            headers={"Idempotency-Key": "pause-key-001"},
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"

        resumed = client.post(
            f"/api/v1/ai/profile-sessions/{session['session_id']}/resume",
            headers={"Idempotency-Key": "resume-key-01"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "draft"

        deleted = client.delete(
            f"/api/v1/ai/profile-sessions/{session['session_id']}",
            headers={"Idempotency-Key": "delete-key-01"},
        )
        assert deleted.status_code == 202
        body = deleted.json()
        assert body["task_id"]
        assert body["cleanup_requested"] is True
    finally:
        _clear_overrides()


# ----------------------------------------------------------------------
# 审查补齐：未登录 / 错误 subject / ideal_partner 主体隔离
# ----------------------------------------------------------------------


def test_unauthenticated_request_returns_401(monkeypatch, profile_store) -> None:
    """未登录访问 session 路由 → 401，不泄露任何资源存在性（简报 Step 4）。"""
    _enable_profile_feature(monkeypatch)
    # 只覆盖 get_db（避免真实 DB 驱动），保留真实 get_current_user：无 token → 401。
    def fake_db():
        yield profile_store.db

    app.dependency_overrides[get_db] = fake_db
    try:
        response = client.get("/api/v1/ai/profile-sessions/ps_missing")
        assert response.status_code == 401

        response = client.post(
            "/api/v1/ai/profile-sessions",
            headers={"Idempotency-Key": "session-key-001"},
            json={"subject": "personal", "consent_version": "profile-text-v1"},
        )
        assert response.status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_create_session_api_rejects_unknown_subject(monkeypatch, profile_store) -> None:
    """错误 subject（非枚举值）在 API 层被 Pydantic 拦截为 422，不泄露资源存在性。"""
    _enable_profile_feature(monkeypatch)
    _override_auth(profile_store)
    try:
        response = client.post(
            "/api/v1/ai/profile-sessions",
            headers={"Idempotency-Key": "session-key-001"},
            json={"subject": "not_a_subject", "consent_version": "profile-text-v1"},
        )
    finally:
        _clear_overrides()
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_session_service_rejects_unknown_subject(profile_store) -> None:
    """服务层防御分支：subject 非 personal/ideal_partner → 400 AI_INPUT_INVALID。"""
    with pytest.raises(AIInputError) as excinfo:
        await create_profile_session(
            profile_store.db, 10, "not_a_subject", "profile-text-v1", "key-003"
        )
    assert excinfo.value.code == "AI_INPUT_INVALID"
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_ideal_partner_extraction_forces_subject_and_leaves_personal_untouched(
    profile_store, monkeypatch
) -> None:
    """I-3：ideal_partner 会话抽取的草稿字段 subject 强制为 ideal_partner。

    mock provider 恒返回 ``subject="personal"``；写入层必须以 session.subject
    为准覆盖，personal 事实表保持不变（没有任何 personal 标签的字段写入）。
    """
    outcome = InvokeOutcome(
        result=StructuredExtractResult(
            schema_version="profile-extract-v1",
            fields=(
                ExtractedField(
                    field_key="city_code",
                    subject="personal",  # provider 恒返回 personal
                    value="330100",
                    source_quote="住在杭州",
                    confidence=0.95,
                    confirmation_status="suggested",
                ),
                ExtractedField(
                    field_key="education_level",
                    subject="personal",
                    value=4,
                    source_quote="本科",
                    confidence=0.93,
                    confirmation_status="suggested",
                ),
            ),
        )
    )
    session = await profile_store.seed_session(
        owner_user_id=10, subject="ideal_partner", status="extracting"
    )
    turn = await profile_store.seed_turn(
        session["session_id"], "turn-001", "希望另一半住在杭州、本科学历"
    )
    task = await profile_store.task_store.seed(
        status="leased",
        lease_owner="worker-1",
        lease_until=_now() + timedelta(seconds=60),
        task_type="profile_extract",
        idempotency_key="extract-key-ip",
        request_digest="digest-ip",
        consent_snapshot_json={
            "scope": "profile_text_extract",
            "version": "profile-text-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
        },
        source_revision_json={
            "profile": 1,
            "preference": 0,
            "privacy": 0,
            "relationship": 0,
            "policy": 0,
        },
        payload_summary={
            "session_id": session["session_id"],
            "turn_id": turn["turn_id"],
            "client_turn_id": turn["client_turn_id"],
            "subject": "ideal_partner",
        },
    )
    monkeypatch.setattr(profile_mod, "AIGateway", _stub_gateway(outcome))
    result = await worker_mod._process(profile_store.db, task, "worker-1")
    assert result == "completed"

    # 草稿字段主体强制为会话的 ideal_partner，而不是 provider 返回的 personal。
    assert profile_store.draft_fields
    assert all(f["subject"] == "ideal_partner" for f in profile_store.draft_fields)
    # personal 事实表不变：没有任何 personal 标签的字段被写入。
    assert not any(f["subject"] == "personal" for f in profile_store.draft_fields)
    # 草稿行主体同样是 ideal_partner。
    assert profile_store.drafts
    assert all(d["subject"] == "ideal_partner" for d in profile_store.drafts)
    # 会话推进到 awaiting_confirmation。
    assert (await profile_store.get(session["session_id"]))["status"] == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_personal_and_ideal_partner_sessions_are_isolated(profile_store) -> None:
    """personal 与 ideal_partner 是两个互不干扰的活动会话。"""
    personal = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    ideal = await create_profile_session(
        profile_store.db, 10, ProfileSubject.IDEAL_PARTNER, "profile-text-v1", "key-002"
    )
    assert personal.session_id != ideal.session_id
    assert len(profile_store.sessions) == 2

    # 各自复用各自的会话，不会互相回放。
    personal_again = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-003"
    )
    ideal_again = await create_profile_session(
        profile_store.db, 10, ProfileSubject.IDEAL_PARTNER, "profile-text-v1", "key-004"
    )
    assert personal_again.session_id == personal.session_id
    assert ideal_again.session_id == ideal.session_id
    assert len(profile_store.sessions) == 2


# ----------------------------------------------------------------------
# 审查补齐：并发唯一键竞态
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_first_create_races_on_unique_key(profile_store, monkeypatch) -> None:
    """I-1 确定性竞态：并发首次创建同 user+subject 只产生一个 session，无 500。

    内存 fake store 的 execute 是同步的，不会自然交错；用 barrier 让两个
    调用都先通过“活动会话检查”再同时插入，强制撞
    ``uk_ai_profile_session_active`` 唯一键，验证 IntegrityError→回读回放路径。
    """
    original_find = profile_mod._find_active_session
    find_calls = 0

    async def gated_find(db: Any, user_id: int, subject: str) -> Any:
        # 前两次检查都返回“无活动会话”，让两个并发创建都走 INSERT 撞唯一键；
        # 冲突后的回读（第三次）委托原实现找到赢家会话并复用。
        nonlocal find_calls
        find_calls += 1
        if find_calls <= 2:
            return None
        return await original_find(db, user_id, subject)

    monkeypatch.setattr(profile_mod, "_find_active_session", gated_find)

    async def create() -> Any:
        return await profile_mod.create_profile_session(
            profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-race"
        )

    sessions = await asyncio.gather(create(), create())
    assert len({session.session_id for session in sessions}) == 1
    assert len(profile_store.sessions) == 1
    # 败方走了 IntegrityError→回读回放路径（而不是 500 或复用检查短路）。
    assert profile_store.db.rollbacks >= 1


@pytest.mark.asyncio
async def test_concurrent_same_client_turn_id_races_on_unique_key(
    profile_store, monkeypatch
) -> None:
    """I-2 确定性竞态：并发同 client_turn_id 只保留一条 turn 与一个 task，无 500。

    check-then-insert 的非原子窗口：两个调用都先通过 ``find_turn_by_client_id``
    检查，其中一个插入撞 ``uk_ai_profile_turn_session_client`` 唯一键，败方
    回滚后回读原 turn 回放，不创建第二个 task。
    """
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    original_find_turn = profile_mod.find_turn_by_client_id
    find_calls = 0

    async def gated_find_turn(db: Any, session_id: str, client_turn_id: str) -> Any:
        # 前两次检查都返回“无既有 turn”，让两个并发提交都走 INSERT 撞唯一键；
        # 冲突后的回读（第三次）委托原实现找到赢家 turn 并回放。
        nonlocal find_calls
        find_calls += 1
        if find_calls <= 2:
            return None
        return await original_find_turn(db, session_id, client_turn_id)

    monkeypatch.setattr(profile_mod, "find_turn_by_client_id", gated_find_turn)

    async def submit() -> Any:
        return await profile_mod.submit_profile_turn(
            profile_store.db, session.session_id, 10, "turn-001", "周末喜欢看展", "turn-key-race"
        )

    submissions = await asyncio.gather(submit(), submit())
    assert len({submission.turn_id for submission in submissions}) == 1
    assert profile_store.count_turns(session.session_id) == 1
    assert await profile_store.count_tasks(submissions[0].turn_id) == 1
    # 只有一条 turn 是赢家（accepted），败方回放同一 turn。
    assert sum(not submission.replayed for submission in submissions) == 1
    assert profile_store.db.rollbacks >= 1


# ----------------------------------------------------------------------
# 审查补齐：TASK_IDEMPOTENCY_CONFLICT 语义
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_idempotency_key_different_turn_conflicts_stably(profile_store) -> None:
    """同幂等 key 不同 payload → 稳定 409 TASK_IDEMPOTENCY_CONFLICT，不 500。"""
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-001"
    )
    first = await submit_profile_turn(
        profile_store.db, session.session_id, 10, "turn-001", "周末喜欢看展", "shared-turn-key"
    )
    with pytest.raises(TaskError) as excinfo:
        await submit_profile_turn(
            profile_store.db, session.session_id, 10, "turn-002", "身高172", "shared-turn-key"
        )
    assert excinfo.value.code == "TASK_IDEMPOTENCY_CONFLICT"
    assert excinfo.value.status_code == 409
    # 第二个 enqueue 在插入任务前就冲突上抛，任务表里仍只有第一条任务。
    assert await profile_store.count_tasks(first.turn_id) == 1


def test_submit_turn_api_same_idempotency_key_conflicts(monkeypatch, profile_store) -> None:
    """API 层同幂等 key 不同 payload → 409，detail.code=TASK_IDEMPOTENCY_CONFLICT。"""
    _enable_profile_feature(monkeypatch)
    session = _seed_api_session(profile_store)
    _override_auth(profile_store)
    try:
        first = client.post(
            f"/api/v1/ai/profile-sessions/{session['session_id']}/turns",
            headers={"Idempotency-Key": "shared-key-0001"},
            json={"client_turn_id": "turn-001", "answer_text": "周末喜欢看展"},
        )
        second = client.post(
            f"/api/v1/ai/profile-sessions/{session['session_id']}/turns",
            headers={"Idempotency-Key": "shared-key-0001"},
            json={"client_turn_id": "turn-002", "answer_text": "身高172"},
        )
    finally:
        _clear_overrides()
    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "TASK_IDEMPOTENCY_CONFLICT"
