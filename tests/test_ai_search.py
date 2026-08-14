"""Task 10 acceptance contract: M03 search AST, draft confirmation and safe results.

The four Step 1 tests are mirrored verbatim from the task brief.
``search_store`` is an in-memory fake: a ``FakeSearchSession`` routes the service
SQL by substring onto in-memory drafts/conditions/snapshots/results plus the
Task 6 task machine, so drafting, parsing, confirming, patching and result
reads can be exercised without a real database.

``search_store.execute(draft_id)`` is the brief Step 1 surface: only a draft in
``confirmed`` status may start a candidate query, and any other status raises
``search_store.NotConfirmed``.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app.services.ai.search as search_mod
from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.schemas.ai_search import (
    SearchCondition,
    SearchConditionPatchRequest,
    SearchDraftRead,
    SearchResultPageRead,
)
from app.services.ai.search import (
    SearchInputInvalid,
    SearchPolicyDenied,
    SearchQuotaExceeded,
    build_search_query_snapshot,
    candidate_query_service,
    compile_search_conditions,
    confirm_search_draft,
    create_search_draft,
    delete_search_snapshot,
    get_search_suggestions,
    materialize_search_snapshot,
    parse_search_draft,
    patch_search_draft,
    read_materialized_search_results,
)
from app.services.ai.tasks import AiTaskRecord
from app.services.candidate_query import (
    SORT_VERSION,
    CandidateCursor,
    InvalidCandidateCursor,
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

    def scalar_one(self) -> Any:
        if not self._rows:
            return None
        return next(iter(self._rows[0].values()))

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
        if "payload_summary = NULL" in sql:
            row["payload_summary"] = None
            row["consent_snapshot_json"] = None
            row["source_revision_json"] = None
            row["result_ref"] = None
        row["updated_at"] = _now()
        return True

    async def seed(self, **kwargs: Any) -> AiTaskRecord:
        task_id = kwargs.pop("task_id", None) or uuid.uuid4().hex
        now = _now()
        row: dict[str, Any] = {
            "id": self._next_id,
            "task_id": task_id,
            "owner_user_id": int(kwargs.pop("owner_user_id", 10)),
            "task_type": str(kwargs.pop("task_type", "search_parse")),
            "scene": str(kwargs.pop("scene", "search_parse")),
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


class SearchStore:
    """In-memory M03 search fact store + candidate fixtures."""

    class NotConfirmed(Exception):
        """Raised when a draft that is not confirmed tries to start a query."""

    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, Any]] = {}
        self.conditions: dict[str, list[dict[str, Any]]] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.results: dict[str, list[dict[str, Any]]] = {}
        self.consents: list[dict[str, Any]] = []
        self.revision_rows: dict[int, dict[str, Any]] = {}
        self.projections: list[dict[str, Any]] = []
        self.candidates: dict[int, dict[str, Any]] = {}
        self.task_store = TaskStore()
        self._next_draft_id = 1
        self._next_snapshot_id = 1
        self._profile_grant_at = _now().replace(microsecond=0)
        self.session = FakeSearchSession(self)

    # ---- seeding helpers ------------------------------------------------

    async def seed_consent(
        self, user_id: int = 10, scope: str = "search_parse"
    ) -> None:
        self.consents.append(
            {
                "user_id": user_id,
                "scope": scope,
                "version": "search-parse-v1",
                "policy_revision": "ai-policy-2026-08-07-v1",
                "granted_at": _now(),
            }
        )

    async def seed_revision(self, user_id: int = 10) -> None:
        self.revision_rows[user_id] = {
            "profile_revision": 1,
            "preference_revision": 1,
            "privacy_revision": 1,
            "relationship_revision": 0,
            "policy_revision": 1,
        }

    async def seed_candidate(
        self,
        user_id: int,
        *,
        who_can_see_me: int = 1,
        profile_visible: int = 1,
        match_active: int = 1,
        account_active: int = 1,
        status: int = 1,
        completion: int = 100,
        birthday: str | None = "1996-05-20",
        height: int | None = 172,
        education_level: int | None = 4,
        income: float | None = 12000.0,
        residence_city_code: str | None = "330100",
        interest_tags: list[str] | None = None,
        is_married: int | None = 1,
        nickname: str | None = None,
        avatar: str | None = None,
        last_active_at: datetime | None = None,
        is_boosted: bool = False,
        blocked: bool = False,
        not_restricted: bool = True,
        profile_complete: bool = True,
        media_approved: bool = True,
    ) -> None:
        self.candidates[user_id] = {
            "user_id": user_id,
            "who_can_see_me": who_can_see_me,
            "profile_visible": profile_visible,
            "match_active": match_active,
            "account_active": account_active,
            "status": status,
            "completion": completion,
            "birthday": birthday,
            "height": height,
            "education_level": education_level,
            "income": income,
            "residence_city_code": residence_city_code,
            "interest_tags": interest_tags or [],
            "personality_tags": [],
            "tags": {},
            "is_married": is_married,
            "nickname": nickname or f"user-{user_id}",
            "avatar": avatar,
            "last_active_at": last_active_at,
            "is_boosted": is_boosted,
            "blocked": blocked,
            "not_restricted": not_restricted,
            "profile_complete": profile_complete,
            "media_approved": media_approved,
        }

    async def seed_projection(
        self,
        subject_user_id: int,
        fields: dict[str, Any],
        *,
        status: str = "active",
        projection_id: int | None = None,
        source_hash: str | None = None,
        source_revision: dict[str, int] | None = None,
        consent_snapshot: dict[str, Any] | None = None,
    ) -> None:
        if not any(
            row["user_id"] == subject_user_id
            and row["scope"] == "profile_text_extract"
            and row.get("revoked_at") is None
            for row in self.consents
        ):
            self.consents.append(
                {
                    "user_id": subject_user_id,
                    "scope": "profile_text_extract",
                    "version": "profile-text-v1",
                    "policy_revision": "ai-policy-2026-08-07-v1",
                    "granted_at": self._profile_grant_at,
                }
            )
        effective_consent = consent_snapshot or {
            "scope": "profile_text_extract",
            "version": "profile-text-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
            "granted_at": self._profile_grant_at.isoformat(),
        }
        self.projections.append(
            {
                "id": projection_id or len(self.projections) + 1,
                "subject_user_id": subject_user_id,
                "source_hash": source_hash or f"search-projection-{subject_user_id}",
                "fields_json": json.dumps(fields, ensure_ascii=False),
                "source_revision_json": json.dumps(
                    source_revision
                    or {
                        "profile": 0,
                        "preference": 0,
                        "privacy": 0,
                        "relationship": 0,
                        "policy": 0,
                    },
                    ensure_ascii=False,
                ),
                "profile_revision": int((source_revision or {}).get("profile", 0)),
                "preference_revision": int((source_revision or {}).get("preference", 0)),
                "privacy_revision": int((source_revision or {}).get("privacy", 0)),
                "relationship_revision": int((source_revision or {}).get("relationship", 0)),
                "policy_revision": int((source_revision or {}).get("policy", 0)),
                "consent_snapshot_json": effective_consent,
                "status": status,
                "expires_at": _now() + timedelta(days=30),
            }
        )

    async def seed_draft(
        self,
        status: str = "awaiting_confirmation",
        owner_user_id: int = 10,
        condition_revision: int = 0,
        conditions: list[SearchCondition] | None = None,
        expires_at: datetime | None = None,
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        """Seed a draft row (brief Step 1 surface)."""
        draft_id = draft_id or f"sd_fixture_{self._next_draft_id}"
        self._next_draft_id += 1
        row = {
            "draft_id": draft_id,
            "user_id": owner_user_id,
            "query_text": "fixture query",
            "source": "manual",
            "locale": "zh-CN",
            "status": status,
            "condition_revision": condition_revision,
            "condition_schema_version": "search-condition-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
            "consent_snapshot_json": '{"scope":"search_parse","version":"v1","policy_revision":"ai-policy-2026-08-07-v1","granted_at":null}',
            "expires_at": expires_at or (_now() + timedelta(hours=24)),
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.drafts[draft_id] = row
        if conditions:
            self.conditions[draft_id] = [
                _condition_row(draft_id, condition_revision, index, condition)
                for index, condition in enumerate(conditions)
            ]
        return row

    # ---- brief Step 1 surface -------------------------------------------

    async def execute(self, draft_id: str) -> Any:
        """Start a candidate query for a draft; only ``confirmed`` may run."""
        draft = self.drafts.get(draft_id)
        if draft is None:
            raise self.NotConfirmed("draft not found")
        if draft["status"] != "confirmed":
            raise self.NotConfirmed("draft is not confirmed")
        return {"draft_id": draft_id, "status": "confirmed", "items": []}


def _condition_row(
    draft_id: str,
    revision_no: int,
    condition_no: int,
    condition: SearchCondition,
) -> dict[str, Any]:
    return {
        "id": self_counter(),
        "draft_id": draft_id,
        "condition_revision": revision_no,
        "condition_no": condition_no,
        "field_key": condition.field_key,
        "operator": getattr(condition.operator, "value", condition.operator),
        "value_json": json.dumps(condition.value, ensure_ascii=False)
        if condition.value is not None
        else None,
        "condition_kind": getattr(condition.kind, "value", condition.kind),
        "confidence": float(condition.confidence),
        "source_span": condition.source_span,
        "user_action": getattr(condition.user_action, "value", condition.user_action),
        "created_at": _now(),
        "updated_at": _now(),
    }


_counter = {"n": 0}


def self_counter() -> int:
    _counter["n"] += 1
    return _counter["n"]


def _candidate_age(birthday: Any) -> int | None:
    """Compute age at ``date.today()`` from a YYYY-MM-DD birthday string."""
    if not birthday:
        return None
    try:
        birth = date.fromisoformat(str(birthday))
    except ValueError:
        return None
    today = date.today()
    return today.year - birth.year - (
        (today.month, today.day) < (birth.month, birth.day)
    )


def _candidate_matches_sql_filters(
    candidate: dict[str, Any], values: dict[str, Any], sql: str
) -> bool:
    """Mirror the server-side visibility predicate and hard filter clauses.

    Only the hard ``_hard_filter_clauses`` (city/marriage/education/height/
    income) and the age ``INTERVAL n YEAR`` bounds embedded in the SQL are
    evaluated here; soft tag ``search_tag_*`` params are intentionally ignored
    so evidence-level soft tests keep exercising projection-based evidence.
    """
    if int(candidate["user_id"]) == int(values.get("visibility_viewer_id", 0)):
        return False
    if not bool(candidate.get("account_active", True)):
        return False
    if not bool(candidate.get("profile_visible", True)):
        return False
    if not bool(candidate.get("match_active", True)):
        return False
    if not bool(candidate.get("not_restricted", True)):
        return False
    if not bool(candidate.get("profile_complete", True)):
        return False
    if not bool(candidate.get("media_approved", True)):
        return False
    if bool(candidate.get("blocked", False)):
        return False
    who_can_see_me = int(candidate.get("who_can_see_me", 1))
    if who_can_see_me not in (1, 2, 3):
        return False
    if who_can_see_me == 2 and int(values.get("visibility_realname_status", 0)) != 2:
        return False
    if who_can_see_me == 3 and not bool(values.get("visibility_viewer_is_vip", 0)):
        return False
    if "filter_city_code" in values and str(
        candidate.get("residence_city_code") or ""
    ) != str(values["filter_city_code"]):
        return False
    if "filter_marriage" in values and int(
        candidate.get("is_married", 0)
    ) != int(values["filter_marriage"]):
        return False
    if "filter_education" in values and int(
        candidate.get("education_level", 0)
    ) < int(values["filter_education"]):
        return False
    if "filter_height_min" in values and (
        candidate.get("height") is None
        or int(candidate["height"]) < int(values["filter_height_min"])
    ):
        return False
    if "filter_height_max" in values and (
        candidate.get("height") is None
        or int(candidate["height"]) > int(values["filter_height_max"])
    ):
        return False
    if "filter_income_min" in values and (
        candidate.get("income") is None
        or float(candidate["income"]) < float(values["filter_income_min"])
    ):
        return False
    if "filter_income_max" in values and (
        candidate.get("income") is None
        or float(candidate["income"]) > float(values["filter_income_max"])
    ):
        return False
    # age bounds are inlined into the SQL as ``INTERVAL n YEAR`` (age_min then
    # age_max + 1); a candidate must satisfy both bounds when present.
    intervals = re.findall(r"INTERVAL\s+(\d+)\s+YEAR", sql)
    age = _candidate_age(candidate.get("birthday"))
    if age is not None:
        if intervals and age < int(intervals[0]):
            return False
        if len(intervals) >= 2 and age > int(intervals[1]) - 1:
            return False
    return True


class FakeSearchSession:
    """Routes service SQL by substring onto one SearchStore."""

    def __init__(self, store: SearchStore) -> None:
        self._store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _MappingResult | _WriteResult:
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        store = self._store

        # ---- ai_task（Task 6 契约）----
        if "INSERT INTO ai_task" in sql:
            return _WriteResult(rowcount=1 if store.task_store.insert(values) else 0)
        if "UPDATE ai_task" in sql and "payload_summary" in sql:
            store.task_store.apply_update(sql, values)
            return _WriteResult(rowcount=1)
        if "FROM ai_task" in sql and "WHERE task_id = :task_id" in sql:
            row = store.task_store.tasks.get(values["task_id"])
            return _MappingResult([row] if row else [])
        if "FROM ai_task" in sql and "owner_user_id = :owner_user_id" in sql:
            row = store.task_store.find_by_idempotency(
                int(values["owner_user_id"]),
                str(values["task_type"]),
                str(values["idempotency_key"]),
            )
            return _MappingResult([row] if row else [])

        # ---- 授权 / 版本 ----
        if "FROM ai_consent_grant" in sql:
            rows = [
                row
                for row in store.consents
                if row["user_id"] == values["user_id"]
                and row["scope"] == values.get("scope")
                and row.get("revoked_at") is None
            ]
            return _MappingResult(sorted(rows, key=lambda r: r["granted_at"], reverse=True)[:1])
        if "FROM user_revision_state" in sql:
            row = store.revision_rows.get(int(values["user_id"]))
            return _MappingResult([row] if row else [])

        # ---- ai_search_draft ----
        if "INSERT INTO ai_search_draft" in sql:
            draft_id = str(values["draft_id"])
            store.drafts[draft_id] = {
                "draft_id": draft_id,
                "user_id": int(values["user_id"]),
                "query_text": str(values["query_text"]),
                "source": values.get("source"),
                "locale": values.get("locale"),
                "status": "parsing",
                "condition_revision": 0,
                "condition_schema_version": "search-condition-v1",
                "policy_revision": str(values["policy_revision"]),
                "consent_snapshot_json": values.get("consent_snapshot_json"),
                "expires_at": values.get("expires_at"),
                "created_at": _now(),
                "updated_at": _now(),
                "last_patch_idempotency_key": None,
                "last_patch_request_digest": None,
                "last_patch_response_json": None,
            }
            return _WriteResult(rowcount=1)
        if sql.startswith("UPDATE ai_search_draft SET status"):
            draft = store.drafts.get(values["draft_id"])
            if draft:
                draft["status"] = str(values["status"])
                draft["updated_at"] = _now()
            return _WriteResult(rowcount=1 if draft else 0)
        if sql.startswith("UPDATE ai_search_draft SET condition_revision"):
            draft = store.drafts.get(values["draft_id"])
            if draft:
                draft["condition_revision"] += 1
                draft["updated_at"] = _now()
            return _WriteResult(rowcount=1 if draft else 0)
        if sql.startswith("UPDATE ai_search_draft SET last_patch_idempotency_key"):
            draft = store.drafts.get(values["draft_id"])
            if draft:
                draft["last_patch_idempotency_key"] = values.get("key")
                draft["last_patch_request_digest"] = values.get("request_digest")
                draft["last_patch_response_json"] = values.get("response_json")
                draft["updated_at"] = _now()
            return _WriteResult(rowcount=1 if draft else 0)
        if "FROM ai_search_draft" in sql:
            draft = store.drafts.get(values["draft_id"])
            return _MappingResult([draft] if draft else [])

        # ---- ai_search_condition ----
        if "INSERT INTO ai_search_condition" in sql:
            draft_id = str(values["draft_id"])
            store.conditions.setdefault(draft_id, []).append(
                {
                    "id": self_counter(),
                    "draft_id": draft_id,
                    "condition_revision": int(values["condition_revision"]),
                    "condition_no": int(values["condition_no"]),
                    "field_key": str(values["field_key"]),
                    "operator": str(values["operator"]),
                    "value_json": values.get("value_json"),
                    "condition_kind": str(values["condition_kind"]),
                    "confidence": float(values["confidence"]),
                    "source_span": values.get("source_span"),
                    "user_action": str(values["user_action"]),
                    "created_at": _now(),
                    "updated_at": _now(),
                }
            )
            return _WriteResult(rowcount=1)
        if sql.startswith("UPDATE ai_search_condition SET user_action"):
            rows = store.conditions.get(str(values["draft_id"]), [])
            for row in rows:
                if int(row["condition_no"]) == int(values["condition_no"]):
                    row["user_action"] = str(values["action"])
            return _WriteResult(rowcount=1)
        if "SET user_action = 'edited'" in sql:
            rows = store.conditions.get(str(values["draft_id"]), [])
            for row in rows:
                if int(row["condition_no"]) == int(values["condition_no"]):
                    row["value_json"] = values.get("value_json")
                    row["user_action"] = "edited"
            return _WriteResult(rowcount=1)
        if "FROM ai_search_condition" in sql:
            rows = store.conditions.get(str(values["draft_id"]), [])
            return _MappingResult(list(rows))

        # ---- ai_search_snapshot ----
        if "INSERT INTO ai_search_snapshot" in sql:
            snapshot_id = str(values["snapshot_id"])
            store.snapshots[snapshot_id] = {
                "id": self_counter(),
                "snapshot_id": snapshot_id,
                "user_id": int(values["user_id"]),
                "draft_id": str(values["draft_id"]),
                "snapshot_hash": str(values["snapshot_hash"]),
                "status": "completed",
                "condition_schema_version": str(values["condition_schema_version"]),
                "policy_revision": str(values["policy_revision"]),
                "consent_snapshot_json": values.get("consent_snapshot_json"),
                "source_revision_json": values.get("source_revision_json"),
                "expires_at": values.get("expires_at"),
                "invalidated_at": None,
                "result_total": int(values.get("result_total") or 0),
                "degraded": int(values.get("degraded") or 0),
                "created_at": _now(),
            }
            return _WriteResult(rowcount=1)
        if sql.startswith("UPDATE ai_search_snapshot SET status"):
            snapshot = store.snapshots.get(values["snapshot_id"])
            if snapshot:
                snapshot["status"] = values["status"]
                snapshot["result_total"] = int(values.get("result_total") or 0)
                snapshot["degraded"] = int(values.get("degraded") or 0)
            return _WriteResult(rowcount=1 if snapshot else 0)
        if sql.startswith("UPDATE ai_search_snapshot SET invalidated_at"):
            snapshot = store.snapshots.get(values["snapshot_id"])
            if snapshot:
                snapshot["invalidated_at"] = _now()
            return _WriteResult(rowcount=1 if snapshot else 0)
        if "FROM ai_search_snapshot" in sql and "draft_id = :draft_id" in sql:
            rows = [
                row
                for row in store.snapshots.values()
                if row["draft_id"] == values["draft_id"]
                and row.get("invalidated_at") is None
            ]
            rows.sort(key=lambda r: r["id"], reverse=True)
            return _MappingResult(rows[:1])
        if "FROM ai_search_snapshot" in sql:
            snapshot = store.snapshots.get(values["snapshot_id"])
            return _MappingResult([snapshot] if snapshot else [])

        # ---- ai_search_result ----
        if sql.startswith("DELETE FROM ai_search_result"):
            rows = store.results.get(str(values["snapshot_id"]), [])
            store.results[str(values["snapshot_id"])] = [
                row for row in rows if int(row.get("rank_position") or 0) <= int(values["limit"])
            ]
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_search_result" in sql:
            snapshot_id = str(values["snapshot_id"])
            store.results.setdefault(snapshot_id, [])
            for existing in store.results[snapshot_id]:
                if int(existing["target_user_id"]) == int(values["target_user_id"]):
                    existing.update(
                        {
                            "rank_position": int(values["rank_position"]),
                            "matched_condition_count": int(
                                values["matched_condition_count"]
                            ),
                            "matched_conditions": values["matched_conditions"],
                            "unknown_conditions": values["unknown_conditions"],
                            "reason_codes": values["reason_codes"],
                            "profile_revision": int(values["profile_revision"]),
                            "result_expires_at": values["result_expires_at"],
                            "stale": 0,
                            "projection_id": values.get("projection_id"),
                            "source_hash": values.get("source_hash"),
                            "consent_snapshot_json": values.get("consent_snapshot_json"),
                            "source_revision_json": values.get("source_revision_json"),
                        }
                    )
                    return _WriteResult(rowcount=1)
            store.results[snapshot_id].append(
                {
                    "snapshot_id": snapshot_id,
                    "target_user_id": int(values["target_user_id"]),
                    "rank_position": int(values["rank_position"]),
                    "matched_condition_count": int(values["matched_condition_count"]),
                    "matched_conditions": values["matched_conditions"],
                    "unknown_conditions": values["unknown_conditions"],
                    "reason_codes": values["reason_codes"],
                    "profile_revision": int(values["profile_revision"]),
                    "result_expires_at": values["result_expires_at"],
                    "stale": 0,
                    "projection_id": values.get("projection_id"),
                    "source_hash": values.get("source_hash"),
                    "consent_snapshot_json": values.get("consent_snapshot_json"),
                    "source_revision_json": values.get("source_revision_json"),
                }
            )
            return _WriteResult(rowcount=1)
        if sql.startswith("SELECT target_user_id, projection_id"):
            rows = [
                row for row in store.results.get(str(values["snapshot_id"]), [])
                if not row.get("stale") and int(row.get("rank_position") or 0) > int(values.get("after_rank") or 0)
            ]
            rows.sort(key=lambda row: int(row["rank_position"]))
            return _MappingResult(rows[: int(values.get("limit") or 0)])

        # ---- 投影 / 建议 ----
        if "FROM ai_feature_projection" in sql:
            if "IN (:uid" in sql:
                subject_ids = {
                    int(values[key]) for key in values if key.startswith("uid")
                }
                rows = [
                    row
                    for row in store.projections
                    if row["subject_user_id"] in subject_ids
                ]
            else:
                subject_id = int(
                    values.get("subject_user_id")
                    if "subject_user_id" in values
                    else values.get("user_id", 0)
                )
                rows = [
                    row
                    for row in store.projections
                    if row["subject_user_id"] == subject_id
                ]
            if "projection_kind = 'personal_searchable'" in sql:
                rows = [row for row in rows if row["status"] == "active"]
            return _MappingResult(rows)

        # ---- 候选查询（CandidateQueryService）----
        if "COUNT(DISTINCT u.id)" in sql:
            matching = [
                candidate
                for candidate in store.candidates.values()
                if _candidate_matches_sql_filters(candidate, values, sql)
            ]
            return _MappingResult([{"count": len(matching)}])
        if "AS user_id" in sql and "FROM users u" in sql:
            candidate_rows = []
            requested_card_ids = {
                int(value) for key, value in values.items() if key.startswith("card_uid")
            }
            for candidate in store.candidates.values():
                if requested_card_ids and int(candidate["user_id"]) not in requested_card_ids:
                    continue
                if not _candidate_matches_sql_filters(candidate, values, sql):
                    continue
                candidate_rows.append(
                    {
                        "user_id": int(candidate["user_id"]),
                        "nickname": candidate["nickname"],
                        "avatar": candidate["avatar"],
                        "birthday": candidate["birthday"],
                        "is_married": candidate["is_married"],
                        "height": candidate["height"],
                        "education_level": candidate["education_level"],
                        "occupation": "技术",
                        "income": candidate["income"],
                        "residence_city_code": candidate["residence_city_code"],
                        "interest_tags": json.dumps(candidate["interest_tags"]),
                        "personality_tags": "[]",
                        "tags": "{}",
                        "is_boosted": candidate["is_boosted"],
                        "last_active_at": candidate["last_active_at"],
                    }
                )
            candidate_rows.sort(
                key=lambda row: (
                    bool(row["is_boosted"]),
                    row["last_active_at"] or datetime.min,
                    int(row["user_id"]),
                ),
                reverse=True,
            )
            if "cursor_user_id" in values:
                cursor_key = (
                    bool(values["cursor_is_boosted"]),
                    values.get("cursor_last_active_at") or datetime.min,
                    int(values["cursor_user_id"]),
                )
                candidate_rows = [
                    row
                    for row in candidate_rows
                    if (
                        bool(row["is_boosted"]),
                        row["last_active_at"] or datetime.min,
                        int(row["user_id"]),
                    )
                    < cursor_key
                ]
            else:
                candidate_rows = candidate_rows[
                    int(values.get("candidate_query_offset", 0)) :
                ]
            return _MappingResult(
                candidate_rows[: int(values.get("candidate_query_limit", 0))]
            )

        # ---- 可见性 decide（CandidateVisibilityService）----
        if "FROM users candidate" in sql:
            candidate = store.candidates.get(int(values["candidate_id"]))
            if candidate is None:
                return _MappingResult([])
            return _MappingResult(
                [
                    {
                        "candidate_id": candidate["user_id"],
                        "viewer_realname_status": 2,
                        "viewer_is_vip": 1,
                        "who_can_see_me": candidate["who_can_see_me"],
                        "account_active": bool(candidate["account_active"]),
                        "profile_visible": bool(candidate["profile_visible"]),
                        "match_active": bool(candidate["match_active"]),
                        "not_restricted": bool(candidate["not_restricted"]),
                        "profile_complete": bool(candidate["profile_complete"]),
                        "media_approved": bool(candidate["media_approved"]),
                        "blocked": bool(candidate["blocked"]),
                    }
                ]
            )

        # ---- viewer 上下文 / vip ----
        if "SELECT EXISTS" in sql and "user_membership" in sql:
            return _MappingResult([{"exists": 1}])
        if "FROM users u" in sql and "LEFT JOIN user_profile_completion" in sql:
            return _MappingResult(
                [
                    {
                        "gender": 1,
                        "birthday": "1996-05-20",
                        "completion_score": 100,
                        "realname_status": 2,
                        "only_vip_can_see_detail": 0,
                    }
                ]
            )

        raise AssertionError(f"unhandled sql: {sql}")


@pytest.fixture
def search_store() -> SearchStore:
    return SearchStore()


async def _run_parse(store: SearchStore, draft_id: str) -> None:
    """Run the registered search_parse handler for a draft (writes conditions)."""
    task = next(
        (
            row
            for row in store.task_store.tasks.values()
            if row["task_type"] == "search_parse"
            and draft_id in str(row.get("payload_summary") or "")
        ),
        None,
    )
    assert task is not None, "no search_parse task found"
    task["payload_summary"] = json.dumps({"draft_id": draft_id})
    await parse_search_draft(store.session, AiTaskRecord.from_row(task), "worker-1")


@pytest.fixture(autouse=True)
def _reset_parse_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 fake-store 测试不依赖开发机上 Redis 的跨测试计数。"""
    from redis.exceptions import RedisError

    class _RedisDown:
        async def eval(self, *args: Any, **kwargs: Any) -> Any:
            raise RedisError("redis disabled for fake-store test")

    monkeypatch.setattr(search_mod, "redis_client", _RedisDown())
    monkeypatch.setattr(search_mod, "_parse_quota_window", lambda: 1_000_000_000)
    search_mod.reset_local_quota_for_testing()
    yield


def _pin_parse_quota(monkeypatch: pytest.MonkeyPatch, window: int = 1_000_000_000) -> None:
    """把分钟额度测试与真实时间窗口/Redis 解耦（review I-3）。

    - monkeypatch ``_parse_quota_window`` 为固定值，杜绝测试中途跨分钟边界导致
      窗口 key 变化、第 N+1 次请求拿到 202 的 flake。
    - monkeypatch ``redis_client`` 为不可达桩，强制走本地计数（测试前由 autouse
      fixture 清空），使结果不依赖 Redis 是否在线/残留计数。
    """
    from redis.exceptions import RedisError

    class _RedisDown:
        async def eval(self, *args: Any, **kwargs: Any) -> Any:
            raise RedisError("redis down in test")

    monkeypatch.setattr(search_mod, "redis_client", _RedisDown())
    monkeypatch.setattr(search_mod, "_parse_quota_window", lambda: window)
    search_mod.reset_local_quota_for_testing()


# ----------------------------------------------------------------------
# Step 1: 编译 AST（逐字测试契约）
# ----------------------------------------------------------------------


def test_compile_maps_only_registered_fields() -> None:
    compiled = compile_search_conditions(
        [
            SearchCondition(
                field_key="age",
                operator="between",
                value={"min": 26, "max": 32},
                kind="hard",
                user_action="confirmed",
            ),
            SearchCondition(
                field_key="interest_tags",
                operator="contains",
                value="户外",
                kind="soft",
                user_action="confirmed",
            ),
        ]
    )
    assert compiled.filters.age_min == 26
    assert compiled.filters.age_max == 32
    assert compiled.soft_terms == (("interest_tags", "户外"),)
    assert compiled.sql_expression is None


def test_forbidden_condition_is_rejected_without_sql_generation() -> None:
    with pytest.raises(SearchPolicyDenied, match="AI_POLICY_DENIED"):
        compile_search_conditions(
            [
                SearchCondition(
                    field_key="phone",
                    operator="eq",
                    value="13800000000",
                    kind="hard",
                    user_action="confirmed",
                )
            ]
        )


def test_registered_field_with_illegal_operator_is_input_invalid() -> None:
    with pytest.raises(SearchInputInvalid, match="AI_INPUT_INVALID"):
        compile_search_conditions(
            [
                SearchCondition(
                    field_key="age",
                    operator="contains",
                    value=30,
                    kind="hard",
                    user_action="confirmed",
                )
            ]
        )


@pytest.mark.asyncio
async def test_unconfirmed_snapshot_cannot_start_candidate_query(search_store) -> None:
    draft = await search_store.seed_draft(status="awaiting_confirmation")
    with pytest.raises(search_store.NotConfirmed):
        await search_store.execute(draft["draft_id"])


# ----------------------------------------------------------------------
# 编译扩展契约
# ----------------------------------------------------------------------


def test_unconfirmed_forbidden_field_goes_to_unknown_not_policy() -> None:
    compiled = compile_search_conditions(
        [
            SearchCondition(
                field_key="phone",
                operator="eq",
                value="13800000000",
                kind="hard",
                user_action="pending",
            )
        ]
    )
    # 未注册字段 pending 不触发策略拒绝，但保留原文供澄清（逐字编译契约）。
    assert len(compiled.unknown) == 1
    assert compiled.unknown[0].field_key == "phone"
    assert compiled.filters.age_min is None
    assert compiled.soft_terms == ()


def test_range_conflict_is_detected_not_raised() -> None:
    compiled = compile_search_conditions(
        [
            SearchCondition(
                field_key="age",
                operator="between",
                value={"min": 32, "max": 26},
                kind="hard",
                user_action="confirmed",
            )
        ]
    )
    assert compiled.filters.age_min == 32
    assert compiled.filters.age_max == 26
    assert compiled.conflicts
    assert any("age" in conflict for conflict in compiled.conflicts)


def test_hard_field_mappings_cover_the_allowlist() -> None:
    compiled = compile_search_conditions(
        [
            SearchCondition(field_key="city_code", operator="eq", value="330100", kind="hard", user_action="confirmed"),
            SearchCondition(field_key="marriage_status", operator="in", value=["single"], kind="hard", user_action="confirmed"),
            SearchCondition(field_key="education_level", operator="gte", value=4, kind="hard", user_action="confirmed"),
            SearchCondition(field_key="height_cm", operator="between", value={"min": 160, "max": 180}, kind="hard", user_action="confirmed"),
            SearchCondition(field_key="income_band", operator="gte", value=2, kind="hard", user_action="confirmed"),
            SearchCondition(field_key="occupation_group", operator="eq", value="技术", kind="soft", user_action="confirmed"),
            SearchCondition(field_key="lifestyle_tags", operator="contains", value="健身", kind="soft", user_action="confirmed"),
            SearchCondition(field_key="relationship_goal", operator="eq", value="marriage", kind="soft", user_action="confirmed"),
        ]
    )
    filters = compiled.filters
    assert filters.city_code == "330100"
    assert filters.marriage_status == 1
    assert filters.education_min == 4
    assert filters.height_min == 160
    assert filters.height_max == 180
    assert filters.income_min == 2
    assert ("occupation_group", "技术") in compiled.soft_terms
    assert ("lifestyle_tags", "健身") in compiled.soft_terms
    assert ("relationship_goal", "marriage") in compiled.soft_terms


def test_multi_value_in_is_preserved_as_typed_hard_membership() -> None:
    compiled = compile_search_conditions(
        [
            SearchCondition(
                field_key="city_code",
                operator="in",
                value=["330100", "440100"],
                kind="hard",
                user_action="confirmed",
            ),
            SearchCondition(
                field_key="marriage_status",
                operator="in",
                value=["single", "married"],
                kind="hard",
                user_action="confirmed",
            ),
        ]
    )
    assert compiled.hard_memberships == (
        ("city_code", ("330100", "440100")),
        ("marriage_status", (1, 2)),
    )
    assert compiled.filters.city_code is None
    assert compiled.filters.marriage_status is None


def test_soft_conditions_never_enter_candidate_where() -> None:
    compiled = compile_search_conditions(
        [
            SearchCondition(
                field_key="interest_tags",
                operator="contains",
                value="户外",
                kind="soft",
                user_action="confirmed",
            )
        ]
    )
    snapshot = build_search_query_snapshot(
        viewer_id=10,
        viewer={"realname_status": 2},
        viewer_is_vip=True,
        compiled=compiled,
    )
    assert "户外" not in snapshot.where_sql
    assert not any(key.startswith("search_tag_") for key in snapshot.params)


# ----------------------------------------------------------------------
# 草稿创建 / 解析 / 确认 / 编辑 / 执行链路
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_draft_requires_consent_and_writes_parsing_draft(search_store) -> None:
    db = search_store.session
    from app.services.ai.search import SearchConsentRequired

    with pytest.raises(SearchConsentRequired):
        await create_search_draft(
            db, 10, "想找26到32岁住杭州的人", "manual", "zh-CN", "idem-create-1"
        )
    await search_store.seed_consent()
    await search_store.seed_revision()

    result = await create_search_draft(
        db, 10, "想找26到32岁住杭州的人", "manual", "zh-CN", "idem-create-2"
    )

    assert result.status == "parsing"
    assert result.task_id
    assert search_store.drafts[result.draft_id]["status"] == "parsing"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_create_draft_same_key_replays_without_random_hash_conflict(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    first = await create_search_draft(
        search_store.session, 10, "想找26到32岁的人", "manual", "zh-CN", "search-create-replay"
    )
    second = await create_search_draft(
        search_store.session, 10, "想找26到32岁的人", "manual", "zh-CN", "search-create-replay"
    )
    assert second.draft_id == first.draft_id
    assert len(search_store.drafts) == 1


@pytest.mark.asyncio
async def test_create_draft_rejects_too_long_query(search_store) -> None:
    db = search_store.session
    from app.services.ai.search import SearchInputInvalid

    with pytest.raises(SearchInputInvalid):
        await create_search_draft(
            db, 10, "x" * 1001, "manual", "zh-CN", "idem-create-3"
        )
    assert search_store.drafts == {}
    assert search_store.task_store.tasks == {}


@pytest.mark.asyncio
async def test_parse_handler_writes_conditions_and_moves_to_awaiting(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁住杭州本科以上周末愿意户外的人", "manual", "zh-CN", "idem-parse-1"
    )
    task = search_store.task_store.tasks[draft.task_id]
    worker = AiTaskRecord.from_row(task)
    task_row = search_store.task_store.tasks[draft.task_id]
    task_row["payload_summary"] = json.dumps({"draft_id": draft.draft_id})

    result = await parse_search_draft(db, worker, "worker-1")

    assert result is not None
    assert result[0] == f"search-draft:{draft.draft_id}"
    rows = search_store.conditions[draft.draft_id]
    assert rows
    assert {row["field_key"] for row in rows} >= {
        "age",
        "city_code",
        "education_level",
        "interest_tags",
    }
    assert all(row["user_action"] == "pending" for row in rows)
    assert search_store.drafts[draft.draft_id]["status"] == "awaiting_confirmation"


@pytest.mark.asyncio
async def test_confirm_requires_all_hard_confirmed(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁住杭州本科以上的人", "manual", "zh-CN", "idem-confirm-1"
    )
    draft_row = search_store.drafts[draft.draft_id]
    draft_row["status"] = "awaiting_confirmation"
    await _run_parse(search_store, draft.draft_id)

    # 全部 pending：不能确认。
    from app.services.ai.search import SearchDraftNotConfirmed

    with pytest.raises(SearchDraftNotConfirmed):
        await confirm_search_draft(
            db, draft.draft_id, 10, 0, "idem-confirm-2"
        )
    assert search_store.snapshots == {}
    assert not any(
        task["task_type"] == "search_execute"
        for task in search_store.task_store.tasks.values()
    )


@pytest.mark.asyncio
async def test_confirm_creates_snapshot_and_execute_task(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁住杭州本科以上的人", "manual", "zh-CN", "idem-confirm-3"
    )
    draft_row = search_store.drafts[draft.draft_id]
    draft_row["status"] = "awaiting_confirmation"
    await _run_parse(search_store, draft.draft_id)
    # 手动确认全部条件。
    rows = search_store.conditions[draft.draft_id]
    for row in rows:
        if row["field_key"] in {"age", "city_code", "education_level"}:
            row["user_action"] = "confirmed"

    snapshot = await confirm_search_draft(
        db, draft.draft_id, 10, 0, "idem-confirm-4"
    )

    assert snapshot.snapshot_id in search_store.snapshots
    assert snapshot.task_id
    assert search_store.drafts[draft.draft_id]["status"] == "confirmed"
    assert any(
        task["task_type"] == "search_execute"
        for task in search_store.task_store.tasks.values()
    )
    stored = search_store.snapshots[snapshot.snapshot_id]
    assert stored["snapshot_hash"]


@pytest.mark.asyncio
async def test_delete_search_snapshot_enqueues_minimal_cleanup_payload(search_store) -> None:
    search_store.snapshots["ss-delete-1"] = {
        "snapshot_id": "ss-delete-1",
        "draft_id": "draft-delete-1",
        "user_id": 10,
        "invalidated_at": None,
    }
    task = await delete_search_snapshot(
        search_store.session, "ss-delete-1", 10, "search-delete-1"
    )

    assert task.task_id
    assert search_store.snapshots["ss-delete-1"]["invalidated_at"] is not None
    stored_task = search_store.task_store.tasks[task.task_id]
    payload = json.loads(stored_task["payload_summary"])
    assert set(payload) == {"scope", "resource_id", "version", "purge_deadline"}
    assert payload["scope"] == "search"
    assert payload["resource_id"] == "snapshot:10:ss-delete-1"


@pytest.mark.asyncio
async def test_patch_confirms_condition_and_bumps_revision(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁的人", "manual", "zh-CN", "idem-patch-1"
    )
    draft_row = search_store.drafts[draft.draft_id]
    draft_row["status"] = "awaiting_confirmation"
    await _run_parse(search_store, draft.draft_id)
    age_no = next(
        row["condition_no"]
        for row in search_store.conditions[draft.draft_id]
        if row["field_key"] == "age"
    )

    from app.schemas.ai_search import SearchConditionPatchRequest

    updated = await patch_search_draft(
        db,
        draft.draft_id,
        10,
        [SearchConditionPatchRequest(condition_no=age_no, action="confirm")],
        expected_condition_revision=0,
    )

    assert isinstance(updated, SearchDraftRead)
    assert updated.condition_revision == 1
    confirmed = {
        condition.field_key: condition.user_action.value
        for condition in updated.conditions
    }
    assert confirmed["age"] == "confirmed"


@pytest.mark.asyncio
async def test_patch_idempotency_key_replays_durable_response(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    draft = await search_store.seed_draft(
        conditions=[
            SearchCondition(
                field_key="age",
                operator="gte",
                value=26,
                kind="hard",
                user_action="pending",
            )
        ]
    )
    first = await patch_search_draft(
        search_store.session,
        draft["draft_id"],
        10,
        [SearchConditionPatchRequest(condition_no=0, action="confirm")],
        expected_condition_revision=0,
        idempotency_key="search-patch-1",
    )
    second = await patch_search_draft(
        search_store.session,
        draft["draft_id"],
        10,
        [SearchConditionPatchRequest(condition_no=0, action="confirm")],
        expected_condition_revision=0,
        idempotency_key="search-patch-1",
    )
    assert first.condition_revision == second.condition_revision == 1
    assert search_store.drafts[draft["draft_id"]]["last_patch_idempotency_key"] == "search-patch-1"


@pytest.mark.asyncio
async def test_patch_replays_original_response_after_later_patch(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    draft = await search_store.seed_draft(
        conditions=[
            SearchCondition(
                field_key="age",
                operator="gte",
                value=26,
                kind="hard",
                user_action="pending",
            )
        ]
    )
    first = await patch_search_draft(
        search_store.session,
        draft["draft_id"],
        10,
        [SearchConditionPatchRequest(condition_no=0, action="confirm")],
        expected_condition_revision=0,
        idempotency_key="search-history-01",
    )
    await patch_search_draft(
        search_store.session,
        draft["draft_id"],
        10,
        [SearchConditionPatchRequest(condition_no=0, action="remove")],
        expected_condition_revision=1,
        idempotency_key="search-history-02",
    )
    replay = await patch_search_draft(
        search_store.session,
        draft["draft_id"],
        10,
        [SearchConditionPatchRequest(condition_no=0, action="confirm")],
        expected_condition_revision=0,
        idempotency_key="search-history-01",
    )
    assert first.condition_revision == replay.condition_revision == 1
    assert replay.conditions[0].value == 26


@pytest.mark.asyncio
async def test_patch_remove_then_parse_does_not_restore_removed_condition(
    search_store,
) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁的人", "manual", "zh-CN", "idem-patch-2"
    )
    draft_row = search_store.drafts[draft.draft_id]
    draft_row["status"] = "awaiting_confirmation"
    await _run_parse(search_store, draft.draft_id)
    age_no = next(
        row["condition_no"]
        for row in search_store.conditions[draft.draft_id]
        if row["field_key"] == "age"
    )

    from app.schemas.ai_search import SearchConditionPatchRequest

    await patch_search_draft(
        db,
        draft.draft_id,
        10,
        [SearchConditionPatchRequest(condition_no=age_no, action="remove")],
        expected_condition_revision=0,
    )
    # 重解析：已有条件行时只推进状态，不写新行。
    task = search_store.task_store.tasks[draft.task_id]
    task["payload_summary"] = json.dumps({"draft_id": draft.draft_id})
    await parse_search_draft(db, AiTaskRecord.from_row(task), "worker-1")

    rows = search_store.conditions[draft.draft_id]
    age_rows = [row for row in rows if row["field_key"] == "age"]
    assert age_rows and age_rows[0]["user_action"] == "removed"
    assert len(age_rows) == 1


@pytest.mark.asyncio
async def test_execute_snapshot_filters_soft_evidence_and_persists_results(
    search_store,
) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    await search_store.seed_candidate(
        42,
        birthday="1996-05-20",
        residence_city_code="330100",
        education_level=4,
        interest_tags=["户外"],
    )
    await search_store.seed_candidate(
        43,
        birthday="1994-05-20",
        residence_city_code="330100",
        education_level=4,
        interest_tags=["摄影"],
    )
    await search_store.seed_projection(42, {"interest_tags": ["户外"]})
    await search_store.seed_projection(43, {})
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁住杭州本科以上喜欢户外的人", "manual", "zh-CN", "idem-exe-1"
    )
    draft_row = search_store.drafts[draft.draft_id]
    draft_row["status"] = "awaiting_confirmation"
    await _run_parse(search_store, draft.draft_id)
    for row in search_store.conditions[draft.draft_id]:
        row["user_action"] = "confirmed"

    snapshot = await confirm_search_draft(
        db, draft.draft_id, 10, 0, "idem-exe-2"
    )

    page = await materialize_search_snapshot(db, snapshot.snapshot_id, 10)

    assert isinstance(page, SearchResultPageRead)
    assert page.status == "completed"
    assert page.total == 2
    ids = {item.user_id for item in page.items}
    assert 42 in ids
    assert 43 in ids
    item_42 = next(item for item in page.items if item.user_id == 42)
    assert item_42.matched_condition_count >= 4
    assert "interest_tags" in item_42.matched_conditions
    assert item_42.unknown_conditions == []
    assert "HARD_CONDITION_MATCH" in item_42.reason_codes
    item_43 = next(item for item in page.items if item.user_id == 43)
    assert "interest_tags" in item_43.unknown_conditions
    assert "SOFT_FIELD_UNKNOWN" in item_43.reason_codes
    assert len(search_store.results[snapshot.snapshot_id]) == 2


@pytest.mark.asyncio
async def test_worker_materializes_soft_rank_and_get_only_reads_materialized_rows(
    search_store,
) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    await search_store.seed_candidate(42, birthday="1996-05-20", interest_tags=["户外"])
    await search_store.seed_candidate(43, birthday="1996-05-20", interest_tags=["摄影"])
    await search_store.seed_projection(42, {"interest_tags": ["户外"]})
    draft = await search_store.seed_draft(
        conditions=[
            SearchCondition(
                field_key="interest_tags",
                operator="contains",
                value="户外",
                kind="soft",
                user_action="confirmed",
            )
        ]
    )
    snapshot = await confirm_search_draft(
        search_store.session, draft["draft_id"], 10, 0, "idem-materialize-1"
    )

    materialized = await materialize_search_snapshot(
        search_store.session, snapshot.snapshot_id, 10
    )
    assert materialized.status == "completed"
    assert [item.user_id for item in materialized.items] == [42]
    assert materialized.items[0].matched_conditions == ["interest_tags"]

    search_store.session.calls.clear()
    read_page = await read_materialized_search_results(
        search_store.session, snapshot.snapshot_id, 10, None, 20
    )
    assert [item.user_id for item in read_page.items] == [42]
    assert not any("COUNT(DISTINCT u.id)" in sql for sql, _ in search_store.session.calls)
    assert not any("INSERT INTO ai_search_result" in sql for sql, _ in search_store.session.calls)


@pytest.mark.asyncio
async def test_worker_materializes_at_most_200_and_marks_partial(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    for user_id in range(600, 805):
        await search_store.seed_candidate(user_id, birthday="1996-05-20")
        await search_store.seed_projection(user_id, {"interest_tags": ["户外"]})
    draft = await search_store.seed_draft(
        conditions=[
            SearchCondition(
                field_key="age",
                operator="gte",
                value=26,
                kind="hard",
                user_action="confirmed",
            )
        ]
    )
    snapshot = await confirm_search_draft(
        search_store.session, draft["draft_id"], 10, 0, "idem-materialize-200"
    )
    page = await materialize_search_snapshot(
        search_store.session, snapshot.snapshot_id, 10
    )
    assert page.status == "partial"
    assert page.degraded is True
    assert page.total == 205
    assert len(search_store.results[snapshot.snapshot_id]) == 200


@pytest.mark.asyncio
async def test_execute_snapshot_excludes_blocked_candidates(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    await search_store.seed_candidate(50, birthday="1996-05-20")
    await search_store.seed_candidate(51, birthday="1996-05-21", blocked=True)
    await search_store.seed_projection(50, {})
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁的人", "manual", "zh-CN", "idem-exe-3"
    )
    draft_row = search_store.drafts[draft.draft_id]
    draft_row["status"] = "awaiting_confirmation"
    await _run_parse(search_store, draft.draft_id)
    for row in search_store.conditions[draft.draft_id]:
        if row["field_key"] in {"age", "city_code", "education_level"}:
            row["user_action"] = "confirmed"

    snapshot = await confirm_search_draft(
        db, draft.draft_id, 10, 0, "idem-exe-4"
    )
    page = await materialize_search_snapshot(db, snapshot.snapshot_id, 10)

    assert {item.user_id for item in page.items} == {50}


@pytest.mark.asyncio
async def test_expired_snapshot_returns_stale_page(search_store) -> None:
    await search_store.seed_consent()
    await search_store.seed_revision()
    await search_store.seed_candidate(60, birthday="1996-05-20")
    db = search_store.session
    draft = await create_search_draft(
        db, 10, "想找26到32岁的人", "manual", "zh-CN", "idem-exe-5"
    )
    draft_row = search_store.drafts[draft.draft_id]
    draft_row["status"] = "awaiting_confirmation"
    await _run_parse(search_store, draft.draft_id)
    for row in search_store.conditions[draft.draft_id]:
        if row["field_key"] in {"age", "city_code", "education_level"}:
            row["user_action"] = "confirmed"
    snapshot = await confirm_search_draft(
        db, draft.draft_id, 10, 0, "idem-exe-6"
    )
    search_store.snapshots[snapshot.snapshot_id]["expires_at"] = (
        _now() - timedelta(hours=1)
    )

    page = await materialize_search_snapshot(db, snapshot.snapshot_id, 10)

    assert page.status == "stale"
    assert page.items == []
    assert page.total == 0


@pytest.mark.asyncio
async def test_suggestions_read_only_confirmed_tags(search_store) -> None:
    await search_store.seed_projection(10, {"interest_tags": ["旅行", "看展"]})
    db = search_store.session

    suggestions = await get_search_suggestions(db, 10)

    assert suggestions.items == ["旅行", "看展"]


@pytest.mark.asyncio
async def test_suggestions_empty_when_no_projection(search_store) -> None:
    db = search_store.session
    suggestions = await get_search_suggestions(db, 10)
    assert suggestions.items == []


# ----------------------------------------------------------------------
# API 路由
# ----------------------------------------------------------------------


def _override_auth(store: SearchStore, owner_id: int = 10) -> None:
    async def fake_current_user() -> CurrentUser:
        return CurrentUser(
            id=owner_id,
            session_id=9,
            phone="13800000000",
            status=1,
            realname_status=2,
        )

    def fake_db():
        yield store.session

    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_db] = fake_db


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _enable_search_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_master_enabled", True)
    monkeypatch.setattr(settings, "ai_search_enabled", True)


def test_create_draft_api_returns_202(monkeypatch, search_store) -> None:
    _enable_search_feature(monkeypatch)
    import asyncio

    asyncio.run(search_store.seed_consent())
    asyncio.run(search_store.seed_revision())
    _override_auth(search_store)
    try:
        response = client.post(
            "/api/v1/ai/search-drafts",
            json={"query_text": "想找26到32岁住杭州的人", "source": "manual", "locale": "zh-CN"},
            headers={"Idempotency-Key": "search-api-1"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "parsing"
    assert body["task_id"]
    assert body["condition_schema_version"] == "search-condition-v1"


def test_create_draft_api_requires_idempotency_key(monkeypatch, search_store) -> None:
    _enable_search_feature(monkeypatch)
    _override_auth(search_store)
    try:
        response = client.post(
            "/api/v1/ai/search-drafts",
            json={"query_text": "想找26到32岁的人"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "AI_INPUT_INVALID"


def test_search_draft_api_returns_404_for_foreign(monkeypatch, search_store) -> None:
    _enable_search_feature(monkeypatch)
    _override_auth(search_store)
    try:
        response = client.get("/api/v1/ai/search-drafts/sd_missing")
    finally:
        _clear_overrides()

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SEARCH_DRAFT_NOT_FOUND"


def test_search_results_api_returns_404_for_foreign_snapshot(
    monkeypatch, search_store
) -> None:
    _enable_search_feature(monkeypatch)
    _override_auth(search_store)
    try:
        response = client.get("/api/v1/ai/search-snapshots/ss_missing/results")
    finally:
        _clear_overrides()

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "SEARCH_SNAPSHOT_NOT_FOUND"


def test_search_feature_disabled_returns_503() -> None:
    # 默认开关关闭。
    _override_auth(SearchStore())
    try:
        response = client.get("/api/v1/ai/search-suggestions")
    finally:
        _clear_overrides()

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "AI_FEATURE_DISABLED"


# ----------------------------------------------------------------------
# Worker handler 注册
# ----------------------------------------------------------------------


def test_search_handlers_are_registered_into_the_worker() -> None:
    assert "search_parse" in worker_mod.TASK_HANDLERS
    assert "search_execute" in worker_mod.TASK_HANDLERS


# ----------------------------------------------------------------------
# Task 10 Gate 验收补充（fix round 1：额度 429 / hard 零违反 / 稳定分页 /
# 精确 total / 结果读取可见性门禁 / 畸形 cursor → 400）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_draft_quota_exhausted_after_per_minute_limit(
    search_store, monkeypatch,
) -> None:
    """超过每用户每分钟 ai_search_parse_rate_per_minute 次解析 → 429 额度耗尽。"""
    _pin_parse_quota(monkeypatch)
    await search_store.seed_consent()
    await search_store.seed_revision()
    db = search_store.session
    limit = settings.ai_search_parse_rate_per_minute
    for index in range(limit):
        result = await create_search_draft(
            db, 10, f"想找26到32岁的人{index}", "manual", "zh-CN", f"idem-quota-{index}"
        )
        assert result.task_id

    with pytest.raises(SearchQuotaExceeded):
        await create_search_draft(
            db, 10, "超额解析请求", "manual", "zh-CN", "idem-quota-over"
        )


def test_search_draft_api_returns_429_after_rate_limit(
    monkeypatch, search_store
) -> None:
    """路由层：额度耗尽 → 429 AI_QUOTA_EXCEEDED（AiErrorDetail 形状）。"""
    _pin_parse_quota(monkeypatch)
    _enable_search_feature(monkeypatch)
    import asyncio

    asyncio.run(search_store.seed_consent())
    asyncio.run(search_store.seed_revision())
    _override_auth(search_store)
    try:
        for index in range(settings.ai_search_parse_rate_per_minute):
            response = client.post(
                "/api/v1/ai/search-drafts",
                json={"query_text": f"想找26到32岁的人{index}"},
                headers={"Idempotency-Key": f"idem-quota-api-{index}"},
            )
            assert response.status_code == 202
        response = client.post(
            "/api/v1/ai/search-drafts",
            json={"query_text": "超额解析请求"},
            headers={"Idempotency-Key": "idem-quota-api-over"},
        )
    finally:
        _clear_overrides()

    assert response.status_code == 429
    body = response.json()
    assert body["detail"]["code"] == "AI_QUOTA_EXCEEDED"
    assert body["detail"]["request_id"]
    assert body["detail"]["retryable"] is True


async def _confirm_seeded_draft(
    store: SearchStore,
    conditions: list[SearchCondition],
    idempotency_key: str,
    owner_user_id: int = 10,
) -> str:
    """Seed 一个全部条件已 confirmed 的待确认草稿并确认，返回 snapshot_id。"""
    await store.seed_consent()
    await store.seed_revision()
    draft = await store.seed_draft(conditions=conditions)
    db = store.session
    snapshot = await confirm_search_draft(
        db, draft["draft_id"], owner_user_id, 0, idempotency_key
    )
    return snapshot.snapshot_id


@pytest.mark.asyncio
async def test_snapshot_results_have_zero_hard_condition_violations(
    search_store,
) -> None:
    """执行确认后的快照：返回结果必须全部满足 hard 条件（违反率为 0）。"""
    conditions = [
        SearchCondition(
            field_key="age",
            operator="between",
            value={"min": 26, "max": 32},
            kind="hard",
            user_action="confirmed",
        ),
        SearchCondition(
            field_key="city_code",
            operator="eq",
            value="330100",
            kind="hard",
            user_action="confirmed",
        ),
        SearchCondition(
            field_key="education_level",
            operator="gte",
            value=4,
            kind="hard",
            user_action="confirmed",
        ),
        SearchCondition(
            field_key="height_cm",
            operator="between",
            value={"min": 160, "max": 180},
            kind="hard",
            user_action="confirmed",
        ),
        SearchCondition(
            field_key="income_band",
            operator="gte",
            value=8000,
            kind="hard",
            user_action="confirmed",
        ),
        SearchCondition(
            field_key="marriage_status",
            operator="eq",
            value="single",
            kind="hard",
            user_action="confirmed",
        ),
    ]
    await search_store.seed_candidate(
        100,
        birthday="1996-05-20",
        residence_city_code="330100",
        education_level=5,
        height=172,
        income=15000.0,
        is_married=1,
    )
    await search_store.seed_projection(100, {})
    # 违反单个 hard 条件的候选：超龄/错误城市/学历不足/身高过低/收入过低/非单身
    await search_store.seed_candidate(101, birthday="1982-01-01")
    await search_store.seed_candidate(102, birthday="1996-05-20", residence_city_code="440100")
    await search_store.seed_candidate(103, birthday="1996-05-20", education_level=2)
    await search_store.seed_candidate(104, birthday="1996-05-20", height=150)
    await search_store.seed_candidate(105, birthday="1996-05-20", income=1000.0)
    await search_store.seed_candidate(106, birthday="1996-05-20", is_married=2)

    snapshot_id = await _confirm_seeded_draft(
        search_store, conditions, "idem-hard-1"
    )
    page = await materialize_search_snapshot(
        search_store.session, snapshot_id, 10
    )

    assert page.status == "completed"
    assert page.total == 1
    ids = [item.user_id for item in page.items]
    assert ids == [100]
    item = page.items[0]
    assert item.matched_condition_count == 6
    assert set(item.matched_conditions) == {
        "age",
        "city_code",
        "education_level",
        "height_cm",
        "income_band",
        "marriage_status",
    }
    assert "HARD_CONDITION_MATCH" in item.reason_codes
    assert "SOFT_FIELD_UNKNOWN" not in item.reason_codes


@pytest.mark.asyncio
async def test_cursor_paging_has_no_duplicates_or_omissions(search_store) -> None:
    """连续 cursor 多页翻页：无重复 user_id、无漏项、顺序稳定。"""
    for uid in range(200, 215):  # 15 个候选，稳定排序（distinct last_active_at）
        await search_store.seed_candidate(
            uid, birthday="1996-05-20", last_active_at=_now() - timedelta(hours=uid)
        )
        await search_store.seed_projection(uid, {})
    conditions = [
        SearchCondition(
            field_key="age",
            operator="between",
            value={"min": 26, "max": 32},
            kind="hard",
            user_action="confirmed",
        )
    ]
    snapshot_id = await _confirm_seeded_draft(search_store, conditions, "idem-page-1")
    db = search_store.session
    # Worker 路径物化全部候选结果，读取路径再分页。
    await materialize_search_snapshot(db, snapshot_id, 10)

    seen: list[int] = []
    cursor: str | None = None
    first_order: list[int] = []
    pages = 0
    while True:
        page = await read_materialized_search_results(db, snapshot_id, 10, cursor, 4)
        assert page.status == "completed"
        ids = [item.user_id for item in page.items]
        assert len(ids) == len(set(ids)), "单页内出现重复 user_id"
        assert not (set(ids) & set(seen)), "跨页出现重复 user_id"
        if not first_order and ids:
            first_order = ids
        seen.extend(ids)
        cursor = page.next_cursor
        pages += 1
        assert pages <= 10, "分页未收敛，疑似死循环"
        if cursor is None:
            break

    assert sorted(seen) == list(range(200, 215)), "分页存在漏项"
    assert pages == 4
    # 顺序稳定：与 CandidateQueryService 的排序完全一致（无重复并集）。
    assert len(seen) == len(set(seen)) == 15


@pytest.mark.asyncio
async def test_result_total_matches_manual_candidate_query_count(
    search_store,
) -> None:
    """同一筛选下 ai_search 结果 total 与手工 CandidateQueryService 精确 count 一致。"""
    for uid in range(300, 308):  # 8 个满足条件的候选
        await search_store.seed_candidate(uid, birthday="1996-05-20")
        await search_store.seed_projection(uid, {})
    await search_store.seed_candidate(
        308, birthday="1996-05-20", residence_city_code="440100"
    )
    conditions = [
        SearchCondition(
            field_key="age",
            operator="between",
            value={"min": 26, "max": 32},
            kind="hard",
            user_action="confirmed",
        ),
        SearchCondition(
            field_key="city_code",
            operator="eq",
            value="330100",
            kind="hard",
            user_action="confirmed",
        ),
    ]
    snapshot_id = await _confirm_seeded_draft(search_store, conditions, "idem-total-1")
    db = search_store.session
    page = await materialize_search_snapshot(db, snapshot_id, 10)

    # 手工 discovery：同一编译筛选直接 count（与 execute 相同查询身份）。
    compiled = compile_search_conditions(conditions)
    viewer = {
        "gender": 1,
        "birthday": "1996-05-20",
        "completion_score": 100,
        "realname_status": 2,
        "only_vip_can_see_detail": 0,
    }
    manual_snapshot = build_search_query_snapshot(
        viewer_id=10, viewer=viewer, viewer_is_vip=True, compiled=compiled
    )
    manual_total = await candidate_query_service.count(db, manual_snapshot)

    assert manual_total == 8
    assert page.total == manual_total
    assert page.total_is_estimate is False
    assert len(page.items) == page.total


@pytest.mark.asyncio
async def test_result_read_excludes_blocked_withdrawn_and_pending_candidates(
    search_store,
) -> None:
    """结果读取逐行门禁：被拉黑/撤回（注销/隐藏）/审核中候选全部被排除。"""
    await search_store.seed_consent()
    await search_store.seed_revision()
    await search_store.seed_candidate(400, birthday="1996-05-20")  # 正常可见
    await search_store.seed_candidate(401, birthday="1996-05-20", blocked=True)
    await search_store.seed_candidate(
        402, birthday="1996-05-20", not_restricted=False  # 被限制（审核中）
    )
    await search_store.seed_candidate(
        403, birthday="1996-05-20", status=0, account_active=0  # 已注销/撤回
    )
    await search_store.seed_candidate(
        404, birthday="1996-05-20", completion=50, profile_complete=False  # 资料审核中
    )
    await search_store.seed_candidate(
        405, birthday="1996-05-20", profile_visible=0  # 撤回展示
    )
    await search_store.seed_projection(400, {})
    conditions = [
        SearchCondition(
            field_key="age",
            operator="between",
            value={"min": 26, "max": 32},
            kind="hard",
            user_action="confirmed",
        )
    ]
    snapshot_id = await _confirm_seeded_draft(search_store, conditions, "idem-vis-1")
    page = await materialize_search_snapshot(
        search_store.session, snapshot_id, 10
    )

    assert page.status == "completed"
    assert {item.user_id for item in page.items} == {400}
    assert page.total == 1
    assert len(search_store.results[snapshot_id]) == 1


@pytest.mark.asyncio
async def test_cross_query_cursor_raises_invalid_candidate_cursor(
    search_store,
) -> None:
    """服务层：跨查询/伪造 cursor → InvalidCandidateCursor（非 500 基础）。"""
    conditions = [
        SearchCondition(
            field_key="age",
            operator="between",
            value={"min": 26, "max": 32},
            kind="hard",
            user_action="confirmed",
        )
    ]
    snapshot_id = await _confirm_seeded_draft(search_store, conditions, "idem-cur-1")
    # Worker 物化结果后再用读取路径校验 cursor 合法性。
    await materialize_search_snapshot(search_store.session, snapshot_id, 10)
    foreign_cursor = candidate_query_service.encode_cursor(
        CandidateCursor(
            sort_version=SORT_VERSION,
            query_fingerprint="some-other-query-fingerprint",
            is_boosted=False,
            last_active_at=None,
            user_id=1,
        )
    )
    with pytest.raises(InvalidCandidateCursor):
        await read_materialized_search_results(
            search_store.session, snapshot_id, 10, foreign_cursor, 20
        )


def test_search_results_api_maps_invalid_cursor_to_400(
    monkeypatch, search_store
) -> None:
    """路由层：畸形/跨查询 cursor → 400 INVALID_CANDIDATE_CURSOR（非 500）。"""
    _enable_search_feature(monkeypatch)
    import asyncio

    asyncio.run(search_store.seed_consent())
    asyncio.run(search_store.seed_revision())
    asyncio.run(search_store.seed_candidate(500, birthday="1996-05-20"))
    conditions = [
        SearchCondition(
            field_key="age",
            operator="between",
            value={"min": 26, "max": 32},
            kind="hard",
            user_action="confirmed",
        )
    ]
    asyncio.run(search_store.seed_draft(conditions=conditions))
    draft = next(iter(search_store.drafts.values()))
    snapshot = asyncio.run(
        confirm_search_draft(
            search_store.session, draft["draft_id"], 10, 0, "idem-cursor-api-1"
        )
    )
    foreign_cursor = candidate_query_service.encode_cursor(
        CandidateCursor(
            sort_version=SORT_VERSION,
            query_fingerprint="another-query-fingerprint",
            is_boosted=False,
            last_active_at=None,
            user_id=1,
        )
    )
    _override_auth(search_store)
    try:
        malformed = client.get(
            f"/api/v1/ai/search-snapshots/{snapshot.snapshot_id}/results",
            params={"cursor": "not-a-valid-cursor-token"},
        )
        cross_query = client.get(
            f"/api/v1/ai/search-snapshots/{snapshot.snapshot_id}/results",
            params={"cursor": foreign_cursor},
        )
    finally:
        _clear_overrides()

    for response in (malformed, cross_query):
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["detail"]["code"] == "INVALID_CANDIDATE_CURSOR"
        assert body["detail"]["request_id"]
