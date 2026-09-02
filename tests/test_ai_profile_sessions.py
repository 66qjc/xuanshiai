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

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

import app.services.ai.profile as profile_mod
from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.schemas.ai_profile import (
    ProfileDraftFieldRead,
    ProfileDraftRead,
    ProfileSubject,
)
from app.services.ai.base import (
    ExtractedField,
    StructuredExtractRequest,
    StructuredExtractResult,
)
from app.services.ai.gateway import InvokeOutcome
from app.services.ai.profile import (
    _PROFILE_QUESTION_BANK,
    AI_FIELD_ALLOWLIST,
    AIInputError,
    ProfileSession,
    ProfileSessionNotFound,
    ProfileSessionStale,
    ProfileSessionStatus,
    create_master_session,
    create_profile_session,
    extract_profile_turn,
    load_owned_session,
    next_profile_question,
    normalize_profile_answer,
    progress_value,
    submit_profile_turn,
)
from app.services.ai.providers import MockAIProvider
from app.services.ai.tasks import AiTaskRecord, TaskError
from app.services.content_filter import clear_sensitive_word_cache
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

    def mappings(self) -> _MappingResult:
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
        elif "SET status = 'superseded'" in sql:
            # ``_supersede``：完成门禁（consent/版本复查）发现任务已被新状态
            # 取代时，把 running 任务移到 superseded 终态，清空租约与 payload。
            row["status"] = "superseded"
            row["finished_at"] = params.get("now")
            row["lease_owner"] = None
            row["lease_until"] = None
            row["consent_snapshot_json"] = None
            row["source_revision_json"] = None
            row["payload_summary"] = None
            row["result_ref"] = None
        elif sql.startswith("UPDATE ai_task SET lease_until"):
            if row["status"] not in ("running", "leased") or row["lease_owner"] != params.get(
                "worker_id"
            ):
                return False
            row["lease_until"] = params.get("lease_until")
        elif "SET payload_summary" in sql:
            row["payload_summary"] = params.get("payload_summary")
            if "source_revision_json" in params:
                row["source_revision_json"] = params.get("source_revision_json")
            if "consent_snapshot_json" in params:
                row["consent_snapshot_json"] = params.get("consent_snapshot_json")
        elif "SET stage = :stage" in sql:
            row["stage"] = params.get("stage")
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

    def __init__(self, store: ProfileStore) -> None:
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
            # build/master 创建把 session_kind 写成 SQL 字面量（绑定参数中无
            # session_kind），假库按真实 DB 语义从语句补齐；update 走 :session_kind
            # 绑定参数，无需补齐。
            if "'master'" in sql:
                values = {**values, "session_kind": "master"}
            elif "'build'" in sql:
                values = {**values, "session_kind": "build"}
            self._store.insert_session(values)
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_profile_turn" in sql:
            if "'assistant'" in sql:
                # ``_insert_assistant_turn`` 以 SQL 字面量写 role='assistant'
                # （绑定参数中无 role），假库按真实 DB 语义记录 assistant 行，
                # 而非落回 role 缺省的 'user'。
                values = {**values, "role": "assistant"}
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
        # ``_insert_turn`` 修复后改用 ``COALESCE(MAX(turn_no), 0)+1`` 取序号，
        # 不再走 ``COUNT(*)``。此处匹配新 SQL 并返回当前最大 turn_no + 1。
        if (
            "FROM ai_profile_turn" in sql
            and "COALESCE(MAX(turn_no)" in sql
        ):
            max_no = max(
                (int(r["turn_no"]) for r in self._store.turns if r["session_id"] == values["session_id"]),
                default=0,
            )
            return _MappingResult([{"next_no": max_no + 1}])
        if "FROM ai_profile_turn" in sql and "COUNT(*)" in sql:
            return _MappingResult(
                [{"COUNT(*)": self._store.count_turns(values["session_id"])}]
            )
        if "FROM ai_profile_turn" in sql:
            row = self._store.find_turn(values["session_id"], values["client_turn_id"])
            return _MappingResult([row] if row else [])
        # ``_revoke_consent`` 与 ``_load_latest_consent`` 的 ``FOR UPDATE`` 锁行 /
        # 按 user_id+scope 取最新 grant：这些 SQL 不带 ``version`` 参数，需与
        # ``_load_consent_grant``（按 user_id+scope+version 精确匹配）区分。
        if "FROM ai_consent_grant" in sql:
            user_id = int(values["user_id"])
            scope = str(values["scope"])
            if "version" in values:
                row = self._store.find_consent(user_id, scope, str(values["version"]))
            else:
                # 取该 user+scope 下最新一条（granted_at 最大）未撤销 grant。
                candidates = [
                    c for c in self._store.consents
                    if c["user_id"] == user_id and c["scope"] == scope
                ]
                row = candidates[-1] if candidates else None
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
        if "FROM ai_profile_draft " in sql or "FROM ai_profile_draft\n" in sql:
            session_id = str(values["session_id"])
            editable = {"draft", "extracting", "awaiting_confirmation", "paused"}
            candidates = [
                d for d in self._store.drafts
                if d["session_id"] == session_id and d["status"] in editable
            ]
            if candidates:
                candidates.sort(key=lambda d: d["updated_at"], reverse=True)
                return _MappingResult([{"draft_id": candidates[0]["draft_id"]}])
            return _MappingResult([])
        if "FROM ai_profile_draft_field" in sql and "WHERE draft_id = :draft_id" in sql:
            rows = []
            for field in self._store.fields_for_draft(str(values["draft_id"])):
                row = dict(field)
                if "value_json" not in row:
                    row["value_json"] = json.dumps(field.get("value"), ensure_ascii=False)
                rows.append(row)
            return _MappingResult(rows)
        if "FROM ai_profile_draft_field" in sql:
            return _MappingResult(self._store.field_keys(str(values["session_id"])))
        # ---- 敏感词库（Task 9 turn 前置审核）----
        # 内存假库没有 config_sensitive_word 表:词库按"空"处理,moderate_text
        # 本地规则放行——与 load_active_sensitive_words 对不可用词库的优雅
        # 降级语义一致;需要验证 reject/replace 语义的测试直接 patch
        # app.services.ai.profile.moderate_text。
        if "FROM config_sensitive_word" in sql:
            return _MappingResult([])
        # ---- Phase 4 P4-01: ai_profile_projection_status ----
        # 假库只关心删除路径(走 mark_deleted):记录 status=deleted 即视为
        # 准入位已不可读;active/invalidated/pending 假库不模拟,默认视为"可读"。
        if "INSERT INTO ai_profile_projection_status" in sql:
            kind = str(values.get("kind") or "")
            status = str(values.get("status") or "")
            if status == "deleted":
                self._store.projection_status[(int(values["user_id"]), kind)] = {
                    "status": status, "reason": values.get("last_error")
                }
            # 其余状态假库不模拟(测试目标只关心删除)
            return _MappingResult([])
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
        # Phase 4 P4-01: ai_profile_projection_status 假存储
        self.projection_status: dict[tuple[int, str], dict[str, Any]] = {}
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
            "skipped_field_keys": None,
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
            "session_kind": str(params.get("session_kind") or "build"),
            "status": "draft",
            "active_status": 1,
            "consent_version": str(params["consent_version"]),
            "policy_revision": str(params["policy_revision"]),
            "current_question_id": None,
            "skipped_field_keys": params.get("skipped_field_keys"),
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
            "role": str(params.get("role") or "user"),
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
            "source_span": params.get("source_span"),
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
        elif "status = 'failed'" in sql:
            # ``_fail_extract_session`` 的终态失败自提交写入：字面量 SQL 带
            # ``WHERE ... AND status = 'extracting'`` 幂等守卫——非 extracting
            # 的会话不误伤（返回 rowcount=0 的 no-op）。
            if "status = 'extracting'" in sql and row["status"] != "extracting":
                return False
            row["status"] = "failed"
            row["active_status"] = 0
            row["ended_at"] = _now()
        elif "SET status = :status" in sql:
            row["status"] = str(params["status"])
        elif "skipped_field_keys = :skipped_field_keys" in sql:
            row["skipped_field_keys"] = params.get("skipped_field_keys")
        elif "input_mode = :input_mode" in sql:
            row["input_mode"] = str(params["input_mode"])
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
    # 隔离 content_filter 的敏感词全局缓存（60s TTL）：进入时清掉其他测试
    # 可能残留的词表（否则词库命中会让 submit 被误拒），退出时清掉本假库
    # 加载的空词表（避免反向污染后续依赖真实词表的测试）。
    clear_sensitive_word_cache()
    store = ProfileStore()
    prior = worker_mod.TASK_HANDLERS.get("profile_extract")
    worker_mod.TASK_HANDLERS["profile_extract"] = extract_profile_turn
    yield store
    if prior is None:
        worker_mod.TASK_HANDLERS.pop("profile_extract", None)
    else:
        worker_mod.TASK_HANDLERS["profile_extract"] = prior
    clear_sensitive_word_cache()


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
    # Simulate an adapter that bypassed its initial Pydantic construction.  The
    # Worker must still reject the whole extraction before it writes a draft.
    outcome = InvokeOutcome(
        result=StructuredExtractResult.model_construct(
            schema_version="profile-extract-v1",
            fields=(
                ExtractedField.model_construct(
                    field_key="realname_status",
                    subject=ProfileSubject.PERSONAL,
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
    assert result["task"]["status"] == "failed"
    assert result["task"]["error_code"] == "AI_INPUT_INVALID"
    assert profile_store.drafts == []
    assert profile_store.draft_fields == []


@pytest.mark.asyncio
async def test_extraction_rejects_forged_confidence_at_worker_boundary(
    profile_store, monkeypatch
) -> None:
    field = ExtractedField.model_construct(
        field_key="interest_tags",
        subject=ProfileSubject.PERSONAL,
        value=("旅行",),
        source_quote="喜欢旅行",
        source_span="喜欢旅行",
        confidence=1.7,
        confirmation_status="suggested",
    )
    outcome = InvokeOutcome(
        result=StructuredExtractResult.model_construct(
            schema_version="profile-extract-v1", fields=(field,)
        )
    )
    result = await _run_worker(profile_store, monkeypatch, _stub_gateway(outcome))
    assert result["task"]["status"] == "failed"
    assert result["task"]["error_code"] == "AI_INPUT_INVALID"
    assert profile_store.drafts == []
    assert profile_store.draft_fields == []


@pytest.mark.asyncio
async def test_extraction_rejects_forged_result_schema_version_at_worker_boundary(
    profile_store, monkeypatch
) -> None:
    field = ExtractedField(
        field_key="interest_tags",
        subject=ProfileSubject.PERSONAL,
        value=["旅行"],
        source_quote="喜欢旅行",
        confirmation_status="suggested",
    )
    outcome = InvokeOutcome(
        result=StructuredExtractResult.model_construct(
            schema_version="profile-extract-v0", fields=(field,)
        )
    )
    result = await _run_worker(profile_store, monkeypatch, _stub_gateway(outcome))
    assert result["task"]["status"] == "failed"
    assert result["task"]["error_code"] == "AI_INPUT_INVALID"
    assert profile_store.drafts == []
    assert profile_store.draft_fields == []


async def _seed_extract_task(
    profile_store: ProfileStore,
    *,
    session: dict[str, Any],
    turn: dict[str, Any],
    idempotency_key: str,
) -> AiTaskRecord:
    return await profile_store.task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until=_now() + timedelta(seconds=60),
        task_type="profile_extract",
        idempotency_key=idempotency_key,
        request_digest=idempotency_key,
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


@pytest.mark.asyncio
async def test_empty_extraction_falls_back_to_asked_tag_field(
    profile_store, monkeypatch
) -> None:
    """口语化回答抽不出字段时，把本轮所问的标签字段写成 suggested，避免同一题死循环。"""
    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="extracting"
    )
    turn = await profile_store.seed_turn(session["session_id"], "turn-001", "吃饭睡觉")
    task = await _seed_extract_task(
        profile_store, session=session, turn=turn, idempotency_key="extract-empty-tags"
    )
    monkeypatch.setattr(
        profile_mod,
        "AIGateway",
        _stub_gateway(
            InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version="profile-extract-v1", fields=()
                )
            )
        ),
    )
    result_ref, _revisions = await extract_profile_turn(
        profile_store.db, task, "worker-1"
    )
    assert result_ref.startswith("profile-draft:")
    keys = {field["field_key"] for field in profile_store.draft_fields}
    assert keys == {"interest_tags"}
    stored = profile_store.draft_fields[0]
    assert stored["confirmation_status"] == "suggested"
    assert stored["value"] == ["吃饭睡觉"]
    loaded = await load_owned_session(profile_store.db, session["session_id"], 10)
    assert loaded.current_question is not None
    assert loaded.current_question.field_key != "interest_tags"


@pytest.mark.asyncio
async def test_empty_extraction_for_non_tag_field_does_not_write_draft(
    profile_store, monkeypatch
) -> None:
    """非标签题抽空时不得写空草稿，否则下一问仍停在同一题。"""
    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="extracting"
    )
    profile_store.insert_draft(
        {
            "draft_id": "dr_existing",
            "user_id": 10,
            "subject": "personal",
            "session_id": session["session_id"],
            "consent_snapshot_json": "{}",
            "policy_revision": "ai-policy-2026-08-07-v1",
            "prompt_version": "profile-extract-prompt-v1",
            "schema_version": "profile-extract-v1",
        }
    )
    profile_store.insert_draft_field(
        {
            "draft_id": "dr_existing",
            "field_key": "interest_tags",
            "subject": "personal",
            "value_json": json.dumps(["旅行"], ensure_ascii=False),
            "display_value": "旅行",
            "source_turn_ids": json.dumps(["turn-0"], ensure_ascii=False),
            "source_span": "旅行",
            "confidence": 0.9,
            "visibility": "self",
            "consent_scope": "profile_text_extract",
            "schema_version": "profile-extract-v1",
            "prompt_version": "profile-extract-prompt-v1",
            "content_hash": "hash",
            "confirmation_status": "suggested",
        }
    )
    turn = await profile_store.seed_turn(session["session_id"], "turn-city", "还没想好")
    task = await _seed_extract_task(
        profile_store, session=session, turn=turn, idempotency_key="extract-empty-city"
    )
    monkeypatch.setattr(
        profile_mod,
        "AIGateway",
        _stub_gateway(
            InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version="profile-extract-v1", fields=()
                )
            )
        ),
    )
    result = await extract_profile_turn(profile_store.db, task, "worker-1")
    assert result is None
    final = await profile_store.task_store.get(task.task_id)
    assert final is not None
    assert final["status"] == "failed"
    assert final["error_code"] == "AI_INPUT_INVALID"
    assert {field["field_key"] for field in profile_store.draft_fields} == {"interest_tags"}
    loaded = await load_owned_session(profile_store.db, session["session_id"], 10)
    assert loaded.current_question is not None
    assert loaded.current_question.field_key == "city_code"


@pytest.mark.asyncio
async def test_extraction_accumulates_fields_on_latest_draft(
    profile_store, monkeypatch
) -> None:
    """后一轮抽取必须带上本会话已有字段，确认/成稿不能只看到最新一题。"""
    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="extracting"
    )
    first_turn = await profile_store.seed_turn(
        session["session_id"], "turn-001", "喜欢旅行"
    )
    first_task = await _seed_extract_task(
        profile_store, session=session, turn=first_turn, idempotency_key="extract-key-001"
    )
    first_field = ExtractedField(
        field_key="interest_tags",
        subject=ProfileSubject.PERSONAL,
        value=["旅行"],
        source_quote="喜欢旅行",
        confirmation_status="suggested",
    )
    monkeypatch.setattr(
        profile_mod,
        "AIGateway",
        _stub_gateway(
            InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version="profile-extract-v1", fields=(first_field,)
                )
            )
        ),
    )
    first_ref, _first_revisions = await extract_profile_turn(
        profile_store.db, first_task, "worker-1"
    )
    assert first_ref.startswith("profile-draft:")
    session_row = profile_store.sessions[session["session_id"]]
    session_row["status"] = "extracting"
    second_turn = await profile_store.seed_turn(
        session["session_id"], "turn-002", "我在杭州"
    )
    second_task = await _seed_extract_task(
        profile_store,
        session=session,
        turn=second_turn,
        idempotency_key="extract-key-002",
    )
    city_field = ExtractedField(
        field_key="city_code",
        subject=ProfileSubject.PERSONAL,
        value="330100",
        source_quote="我在杭州",
        confirmation_status="suggested",
    )
    monkeypatch.setattr(
        profile_mod,
        "AIGateway",
        _stub_gateway(
            InvokeOutcome(
                result=StructuredExtractResult(
                    schema_version="profile-extract-v1", fields=(city_field,)
                )
            )
        ),
    )
    second_ref, _second_revisions = await extract_profile_turn(
        profile_store.db, second_task, "worker-1"
    )
    assert second_ref.startswith("profile-draft:")
    latest = max(profile_store.drafts, key=lambda row: row["updated_at"])
    keys = {
        field["field_key"]
        for field in profile_store.fields_for_draft(latest["draft_id"])
    }
    assert keys == {"interest_tags", "city_code"}
    loaded = await load_owned_session(profile_store.db, session["session_id"], 10)
    assert loaded.current_question is not None
    assert loaded.current_question.field_key not in {"interest_tags", "city_code"}


@pytest.mark.asyncio
async def test_extraction_writes_full_source_evidence(profile_store) -> None:
    draft = await profile_store.run_mock_extraction("周末喜欢旅行和看展")
    first = draft.fields[0]
    assert first.confirmation_status == "suggested"
    stored = profile_store.fields_for_draft(draft.draft_id)
    assert len(stored) >= 1
    for field in stored:
        assert field["source_turn_ids"] is not None
        assert field["source_span"]
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
    # Task6 Step2：问题必须带稳定 field_key
    assert session.current_question.field_key == "interest_tags"


def test_next_question_never_repeats_confirmed_fields() -> None:
    session = ProfileSession(
        session_id="ps_x",
        owner_user_id=10,
        subject=ProfileSubject.PERSONAL,
        status=ProfileSessionStatus.DRAFT,
        input_mode="text",
        session_kind="build",
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
    # Task6 Step2：跳过已确认字段后，返回的问题 field_key 也不应重复
    assert question.field_key not in {"interest_tags", "city_code"}


def test_question_bank_field_keys_cover_allowlist_subset() -> None:
    """Task6 Step2：question bank 的每个 field_key 都对应一个 allowlist 字段。"""
    from app.schemas.ai_common import AI_FIELD_ALLOWLIST

    for field_key, question in _PROFILE_QUESTION_BANK.items():
        assert question.field_key == field_key
        assert field_key in AI_FIELD_ALLOWLIST


def test_profile_session_carries_nullable_draft_id_default() -> None:
    """Task6 Step2：ProfileSession 默认 draft_id=None（加法字段，无活动草稿）。"""
    session = ProfileSession(
        session_id="ps_x",
        owner_user_id=10,
        subject=ProfileSubject.PERSONAL,
        status=ProfileSessionStatus.DRAFT,
        input_mode="text",
        session_kind="build",
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
        current_question=None,
        revision_vector=None,  # type: ignore[arg-type]
        consent_snapshot={},
        field_keys=frozenset(),
        confirmed_keys=frozenset(),
        profile_revision=0,
        preference_revision=0,
        expires_at=None,
        created_at=None,
        updated_at=None,
    )
    assert session.draft_id is None


@pytest.mark.asyncio
async def test_create_session_returns_none_draft_id_before_extraction(
    profile_store,
) -> None:
    """Task6 Step2：新建会话尚未抽取时，session.draft_id 为 None。"""
    session = await create_profile_session(
        profile_store.db, 10, ProfileSubject.PERSONAL, "profile-text-v1", "key-draft-none-001"
    )
    assert session.draft_id is None


def test_profile_session_read_serializes_draft_id() -> None:
    """Task6 Step2：ProfileSessionRead schema 序列化 draft_id（None 和非 None）。"""
    from datetime import datetime as _dt

    from app.schemas.ai_profile import (
        ProfileProgress,
    )
    from app.schemas.ai_profile import (
        ProfileSessionRead as SchemaSessionRead,
    )

    read_none = SchemaSessionRead(
        session_id="ps_1",
        subject=ProfileSubject.PERSONAL,
        status=ProfileSessionStatus.DRAFT,
        input_mode="text",
        progress=ProfileProgress(basis="confirmed_field_coverage", value=0.0),
        current_question=None,
        draft_id=None,
        profile_revision=0,
        preference_revision=0,
        expires_at=None,
        created_at=_dt(2026, 8, 15, 0, 0, 0),
    )
    dumped_none = read_none.model_dump()
    assert dumped_none["draft_id"] is None

    read_with_draft = read_none.model_copy(update={"draft_id": "dr_abc"})
    dumped = read_with_draft.model_dump()
    assert dumped["draft_id"] == "dr_abc"


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
    # Task6 Step2：API 响应的 current_question 含稳定 field_key（加法字段）
    assert body["current_question"]["field_key"] == "interest_tags"
    # Task6 Step2：新建会话尚未抽取，draft_id 为 None（加法字段）
    assert body["draft_id"] is None


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
async def test_master_session_replaces_legacy_active_session_without_deleting_it(
    profile_store,
) -> None:
    """master 入口不得静默复用旧 build/update 会话。"""
    legacy = await profile_store.seed_session(
        owner_user_id=10,
        subject="personal",
        status="draft",
        session_id="legacy_build_personal",
    )
    legacy["session_kind"] = "build"

    session = await create_master_session(
        profile_store.db,
        10,
        ProfileSubject.PERSONAL,
        "profile-text-v1",
    )

    assert session.session_id != legacy["session_id"]
    assert session.session_kind == "master"
    assert legacy["status"] == "stale"
    assert legacy["active_status"] == 0
    assert legacy["session_id"] in profile_store.sessions


@pytest.mark.asyncio
async def test_ideal_partner_extraction_rejects_provider_subject_mismatch(
    profile_store, monkeypatch
) -> None:
    """Cross-subject provider output is rejected before any draft write."""
    outcome = InvokeOutcome(
        result=StructuredExtractResult(
            schema_version="profile-extract-v1",
            fields=(
                ExtractedField(
                    field_key="city_code",
                    subject="personal",
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
    assert result == "failed"
    final = await profile_store.task_store.get(task.task_id)
    assert final is not None
    assert final["status"] == "failed"
    assert final["error_code"] == "AI_INPUT_INVALID"
    assert profile_store.drafts == []
    assert profile_store.draft_fields == []


@pytest.mark.asyncio
async def test_mock_extraction_returns_subject_aware_ideal_partner_constraints() -> None:
    """ideal_partner mock values are preferences, never personal fact scalars."""
    result = await MockAIProvider().structured_extract(
        StructuredExtractRequest(
            subject="ideal_partner",
            turn_texts=("希望另一半身高160到180，月收入至少一万",),
            consent_version="profile-text-v1",
            policy_revision="ai-policy-2026-08-07-v1",
        )
    )

    fields = {field.field_key: field for field in result.fields}
    assert all(field.subject is ProfileSubject.IDEAL_PARTNER for field in fields.values())
    assert fields["height_cm"].value == {"min": 160, "max": 180}
    assert fields["income_band"].value == {"min": 10000, "max": None}
    assert fields["interest_tags"].value == ("旅行", "音乐")
    assert fields["height_cm"].source_span
    assert fields["height_cm"].schema_version == "profile-extract-v1"
    assert fields["height_cm"].prompt_version == "profile-extract-prompt-v1"
    assert fields["height_cm"].policy_revision == "ai-policy-2026-08-07-v1"


@pytest.mark.parametrize(
    "payload",
    (
        {"field_key": "realname_status", "subject": "personal", "value": 1},
        {"field_key": "height_cm", "subject": "personal", "value": {"min": 160, "max": 180}},
        {"field_key": "height_cm", "subject": "ideal_partner", "value": 172},
        {"field_key": "age", "subject": "ideal_partner", "value": {"min": 36, "max": 26}},
    ),
)
def test_extracted_field_rejects_unknown_forged_or_subject_invalid_values(
    payload: dict[str, Any],
) -> None:
    """Bad provider values fail schema validation before a draft can be created."""
    with pytest.raises(ValidationError):
        ExtractedField.model_validate(payload)


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


# ----------------------------------------------------------------------
# Task 8: 抽取 JSON 失败可重试 + 会话终态失败置 FAILED
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_json_parse_failure_is_retryable() -> None:
    """provider 返回非合法 JSON 时应分类为 RETRYABLE,而非一次判死。"""
    from app.services.ai.providers import _parse_json_response, ProviderError, ProviderErrorKind

    with pytest.raises(ProviderError) as exc_info:
        _parse_json_response("not json at all")
    assert exc_info.value.kind == ProviderErrorKind.RETRYABLE


@pytest.mark.asyncio
async def test_session_failed_transitions(profile_store) -> None:
    """EXTRACTING→FAILED 合法(终态失败写入);FAILED 是终态,迁往任何状态非法。"""
    from app.services.ai.profile import assert_session_transition

    session = await profile_store.seed_session(status="extracting")
    assert session["status"] == "extracting"
    # EXTRACTING → FAILED 合法:Task 8 让不可重试终态失败能写入 FAILED,
    # 否则前端会一直停留在"提取中"。
    assert_session_transition(ProfileSessionStatus.EXTRACTING, ProfileSessionStatus.FAILED)
    # FAILED 是终态:转移表为空集合,迁往任何状态(含自身)都非法。
    for target in ProfileSessionStatus:
        with pytest.raises(ValueError):
            assert_session_transition(ProfileSessionStatus.FAILED, target)


# ----------------------------------------------------------------------
# Task 8 修复回合：终态失败的会话 FAILED 必须穿透 worker 回滚持久化
# ----------------------------------------------------------------------


class _ProviderSession(FakeProfileSession):
    """适配 worker ``session_provider`` 协议的 FakeProfileSession。

    生产 worker 用独立 session 运行 handler、由 ``finalize_handler(True/False)``
    决定 commit/rollback。基类的 rollback 只在「曾 commit 过」时还原，但双会话
    模式下每个 handler session 都是独立开启的——真实 DB 语义是「未 commit 的
    写入在 rollback 时全部撤销，回到 session 开启时的库状态」。因此在
    ``__aenter__`` 时先快照整个 store 作为回滚基线，rollback 无 commit 基线时
    还原到该快照，忠实再现 ``finalize_handler(False)`` 丢弃 handler 未提交
    写入的生产行为（否则 1c0e42a 的"FAILED 写入被回滚丢弃"缺陷会被假 store
    掩盖）。
    """

    def __init__(self, store: ProfileStore) -> None:
        super().__init__(store)
        self._entry_snapshot: dict[str, Any] | None = None

    async def __aenter__(self) -> _ProviderSession:
        self._entry_snapshot = self._snapshot_store()
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self._committed_snapshot is not None:
            self._restore_store(self._committed_snapshot)
        elif self._entry_snapshot is not None:
            self._restore_store(self._entry_snapshot)


async def _run_worker_isolated(
    profile_store: ProfileStore,
    monkeypatch: pytest.MonkeyPatch,
    gateway_type: type,
    *,
    payload_turn_id: str | None = None,
) -> dict[str, Any]:
    """经 worker 的 ``session_provider`` 双会话模式跑一个 extracting 会话的抽取任务。

    与 ``_run_worker`` 的区别：handler 在自己的独立 session 中运行，其写入由
    ``_process`` 的 ``finalize_handler(commit/rollback)`` 裁决——即生产路径的
    真实事务边界。``payload_turn_id`` 可覆写任务 payload 里的 turn_id（模拟
    turn 定位失败的终态分支）。返回 outcome/task 行供断言。
    """
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
            "turn_id": payload_turn_id or turn["turn_id"],
            "client_turn_id": turn["client_turn_id"],
            "subject": "personal",
        },
    )
    monkeypatch.setattr(profile_mod, "AIGateway", gateway_type)
    outcome = await worker_mod._process(
        None, task, "worker-1", session_provider=lambda: _ProviderSession(profile_store)
    )
    final = await profile_store.task_store.get(task.task_id)
    assert final is not None
    return {"outcome": outcome, "task": final, "session": session["session_id"]}


@pytest.mark.asyncio
async def test_terminal_extract_failure_marks_session_failed_durably(
    profile_store, monkeypatch
) -> None:
    """终态(不可重试)抽取失败：任务 failed 不进 retry_wait；会话 FAILED 持久化。

    关键断言是用全新 session 读库：handler 内 ``_fail_extract_session`` 的自提交
    必须让 FAILED 写入穿透 ``finalize_handler(False)`` 的回滚——这正是评审
    Critical 指出的原缺陷（1c0e42a 在 handler 事务内写 FAILED 但无 commit，
    worker 对返回 None 的 handler 无条件回滚，写入被丢弃，会话永远 extracting）。
    同时 fail_task(retryable=False) 被同一次 commit 固化，worker 事后用硬编码
    retryable=True 的重记撞终态守卫变 no-op，任务不进 retry_wait。
    """
    result = await _run_worker_isolated(
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
    assert result["outcome"] == "failed"
    task_row = result["task"]
    assert task_row["status"] == "failed"
    assert task_row["error_code"] == "AI_INPUT_INVALID"
    # 未进 retry_wait：worker 的重记被 fail_task 的终态守卫挡下。
    assert task_row["attempt_count"] == 0
    # 用全新 session 查询，证明写穿过了 finalize_handler(False) 的回滚。
    fresh = FakeProfileSession(profile_store)
    read = await fresh.execute(
        "SELECT session_id, status, active_status, ended_at FROM ai_profile_session "
        "WHERE session_id = :session_id",
        {"session_id": result["session"]},
    )
    row = read.mappings().first()
    assert row is not None
    assert row["status"] == "failed"
    assert row["active_status"] == 0
    assert row["ended_at"] is not None


@pytest.mark.asyncio
async def test_retryable_extract_failure_keeps_session_extracting(
    profile_store, monkeypatch
) -> None:
    """可重试抽取失败：任务进 retry_wait，会话保持 extracting 等待重试。"""
    result = await _run_worker_isolated(
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
    assert result["outcome"] == "failed"
    task_row = result["task"]
    assert task_row["status"] == "retry_wait"
    assert task_row["error_code"] == "AI_TEMPORARILY_UNAVAILABLE"
    # 可重试失败不改会话状态：worker 退避后重试，会话仍在 extracting。
    fresh = FakeProfileSession(profile_store)
    read = await fresh.execute(
        "SELECT session_id, status, active_status, ended_at FROM ai_profile_session "
        "WHERE session_id = :session_id",
        {"session_id": result["session"]},
    )
    row = read.mappings().first()
    assert row is not None
    assert row["status"] == "extracting"
    assert row["active_status"] == 1
    assert row["ended_at"] is None


@pytest.mark.asyncio
async def test_turn_mismatch_failure_marks_session_failed_durably(
    profile_store, monkeypatch
) -> None:
    """payload/turn 不匹配的终态分支：任务 failed、会话 FAILED 同样持久化。"""
    result = await _run_worker_isolated(
        profile_store,
        monkeypatch,
        _stub_gateway(
            InvokeOutcome(result=StructuredExtractResult(schema_version="profile-extract-v1", fields=()))
        ),
        payload_turn_id="turn-does-not-match",
    )
    assert result["outcome"] == "failed"
    task_row = result["task"]
    assert task_row["status"] == "failed"
    assert task_row["error_code"] == "AI_INPUT_INVALID"
    assert task_row["attempt_count"] == 0
    fresh = FakeProfileSession(profile_store)
    read = await fresh.execute(
        "SELECT session_id, status, active_status FROM ai_profile_session "
        "WHERE session_id = :session_id",
        {"session_id": result["session"]},
    )
    row = read.mappings().first()
    assert row is not None
    assert row["status"] == "failed"
    assert row["active_status"] == 0


@pytest.mark.asyncio
async def test_payload_missing_turn_id_marks_session_failed(profile_store) -> None:
    """payload 缺 turn_id（session_id 仍在）的终态分支：会话 FAILED、任务 failed。"""
    session = await profile_store.seed_session(
        owner_user_id=10, subject="personal", status="extracting"
    )
    task = await profile_store.task_store.seed(
        status="running",
        lease_owner="worker-1",
        lease_until=_now() + timedelta(seconds=60),
        task_type="profile_extract",
        idempotency_key="extract-key-payload",
        payload_summary={"session_id": session["session_id"], "subject": "personal"},
    )
    result = await extract_profile_turn(profile_store.db, task, "worker-1")
    assert result is None
    final = await profile_store.task_store.get(task.task_id)
    assert final is not None
    assert final["status"] == "failed"
    assert final["error_code"] == "AI_FEATURE_DISABLED"
    row = profile_store.sessions[session["session_id"]]
    assert row["status"] == "failed"
    assert row["active_status"] == 0
    assert row["ended_at"] is not None


# ----------------------------------------------------------------------
# Task 9: turn 提交前置内容审核（moderate_text）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_turn_rejects_moderated_content(profile_store) -> None:
    """turn 提交必须前置内容审核,违规文本拒绝入库。"""
    from app.services.content_filter import ContentDecision
    from unittest.mock import AsyncMock, patch

    session = await profile_store.seed_session(owner_user_id=10, subject="personal")
    with patch(
        "app.services.ai.profile.moderate_text",
        new_callable=AsyncMock,
        return_value=ContentDecision(action="reject", display_content="违规内容"),
    ):
        with pytest.raises(AIInputError):
            await submit_profile_turn(
                profile_store.db, session["session_id"], 10, "turn-001", "违规内容", "key-001"
            )
    # reject 时 turn 未落库、task 未入队。
    assert profile_store.turns == []
    assert list(profile_store.task_store.tasks.values()) == []


@pytest.mark.asyncio
async def test_submit_turn_replace_uses_moderated_text(profile_store) -> None:
    """replace 动作用审核后的脱敏文本替代原文落库并进入抽取链路。"""
    from unittest.mock import AsyncMock, patch

    from app.services.content_filter import ContentDecision

    session = await profile_store.seed_session(owner_user_id=10, subject="personal")
    with patch(
        "app.services.ai.profile.moderate_text",
        new_callable=AsyncMock,
        return_value=ContentDecision(action="replace", display_content="周末喜欢去***"),
    ):
        submission = await submit_profile_turn(
            profile_store.db, session["session_id"], 10, "turn-001", "周末喜欢去酒吧", "key-001"
        )
    # 落库与返回的 answer_text 都是脱敏文本,原文绝不进入 LLM prompt 链路。
    assert submission.answer_text == "周末喜欢去***"
    assert all(row["answer_text"] == "周末喜欢去***" for row in profile_store.turns)


@pytest.mark.asyncio
async def test_submit_turn_replay_skips_moderation(profile_store) -> None:
    """幂等回放不重复审核:已落库 turn 的重放不因词库事后收紧被拒。"""
    from unittest.mock import AsyncMock, patch

    from app.services.content_filter import ContentDecision

    session = await profile_store.seed_session(owner_user_id=10, subject="personal")
    with patch(
        "app.services.ai.profile.moderate_text",
        new_callable=AsyncMock,
        return_value=ContentDecision(action="allow", display_content="周末喜欢看展"),
    ) as first_moderate:
        first = await submit_profile_turn(
            profile_store.db, session["session_id"], 10, "turn-001", "周末喜欢看展", "key-001"
        )
        first_moderate.assert_awaited_once()
    # 提交后词库/外部审核收紧为 reject:同一 client_turn_id 必须原样回放,不再审核。
    with patch(
        "app.services.ai.profile.moderate_text",
        new_callable=AsyncMock,
        return_value=ContentDecision(action="reject", display_content="周末喜欢看展"),
    ) as replay_moderate:
        replay = await submit_profile_turn(
            profile_store.db, session["session_id"], 10, "turn-001", "周末喜欢看展", "key-001"
        )
        replay_moderate.assert_not_called()
    assert replay.turn_id == first.turn_id
    assert replay.replayed is True
    assert await profile_store.count_tasks(first.turn_id) == 1


# ----------------------------------------------------------------------
# WP-P2：进度引导（can_early_publish）与发布门槛共用可配置阈值
# ----------------------------------------------------------------------


def test_session_read_can_early_publish_follows_threshold(monkeypatch) -> None:
    """进度引导（方案 WP-P2）：确认字段数达到可配置阈值时 can_early_publish=True。

    阈值来自 settings.ai_profile_min_fields（默认 7 = 10 字段的 67%），
    提前建构与发布共用同一阈值，避免两套数字漂移。
    """
    from types import SimpleNamespace

    from app.api.routes.ai_profile import _to_session_read
    from app.schemas.ai_profile import ProfileSessionStatus

    def _session(confirmed):
        return SimpleNamespace(
            session_id="s-1",
            subject=ProfileSubject("personal"),
            status=ProfileSessionStatus("draft"),
            input_mode="text",
            session_kind="build",
            current_question=None,
            confirmed_keys=frozenset(confirmed),
            draft_id=None,
            profile_revision=0,
            preference_revision=0,
            expires_at=None,
            created_at=_now(),
        )

    monkeypatch.setattr(settings, "ai_profile_min_fields", 7)
    below = _to_session_read(
        _session(["age", "city", "height", "education", "income", "occupation"])
    )
    assert below.progress.can_early_publish is False
    assert below.progress.early_publish_hint == ""

    at = _to_session_read(
        _session(["age", "city", "height", "education", "income", "occupation", "interest"])
    )
    assert at.progress.can_early_publish is True
    assert "提前" in at.progress.early_publish_hint

    monkeypatch.setattr(settings, "ai_profile_min_fields", 5)
    lowered = _to_session_read(
        _session(["age", "city", "height", "education", "income"])
    )
    assert lowered.progress.can_early_publish is True
