"""Task 8 acceptance contract: draft confirmation, confirmed-only publish,
subject isolation, immutable history/restore and two-phase deletion.

The four Step 1 tests are mirrored verbatim from the task brief.  The
``profile_store`` fixture extends the Task 7 in-memory fake store
(``tests.test_ai_profile_sessions.ProfileStore``) with the Task 8 surface:
``seed_draft`` (fields with ``confirmed``/``suggested`` status, draft revision),
``publish``, ``confirm_all``, ``published_field_keys``, ``personal_fact_keys``,
``preference_keys``, ``delete_profile``, ``readable_ai_results``,
``has_outbox_event``, ``seed_published_profile`` and the ``DraftVersionConflict``
exception class.  The fake session routes the Task 8 service SQL onto the
in-memory tables (ai_profile_draft/_field, ai_profile_revision/_field,
user_revision_state, derivation_outbox, ai_task, projections/search/compat),
so publish/delete semantics can be exercised without a real database.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import pytest

from app.schemas.ai_profile import (
    ProfileDraftFieldPatchRequest,
    ProfileFieldPatchAction,
    ProfileSubject,
)
from app.services.ai.profile import (
    AIInputError,
    DraftStatusConflict,
    DraftVersionConflict,
    ProfileDraftNotFound,
    confirm_profile_draft,
    delete_ai_profile,
    delete_ai_profile_field,
    list_profile_revisions,
    load_owned_draft,
    publish_profile_draft,
    restore_profile_revision,
)
from app.services.ai.tasks import TaskError
from tests.test_ai_profile_sessions import (
    FakeProfileSession,
    _MappingResult,
    _now,
    _WriteResult,
)
from tests.test_ai_profile_sessions import (
    ProfileStore as Task7ProfileStore,
)

_PUBLISHABLE_CONFIRMED_FIELDS = [
    {"field_key": "interest_tags", "value": ["看展"], "status": "confirmed"},
    {"field_key": "city_code", "value": "330100", "status": "confirmed"},
    {"field_key": "occupation_group", "value": "technology", "status": "confirmed"},
    {"field_key": "education_level", "value": 4, "status": "confirmed"},
    {"field_key": "height_cm", "value": 175, "status": "confirmed"},
]


class _OneMappingResult(_MappingResult):
    """Adds ``one()``/``one_or_none()`` for ``increment_revision_and_enqueue``."""

    def one(self) -> dict[str, Any]:
        assert self._rows, "no row for .one()"
        return self._rows[0]

    def one_or_none(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class PublishFakeSession(FakeProfileSession):
    """Routes the Task 8 service SQL; everything else falls back to Task 7."""

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _MappingResult | _WriteResult:
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        store = self._store

        # ---- ai_profile_revision / ai_profile_revision_field ----
        if sql.startswith("UPDATE ai_profile_revision SET source_revision_json"):
            revision = store.revisions.get(int(values["revision_id"]))
            if revision is not None:
                revision["source_revision_json"] = values["source_revision_json"]
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_profile_revision_field" in sql:
            store.insert_revision_field(values)
            return _WriteResult(rowcount=1)
        if "INSERT INTO ai_profile_revision" in sql:
            store.insert_revision(values)
            return _WriteResult(rowcount=1)
        if "FROM ai_profile_revision_field" in sql:
            return _MappingResult(
                store.revision_fields_for(str(values["revision_id"]))
            )
        if "FROM ai_profile_revision" in sql and "MAX(revision_no)" in sql:
            matching = [
                int(row["revision_no"])
                for row in store.revisions.values()
                if row["user_id"] == int(values["user_id"])
                and row["subject"] == str(values["subject"])
            ]
            return _MappingResult(
                [{"next_no": (max(matching) + 1) if matching else 1}]
            )
        if "FROM ai_profile_revision" in sql and "GROUP BY" in sql:
            return _MappingResult(store.history_revisions(int(values["user_id"])))
        if "FROM ai_profile_revision" in sql and "COUNT(*)" in sql:
            return _MappingResult(
                [{"total": store.count_revisions(int(values["user_id"]))}]
            )
        if "FROM ai_profile_revision" in sql and "WHERE id = :revision_id" in sql:
            return _MappingResult([store.revisions.get(int(values["revision_id"]))])
        if "FROM ai_profile_revision" in sql:
            return _MappingResult(
                [
                    row
                    for row in store.revisions.values()
                    if row["user_id"] == int(values["user_id"])
                    and row["subject"] == str(values["subject"])
                ]
            )
        if "SELECT id FROM ai_profile_revision" in sql:
            matching = [
                row["id"]
                for row in store.revisions.values()
                if row["user_id"] == int(values["user_id"])
                and row["subject"] == str(values["subject"])
                and row["revision_no"] == int(values["revision_no"])
            ]
            return _MappingResult([{"id": max(matching)}] if matching else [])

        # ---- ai_profile_draft / ai_profile_draft_field ----
        if "INSERT INTO ai_profile_draft_field" in sql:
            return await super().execute(statement, params)
        if "INSERT INTO ai_profile_draft " in sql:
            # restore 路径插入的草稿 session_id 为 NULL；注册到 drafts_by_id 供读取。
            store.insert_restored_draft(values)
            return _WriteResult(rowcount=1)
        # 注意：批量删除 SQL 内嵌 "FROM ai_profile_draft"，必须先处理 UPDATE。
        if sql.startswith("UPDATE ai_profile_draft_field"):
            store.apply_draft_field_update(sql, values)
            return _WriteResult(rowcount=1)
        if sql.startswith("UPDATE ai_profile_draft"):
            store.apply_draft_update(sql, values)
            return _WriteResult(rowcount=1)
        if "FROM ai_profile_draft_field" in sql:
            return _MappingResult(
                store.fields_for_draft(str(values["draft_id"]))
            )
        if "FROM ai_profile_draft" in sql:
            return _MappingResult([store.drafts_by_id.get(str(values["draft_id"]))])
        if "UPDATE ai_profile_session" in sql and "status = 'published'" in sql:
            session = store.sessions.get(str(values["session_id"]))
            if session is not None:
                session["status"] = "published"
                session["active_status"] = 0
                session["ended_at"] = _now()
                session["updated_at"] = _now()
            return _WriteResult(rowcount=1)

        # ---- revision state + outbox ----
        if "INSERT INTO user_revision_state" in sql:
            store.upsert_revision(values, sql)
            return _WriteResult(rowcount=1)
        if "FROM user_revision_state" in sql and "FOR UPDATE" in sql:
            return _OneMappingResult(
                [store.revision_rows.get(int(values["user_id"]))] or []
            )
        if "INSERT INTO derivation_outbox" in sql:
            store.insert_outbox(values)
            return _WriteResult(rowcount=1)

        # ---- deletion propagation targets (sync invisibility) ----
        if "UPDATE ai_feature_projection" in sql:
            store.apply_projection_update(values)
            return _WriteResult(rowcount=1)
        if "UPDATE ai_search_result" in sql:
            store.apply_search_result_update(values)
            return _WriteResult(rowcount=1)
        if "UPDATE ai_compatibility_snapshot" in sql:
            store.apply_compat_update(values)
            return _WriteResult(rowcount=1)
        # 代际 fence 标记（本 store 不建模搜索草稿/快照集合，仅记录调用）。
        if "UPDATE ai_search_snapshot" in sql:
            store.deletion_marks.append(("snapshot_invalidated", dict(values)))
            return _WriteResult(rowcount=1)
        if "UPDATE ai_search_draft" in sql:
            store.deletion_marks.append(("draft_invalidated", dict(values)))
            return _WriteResult(rowcount=1)
        if "UPDATE ai_consent_grant" in sql:
            store.apply_consent_update(values)
            return _WriteResult(rowcount=1)
        if "FROM ai_consent_grant" in sql and "version" not in values:
            # restore 路径的 _load_latest_consent：无 version 参数，取该 scope 最新有效授权。
            rows = [
                r
                for r in store.consents
                if int(r["user_id"]) == int(values["user_id"])
                and r["scope"] == str(values["scope"])
                and r.get("revoked_at") is None
            ]
            rows.sort(key=lambda r: r["granted_at"], reverse=True)
            return _MappingResult([rows[0]] if rows else [])

        return await super().execute(statement, params)


class ProfileStore(Task7ProfileStore):
    """Task 8 in-memory profile store (extends the Task 7 fixture surface)."""

    NotFound = Task7ProfileStore.NotFound
    Stale = Task7ProfileStore.Stale
    DraftVersionConflict = DraftVersionConflict

    def __init__(self) -> None:
        super().__init__()
        self.drafts_by_id: dict[str, dict[str, Any]] = {}
        self.revisions: dict[int, dict[str, Any]] = {}
        self.revision_fields: list[dict[str, Any]] = []
        self.outbox_events: list[dict[str, Any]] = []
        self.projections: list[dict[str, Any]] = []
        self.search_results: list[dict[str, Any]] = []
        self.compat_snapshots: list[dict[str, Any]] = []
        self.deletion_marks: list[tuple[str, dict[str, Any]]] = []
        self._next_revision_id = 1
        self.session = PublishFakeSession(self)
        self.db = self.session
        # 预置 draft-1：实际 revision 为 3，供
        # test_stale_expected_revision_is_rejected 直接 publish(expected=1) 触发冲突。
        self.drafts_by_id["draft-1"] = self._make_draft_row(
            draft_id="draft-1", user_id=10, subject="personal", revision=3
        )
        self.draft_fields.append(
            self._make_draft_field_row(
                draft_id="draft-1",
                field_key="interest_tags",
                subject="personal",
                value=["看展"],
                status="confirmed",
            )
        )

    # ---- seed helpers ---------------------------------------------------

    def _make_draft_row(
        self,
        *,
        draft_id: str,
        user_id: int,
        subject: str,
        revision: int,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        return {
            "draft_id": draft_id,
            "user_id": int(user_id),
            "subject": subject,
            "session_id": session_id,
            "status": "draft",
            "expected_revision": int(revision),
            "consent_snapshot_json": None,
            "policy_revision": "ai-policy-2026-08-07-v1",
            "prompt_version": None,
            "schema_version": "profile-extract-v1",
            "published_revision_id": None,
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }

    def _make_draft_field_row(
        self,
        *,
        draft_id: str,
        field_key: str,
        subject: str,
        value: Any,
        status: str,
    ) -> dict[str, Any]:
        now = _now()
        return {
            "draft_id": draft_id,
            "field_key": field_key,
            "subject": subject,
            "value": value,
            "value_json": json.dumps(value, ensure_ascii=False),
            "display_value": ", ".join(value) if isinstance(value, list) else str(value),
            "source_type": "user_answer",
            "source_turn_ids": json.dumps(["turn-001"]),
            "source_span": "原始回答片段",
            "confidence": 0.9,
            "visibility": "self",
            "consent_scope": "profile_text_extract",
            "schema_version": "profile-extract-v1",
            "prompt_version": "profile-extract-prompt-v1",
            "content_hash": f"hash-{field_key}-{draft_id}",
            "confirmation_status": status,
            "created_at": now,
            "updated_at": now,
        }

    async def seed_draft(
        self,
        owner_user_id: int = 10,
        subject: str = "personal",
        fields: list[dict[str, Any]] | None = None,
        revision: int = 0,
        draft_id: str | None = None,
        status: str = "draft",
    ) -> dict[str, Any]:
        """Seed an editable draft row with field candidates and a revision."""
        now = _now()
        did = draft_id or f"dr_{uuid.uuid4().hex[:12]}"
        row = {
            "draft_id": did,
            "user_id": int(owner_user_id),
            "subject": subject,
            "session_id": None,
            "status": status,
            "expected_revision": int(revision),
            "consent_snapshot_json": None,
            "policy_revision": "ai-policy-2026-08-07-v1",
            "prompt_version": None,
            "schema_version": "profile-extract-v1",
            "published_revision_id": None,
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.drafts_by_id[did] = row
        if not any(d["draft_id"] == did for d in self.drafts):
            self.drafts.append(row)
        seed_fields = fields if fields is not None else [
            {"field_key": "city_code", "value": "330100", "status": "suggested"}
        ]
        for field in seed_fields:
            self.draft_fields.append(
                self._make_draft_field_row(
                    draft_id=did,
                    field_key=str(field["field_key"]),
                    subject=subject,
                    value=field.get("value"),
                    status=str(field.get("status") or "suggested"),
                )
            )
        self.revision_rows.setdefault(
            int(owner_user_id),
            {
                "profile_revision": 0,
                "preference_revision": 0,
                "privacy_revision": 0,
                "relationship_revision": 0,
                "policy_revision": 0,
            },
        )
        return dict(row)

    async def seed_published_profile(
        self, owner_user_id: int = 10, subject: str = "personal"
    ) -> None:
        """Seed one active projection + one compat snapshot (readable results)."""
        self.projections.append(
            {
                "id": 1,
                "subject_user_id": int(owner_user_id),
                "projection_kind": (
                    "personal_searchable"
                    if subject == "personal"
                    else "ideal_partner_preference"
                ),
                "source_hash": "hash-seed",
                "projection_version": "profile-extract-v1",
                "fields_json": None,
                "source_revision_json": None,
                "privacy_revision": 0,
                "consent_snapshot_json": {},
                "status": "active",
                "invalidated_at": None,
                "purge_after": None,
                "created_at": _now(),
                "updated_at": _now(),
            }
        )
        self.compat_snapshots.append(
            {
                "id": 1,
                "snapshot_id": f"cp_{uuid.uuid4().hex[:12]}",
                "viewer_user_id": int(owner_user_id),
                "target_user_id": int(owner_user_id) + 1,
                "status": "ready",
                "invalidated_at": None,
                "purge_after": None,
                "created_at": _now(),
            }
        )

    # ---- internal mutations for the fake session -----------------------

    def insert_revision(self, params: dict[str, Any]) -> dict[str, Any]:
        rid = self._next_revision_id
        self._next_revision_id += 1
        row = {
            "id": rid,
            "user_id": int(params["user_id"]),
            "subject": str(params["subject"]),
            "revision_no": int(params["revision_no"]),
            "draft_id": str(params["draft_id"]),
            "source_revision_json": params.get("source_revision_json"),
            "policy_revision": str(params["policy_revision"]),
            "published_by": int(params["published_by"] or 0),
            "published_at": params.get("published_at"),
            "created_at": params.get("created_at"),
        }
        self.revisions[rid] = row
        return row

    def insert_revision_field(self, params: dict[str, Any]) -> None:
        value = json.loads(params["value_json"]) if params.get("value_json") else None
        self.revision_fields.append(
            {
                "id": len(self.revision_fields) + 1,
                "revision_id": int(params["revision_id"]),
                "field_key": str(params["field_key"]),
                "subject": str(params["subject"]),
                "value": value,
                "value_json": params.get("value_json"),
                "display_value": params.get("display_value"),
                "confidence": float(params.get("confidence") or 0.0),
                "source_type": str(params.get("source_type") or "user_answer"),
                "source_turn_ids": params.get("source_turn_ids"),
                "source_span": params.get("source_span"),
                "content_hash": str(params.get("content_hash") or ""),
                "schema_version": str(params.get("schema_version") or "profile-extract-v1"),
                "prompt_version": params.get("prompt_version"),
                "created_at": params.get("created_at"),
            }
        )

    def insert_restored_draft(self, params: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        row = {
            "draft_id": str(params["draft_id"]),
            "user_id": int(params["user_id"]),
            "subject": str(params["subject"]),
            "session_id": None,
            "status": "draft",
            "expected_revision": 0,
            "consent_snapshot_json": params.get("consent_snapshot_json"),
            "policy_revision": str(params["policy_revision"]),
            "prompt_version": str(params["prompt_version"]),
            "schema_version": str(params["schema_version"]),
            "published_revision_id": None,
            "expires_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.drafts_by_id[row["draft_id"]] = row
        self.drafts.append(row)
        return row

    def revision_fields_for(self, revision_id: int) -> list[dict[str, Any]]:
        return [
            dict(f)
            for f in self.revision_fields
            if f["revision_id"] == int(revision_id)
        ]

    def history_revisions(self, user_id: int) -> list[dict[str, Any]]:
        rows = [
            {
                "id": row["id"],
                "subject": row["subject"],
                "revision_no": row["revision_no"],
                "policy_revision": row["policy_revision"],
                "published_at": row["published_at"],
                "field_count": sum(
                    1
                    for f in self.revision_fields
                    if f["revision_id"] == row["id"]
                ),
            }
            for row in self.revisions.values()
            if row["user_id"] == user_id
        ]
        rows.sort(key=lambda row: row["id"], reverse=True)
        return rows

    def count_revisions(self, user_id: int) -> int:
        return sum(1 for row in self.revisions.values() if row["user_id"] == user_id)

    def apply_draft_update(self, sql: str, params: dict[str, Any]) -> bool:
        if "SET status = 'deleted'" in sql and "draft_id" not in params:
            # 整主体删除：按 user_id + subject 批量标记草稿 deleted（不可见）。
            changed = False
            for row in self.drafts_by_id.values():
                if (
                    int(row["user_id"]) == int(params["user_id"])
                    and row["subject"] == str(params["subject"])
                    and row["status"] != "deleted"
                ):
                    row["status"] = "deleted"
                    row["updated_at"] = _now()
                    changed = True
            return changed
        row = self.drafts_by_id.get(str(params["draft_id"]))
        if row is None:
            return False
        if "SET status = 'published'" in sql:
            row["status"] = "published"
            row["published_revision_id"] = params.get("revision_id")
            row["expected_revision"] = int(row["expected_revision"]) + 1
        elif "SET status = 'deleted'" in sql:
            row["status"] = "deleted"
        elif "expected_revision = :revision" in sql:
            row["expected_revision"] = int(params["revision"])
        elif "last_operation_idempotency_key" in sql:
            row["last_operation_idempotency_key"] = params.get("key")
            row["last_operation_request_digest"] = params.get("request_digest")
            row["last_operation_response_json"] = params.get("response_json")
        else:
            raise AssertionError(f"unhandled draft update: {sql}")
        row["updated_at"] = _now()
        return True

    def apply_draft_field_update(self, sql: str, params: dict[str, Any]) -> bool:
        if "confirmation_status = 'deleted'" in sql:
            for field in self.draft_fields:
                if (
                    field["field_key"] == str(params["field_key"])
                    and field["subject"] == str(params["subject"])
                    and field["confirmation_status"] != "deleted"
                ):
                    field["confirmation_status"] = "deleted"
                    field["updated_at"] = _now()
            return True
        draft_id = str(params["draft_id"])
        field_key = str(params["field_key"])
        for field in self.draft_fields:
            if field["draft_id"] == draft_id and field["field_key"] == field_key:
                if "confirmation_status = :status" in sql:
                    field["confirmation_status"] = str(params["status"])
                elif "SET value_json = :value_json" in sql:
                    field["value"] = json.loads(params["value_json"])
                    field["value_json"] = params["value_json"]
                    field["display_value"] = params.get("display_value")
                    field["content_hash"] = params.get("content_hash")
                    field["confirmation_status"] = "confirmed"
                field["updated_at"] = _now()
                return True
        return False

    def upsert_revision(self, params: dict[str, Any], sql: str) -> None:
        user_id = int(params["user_id"])
        row = self.revision_rows.setdefault(
            user_id,
            {
                "profile_revision": 0,
                "preference_revision": 0,
                "privacy_revision": 0,
                "relationship_revision": 0,
                "policy_revision": 0,
            },
        )
        # INSERT ... ON DUPLICATE KEY UPDATE profile_revision = profile_revision + 1
        match = __import__("re").search(
            r"(\w+_revision) = \1 \+ 1", sql
        )
        column = match.group(1) if match else "profile_revision"
        row[column] = int(row.get(column) or 0) + 1

    def insert_outbox(self, params: dict[str, Any]) -> None:
        self.outbox_events.append(
            {
                "event_id": str(params["event_id"]),
                "aggregate_id": int(params["aggregate_id"]),
                "event_type": str(params["event_type"]),
                "changed_fields": params.get("changed_fields"),
                "source_revision_json": params.get("source_revision_json"),
                "payload_minimal": params.get("payload_minimal"),
                "priority": int(params.get("priority") or 50),
            }
        )

    def apply_projection_update(self, params: dict[str, Any]) -> bool:
        changed = False
        for row in self.projections:
            if (
                int(row["subject_user_id"]) == int(params["user_id"])
                and row["status"] == "active"
            ):
                row["status"] = "invalidated"
                row["invalidated_at"] = _now()
                row["purge_after"] = _now() + timedelta(days=30)
                changed = True
        return changed

    def apply_search_result_update(self, params: dict[str, Any]) -> bool:
        changed = False
        for row in self.search_results:
            if int(row["target_user_id"]) == int(params["user_id"]):
                row["stale"] = 1
                changed = True
        return changed

    def apply_compat_update(self, params: dict[str, Any]) -> bool:
        changed = False
        for row in self.compat_snapshots:
            if int(row["viewer_user_id"]) == int(params["user_id"]) or int(
                row["target_user_id"]
            ) == int(params["user_id"]):
                row["status"] = "blocked"
                row["invalidated_at"] = _now()
                row["purge_after"] = _now() + timedelta(days=30)
                changed = True
        return changed

    def apply_consent_update(self, params: dict[str, Any]) -> bool:
        changed = False
        for row in self.consents:
            if (
                int(row["user_id"]) == int(params["user_id"])
                and row["scope"] == str(params["scope"])
                and row.get("revoked_at") is None
            ):
                row["revoked_at"] = _now()
                row["revoke_reason"] = params.get("revoke_reason")
                changed = True
        return changed

    # ---- Task 8 fixture surface (brief semantics) ----------------------

    async def confirm_all(
        self, draft_id: str, owner_user_id: int = 10, expected_revision: int = 0
    ) -> dict[str, Any]:
        actions = [
            ProfileDraftFieldPatchRequest(
                field_key=field["field_key"],
                action=ProfileFieldPatchAction.CONFIRM,
                value=field.get("value"),
                expected_revision=expected_revision,
            )
            for field in self.fields_for_draft(draft_id)
        ]
        updated = await confirm_profile_draft(
            self.db, draft_id, owner_user_id, actions, expected_revision
        )
        return {
            "draft_id": updated.draft_id,
            "revision": updated.revision,
            "fields": updated.fields,
        }

    async def publish(
        self,
        draft_id: str,
        owner_user_id: int = 10,
        expected_revision: int = 0,
        idempotency_key: str | None = None,
    ) -> Any:
        key = idempotency_key or f"pub-{draft_id}-{expected_revision}"
        return await publish_profile_draft(
            self.db, draft_id, owner_user_id, expected_revision, key
        )

    async def published_field_keys(self, user_id: int, subject: str) -> list[str]:
        keys: list[str] = []
        for revision in self.revisions.values():
            if (
                int(revision["user_id"]) == int(user_id)
                and revision["subject"] == subject
            ):
                keys.extend(
                    f["field_key"]
                    for f in self.revision_fields_for(revision["id"])
                    if f["subject"] == subject
                )
        return keys

    async def personal_fact_keys(self, user_id: int) -> list[str]:
        return await self.published_field_keys(user_id, ProfileSubject.PERSONAL.value)

    async def preference_keys(self, user_id: int) -> list[str]:
        return await self.published_field_keys(user_id, ProfileSubject.IDEAL_PARTNER.value)

    async def delete_profile(
        self, user_id: int, subject: str, idempotency_key: str
    ) -> dict[str, Any]:
        task = await delete_ai_profile(
            self.db, user_id, ProfileSubject(subject), idempotency_key
        )
        return {"task_id": task.task_id, "status": task.status}

    async def delete_profile_field(
        self, user_id: int, subject: str, field_key: str, idempotency_key: str
    ) -> dict[str, Any]:
        task = await delete_ai_profile_field(
            self.db, user_id, ProfileSubject(subject), field_key, idempotency_key
        )
        return {"task_id": task.task_id, "status": task.status}

    async def readable_ai_results(self, user_id: int) -> list[dict[str, Any]]:
        readable: list[dict[str, Any]] = []
        readable.extend(
            row
            for row in self.projections
            if int(row["subject_user_id"]) == int(user_id) and row["status"] == "active"
        )
        readable.extend(
            row
            for row in self.search_results
            if int(row["target_user_id"]) == int(user_id) and int(row["stale"] or 0) == 0
        )
        readable.extend(
            row
            for row in self.compat_snapshots
            if (
                int(row["viewer_user_id"]) == int(user_id)
                or int(row["target_user_id"]) == int(user_id)
            )
            and row["status"] != "blocked"
        )
        return readable

    async def has_outbox_event(self, event_type: str, user_id: int) -> bool:
        return any(
            row["event_type"] == event_type
            and int(row["aggregate_id"]) == int(user_id)
            for row in self.outbox_events
        )

    async def find_task(self, task_id: str) -> dict[str, Any] | None:
        return await self.task_store.get(task_id)


@pytest.fixture
def profile_store() -> ProfileStore:
    return ProfileStore()


# ----------------------------------------------------------------------
# Step 1: 简报逐字测试
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_writes_confirmed_fields_only(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=_PUBLISHABLE_CONFIRMED_FIELDS + [
            {"field_key": "income", "value": "high", "status": "suggested"},
        ],
        revision=3,
    )
    await profile_store.publish(draft["draft_id"], owner_user_id=10, expected_revision=3)
    assert await profile_store.published_field_keys(10, "personal") == [
        "interest_tags",
        "city_code",
        "occupation_group",
        "education_level",
        "height_cm",
    ]


@pytest.mark.asyncio
async def test_ideal_partner_never_updates_personal_profile(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="ideal_partner",
        fields=_PUBLISHABLE_CONFIRMED_FIELDS,
        revision=1,
    )
    await profile_store.confirm_all(draft["draft_id"], owner_user_id=10, expected_revision=1)
    await profile_store.publish(draft["draft_id"], owner_user_id=10, expected_revision=2)
    assert await profile_store.personal_fact_keys(10) == []
    assert await profile_store.preference_keys(10) != []


@pytest.mark.asyncio
async def test_delete_hides_draft_and_derived_results_before_cleanup(profile_store) -> None:
    await profile_store.seed_published_profile(owner_user_id=10, subject="personal")
    task = await profile_store.delete_profile(10, "personal", "delete-key-01")
    assert task["status"] == "queued"
    assert await profile_store.readable_ai_results(10) == []
    assert await profile_store.has_outbox_event("ai_profile_deleted", 10)


@pytest.mark.asyncio
async def test_stale_expected_revision_is_rejected(profile_store) -> None:
    with pytest.raises(profile_store.DraftVersionConflict):
        await profile_store.publish("draft-1", owner_user_id=10, expected_revision=1)


# ----------------------------------------------------------------------
# 草稿状态守卫（审查 I-1）：deleted/published 终态草稿只读
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_rejects_deleted_draft(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "confirmed"}],
        revision=1,
        status="deleted",
    )
    with pytest.raises(DraftStatusConflict) as excinfo:
        await profile_store.publish(
            draft["draft_id"], owner_user_id=10, expected_revision=1
        )
    assert excinfo.value.code == "RESULT_STALE"
    assert excinfo.value.status_code == 409
    # 拒绝发布不写 revision、不递增版本向量、不写 outbox 事件。
    assert profile_store.count_revisions(10) == 0
    assert profile_store.revision_rows[10]["profile_revision"] == 0
    assert await profile_store.published_field_keys(10, "personal") == []
    assert not await profile_store.has_outbox_event("ai_profile_published", 10)


@pytest.mark.asyncio
async def test_publish_rejects_deleted_draft_even_with_stale_revision(profile_store) -> None:
    # 删除不递增草稿 expected_revision，旧 expected_revision 仍与行值一致；
    # 守卫必须在 ensure_revision 之前生效，否则会以原 revision 静默重新发布。
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "confirmed"}],
        revision=3,
        status="deleted",
    )
    with pytest.raises(DraftStatusConflict) as excinfo:
        await profile_store.publish(
            draft["draft_id"], owner_user_id=10, expected_revision=3
        )
    assert excinfo.value.code == "RESULT_STALE"
    assert profile_store.count_revisions(10) == 0


@pytest.mark.asyncio
async def test_publish_rejects_published_draft(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "confirmed"}],
        revision=1,
        status="published",
    )
    with pytest.raises(DraftStatusConflict):
        await profile_store.publish(
            draft["draft_id"], owner_user_id=10, expected_revision=1
        )


@pytest.mark.asyncio
async def test_confirm_rejects_deleted_draft(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "suggested"}],
        revision=1,
        status="deleted",
    )
    with pytest.raises(DraftStatusConflict) as excinfo:
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="interest_tags",
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=1,
                )
            ],
            expected_revision=1,
        )
    assert excinfo.value.code == "RESULT_STALE"
    assert excinfo.value.status_code == 409
    # 拒绝确认不递增草稿版本、不改字段状态。
    assert profile_store.drafts_by_id[draft["draft_id"]]["expected_revision"] == 1
    field = profile_store.fields_for_draft(draft["draft_id"])[0]
    assert field["confirmation_status"] == "suggested"


@pytest.mark.asyncio
async def test_confirm_rejects_published_draft(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "suggested"}],
        revision=1,
        status="published",
    )
    with pytest.raises(DraftStatusConflict):
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="interest_tags",
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=1,
                )
            ],
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_editable_draft_confirm_and_publish_are_unaffected(profile_store) -> None:
    # 正常 draft 草稿仍可 confirm → publish（守卫不干扰可编辑路径）。
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[
            {"field_key": "interest_tags", "value": ["看展"], "status": "suggested"},
            {"field_key": "city_code", "value": "330100", "status": "suggested"},
            {"field_key": "occupation_group", "value": "technology", "status": "suggested"},
            {"field_key": "education_level", "value": 4, "status": "suggested"},
            {"field_key": "height_cm", "value": 175, "status": "suggested"},
        ],
        revision=1,
    )
    updated = await confirm_profile_draft(
        profile_store.db,
        draft["draft_id"],
        10,
        [
            ProfileDraftFieldPatchRequest(
                field_key="interest_tags",
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=1,
            ),
            ProfileDraftFieldPatchRequest(
                field_key="city_code",
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=1,
            ),
            ProfileDraftFieldPatchRequest(
                field_key="occupation_group",
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=1,
            ),
            ProfileDraftFieldPatchRequest(
                field_key="education_level",
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=1,
            ),
            ProfileDraftFieldPatchRequest(
                field_key="height_cm",
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=1,
            ),
        ],
        expected_revision=1,
    )
    assert updated.revision == 2
    submission = await profile_store.publish(
        draft["draft_id"], owner_user_id=10, expected_revision=2
    )
    assert submission.replayed is False
    assert await profile_store.published_field_keys(10, "personal") == [
        "interest_tags",
        "city_code",
        "occupation_group",
        "education_level",
        "height_cm",
    ]


@pytest.mark.asyncio
async def test_confirm_patch_idempotency_replays_and_conflicts(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["鐪嬪睍"], "status": "suggested"}],
        revision=1,
    )
    action = ProfileDraftFieldPatchRequest(
        field_key="interest_tags",
        action=ProfileFieldPatchAction.CONFIRM,
        expected_revision=1,
    )
    first = await confirm_profile_draft(
        profile_store.db,
        draft["draft_id"],
        10,
        [action],
        expected_revision=1,
        idempotency_key="confirm-patch-01",
    )
    replay = await confirm_profile_draft(
        profile_store.db,
        draft["draft_id"],
        10,
        [action],
        expected_revision=1,
        idempotency_key="confirm-patch-01",
    )
    assert replay.revision == first.revision == 2
    conflicting = ProfileDraftFieldPatchRequest(
        field_key="interest_tags",
        action=ProfileFieldPatchAction.REJECT,
        expected_revision=1,
    )
    with pytest.raises(TaskError) as excinfo:
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [conflicting],
            expected_revision=1,
            idempotency_key="confirm-patch-01",
        )
    assert excinfo.value.code == "TASK_IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_confirm_patch_replays_original_response_after_later_patch(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["鐪嬪睍"], "status": "suggested"}],
        revision=1,
    )
    confirm = ProfileDraftFieldPatchRequest(
        field_key="interest_tags",
        action=ProfileFieldPatchAction.CONFIRM,
        expected_revision=1,
    )
    first = await confirm_profile_draft(
        profile_store.db, draft["draft_id"], 10, [confirm], 1, "confirm-history-01"
    )
    reject = ProfileDraftFieldPatchRequest(
        field_key="interest_tags",
        action=ProfileFieldPatchAction.REJECT,
        expected_revision=2,
    )
    await confirm_profile_draft(
        profile_store.db, draft["draft_id"], 10, [reject], 2, "confirm-history-02"
    )
    replay = await confirm_profile_draft(
        profile_store.db, draft["draft_id"], 10, [confirm], 1, "confirm-history-01"
    )
    assert first.revision == replay.revision == 2
    assert replay.fields[0].confirmation_status == "confirmed"


# ----------------------------------------------------------------------
# 发布：confirmed-only、空 confirmed、幂等回放、版本递增
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_requires_at_least_one_confirmed_field(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "suggested"}],
        revision=1,
    )
    with pytest.raises(AIInputError) as excinfo:
        await profile_store.publish(draft["draft_id"], owner_user_id=10, expected_revision=1)
    assert excinfo.value.code == "AI_INPUT_INVALID"
    assert "confirmed" in excinfo.value.message
    assert await profile_store.published_field_keys(10, "personal") == []


@pytest.mark.asyncio
async def test_publish_requires_at_least_five_confirmed_fields(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[
            {"field_key": "interest_tags", "value": ["看展"], "status": "confirmed"},
            {"field_key": "city_code", "value": "330100", "status": "confirmed"},
            {"field_key": "occupation_group", "value": "technology", "status": "confirmed"},
            {"field_key": "education_level", "value": 4, "status": "confirmed"},
        ],
        revision=1,
    )
    with pytest.raises(AIInputError) as excinfo:
        await profile_store.publish(draft["draft_id"], owner_user_id=10, expected_revision=1)
    assert excinfo.value.code == "AI_INPUT_INVALID"
    assert "5 confirmed" in excinfo.value.message
    assert await profile_store.published_field_keys(10, "personal") == []


@pytest.mark.asyncio
async def test_publish_same_key_replays_same_task_without_second_revision(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=list(_PUBLISHABLE_CONFIRMED_FIELDS),
        revision=1,
    )
    first = await profile_store.publish(
        draft["draft_id"], owner_user_id=10, expected_revision=1, idempotency_key="pub-key-001"
    )
    second = await profile_store.publish(
        draft["draft_id"], owner_user_id=10, expected_revision=1, idempotency_key="pub-key-001"
    )
    assert first.replayed is False
    assert second.replayed is True
    assert second.task_id == first.task_id
    assert first.revision is not None
    assert second.revision is None
    assert profile_store.count_revisions(10) == 1
    assert await profile_store.published_field_keys(10, "personal") == [
        "interest_tags",
        "city_code",
        "occupation_group",
        "education_level",
        "height_cm",
    ]


@pytest.mark.asyncio
async def test_publish_increments_only_the_subjects_revision(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=list(_PUBLISHABLE_CONFIRMED_FIELDS),
        revision=1,
    )
    before = dict(profile_store.revision_rows[10])
    await profile_store.publish(draft["draft_id"], owner_user_id=10, expected_revision=1)
    after = profile_store.revision_rows[10]
    assert after["profile_revision"] == before["profile_revision"] + 1
    assert after["preference_revision"] == before["preference_revision"]


@pytest.mark.asyncio
async def test_publish_pins_revision_consent_and_published_revision_on_projection_task(
    profile_store,
) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=list(_PUBLISHABLE_CONFIRMED_FIELDS),
        revision=1,
    )
    profile_store.drafts_by_id[draft["draft_id"]]["consent_snapshot_json"] = {
        "scope": "profile_text_extract",
        "version": "profile-text-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": "2026-08-11T00:00:00",
    }

    submission = await profile_store.publish(
        draft["draft_id"], owner_user_id=10, expected_revision=1
    )
    task = await profile_store.find_task(submission.task_id)
    assert task is not None
    assert json.loads(task["source_revision_json"]) == {
        "profile": 1,
        "preference": 0,
        "privacy": 0,
        "relationship": 0,
        "policy": 0,
    }
    assert json.loads(task["consent_snapshot_json"])["version"] == "profile-text-v1"
    payload = json.loads(task["payload_summary"])
    assert payload["published_revision_id"] == submission.revision.revision_id
    assert payload["source_revision"] == json.loads(task["source_revision_json"])
    event = next(
        event for event in profile_store.outbox_events
        if event["event_type"] == "ai_profile_published"
    )
    event_payload = json.loads(event["payload_minimal"])
    assert event_payload["published_revision_id"] == submission.revision.revision_id
    assert event_payload["subject"] == "personal"


# ----------------------------------------------------------------------
# 确认：乐观锁、replace 校验、delete 标记、reject
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_actions_reject_stale_expected_revision(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "suggested"}],
        revision=1,
    )
    with pytest.raises(DraftVersionConflict):
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="interest_tags",
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=1,
                )
            ],
            expected_revision=99,
        )


@pytest.mark.asyncio
async def test_confirm_action_itself_carries_old_revision(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "suggested"}],
        revision=1,
    )
    with pytest.raises(DraftVersionConflict):
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="interest_tags",
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=2,
                )
            ],
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_replace_revalidates_value_and_confirms(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "suggested"}],
        revision=1,
    )
    updated = await confirm_profile_draft(
        profile_store.db,
        draft["draft_id"],
        10,
        [
            ProfileDraftFieldPatchRequest(
                field_key="interest_tags",
                action=ProfileFieldPatchAction.REPLACE,
                value=["旅行", "登山"],
                expected_revision=1,
            )
        ],
        expected_revision=1,
    )
    assert updated.revision == 2
    field = next(f for f in updated.fields if f.field_key == "interest_tags")
    assert field.value == ["旅行", "登山"]
    assert field.confirmation_status == "confirmed"


@pytest.mark.asyncio
async def test_replace_rejects_blank_tag_list(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "suggested"}],
        revision=1,
    )
    with pytest.raises(AIInputError):
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="interest_tags",
                    action=ProfileFieldPatchAction.REPLACE,
                    value=[],
                    expected_revision=1,
                )
            ],
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_replace_rejects_invalid_enum_value(profile_store) -> None:
    """REPLACE 必须复用抽取边界的枚举校验，拒绝非法 marriage_status 值。"""
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "marriage_status", "value": "single", "status": "suggested"}],
        revision=1,
    )
    with pytest.raises(AIInputError):
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="marriage_status",
                    action=ProfileFieldPatchAction.REPLACE,
                    value="未婚",  # 中文标签不是合法枚举值
                    expected_revision=1,
                )
            ],
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_replace_rejects_out_of_range_integer(profile_store) -> None:
    """REPLACE 必须复用抽取边界的区间校验，拒绝越界 age。"""
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "age", "value": 25, "status": "suggested"}],
        revision=1,
    )
    with pytest.raises(AIInputError):
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="age",
                    action=ProfileFieldPatchAction.REPLACE,
                    value=999,
                    expected_revision=1,
                )
            ],
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_replace_rejects_non_integer_for_education_level(profile_store) -> None:
    """education_level 契约类型是 integer，REPLACE 不接受字符串标签。"""
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "education_level", "value": 4, "status": "suggested"}],
        revision=1,
    )
    with pytest.raises(AIInputError):
        await confirm_profile_draft(
            profile_store.db,
            draft["draft_id"],
            10,
            [
                ProfileDraftFieldPatchRequest(
                    field_key="education_level",
                    action=ProfileFieldPatchAction.REPLACE,
                    value="本科",
                    expected_revision=1,
                )
            ],
            expected_revision=1,
        )


@pytest.mark.asyncio
async def test_replace_accepts_valid_enum_value_and_normalizes(profile_store) -> None:
    """合法枚举值通过校验且写入归一化后的值。"""
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "marriage_status", "value": "single", "status": "suggested"}],
        revision=1,
    )
    updated = await confirm_profile_draft(
        profile_store.db,
        draft["draft_id"],
        10,
        [
            ProfileDraftFieldPatchRequest(
                field_key="marriage_status",
                action=ProfileFieldPatchAction.REPLACE,
                value="divorced",
                expected_revision=1,
            )
        ],
        expected_revision=1,
    )
    field = next(f for f in updated.fields if f.field_key == "marriage_status")
    assert field.value == "divorced"
    assert field.confirmation_status == "confirmed"


@pytest.mark.asyncio
async def test_delete_marks_field_invisible_and_reject_marks_rejected(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[
            {"field_key": "interest_tags", "value": ["看展"], "status": "suggested"},
            {"field_key": "income_band", "value": "high", "status": "suggested"},
        ],
        revision=1,
    )
    updated = await confirm_profile_draft(
        profile_store.db,
        draft["draft_id"],
        10,
        [
            ProfileDraftFieldPatchRequest(
                field_key="interest_tags",
                action=ProfileFieldPatchAction.DELETE,
                expected_revision=1,
            ),
            ProfileDraftFieldPatchRequest(
                field_key="income_band",
                action=ProfileFieldPatchAction.REJECT,
                expected_revision=1,
            ),
        ],
        expected_revision=1,
    )
    by_key = {f.field_key: f for f in updated.fields}
    assert by_key["interest_tags"].confirmation_status == "deleted"
    assert by_key["income_band"].confirmation_status == "rejected"


# ----------------------------------------------------------------------
# 历史：只读、恢复不改旧行、非本人不可见
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_lists_only_own_published_revisions(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=list(_PUBLISHABLE_CONFIRMED_FIELDS),
        revision=1,
    )
    await profile_store.publish(draft["draft_id"], owner_user_id=10, expected_revision=1)
    page = await list_profile_revisions(profile_store.db, 10)
    assert page.total == 1
    assert page.items[0].revision_no == 1
    assert page.items[0].field_count == 5
    foreign = await list_profile_revisions(profile_store.db, 11)
    assert foreign.total == 0
    assert foreign.items == []


@pytest.mark.asyncio
async def test_restore_creates_new_draft_without_touching_old_revision(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=list(_PUBLISHABLE_CONFIRMED_FIELDS),
        revision=1,
    )
    submission = await profile_store.publish(
        draft["draft_id"], owner_user_id=10, expected_revision=1
    )
    assert submission.revision is not None
    old_revision_id = submission.revision.revision_id
    restored = await restore_profile_revision(
        profile_store.db, old_revision_id, owner_user_id=10
    )
    assert restored.draft_id != draft["draft_id"]
    assert restored.revision == 0
    assert all(f.confirmation_status == "suggested" for f in restored.fields)
    assert restored.fields[0].source_span == "原始回答片段"
    # 旧 revision 行保持不变（只读）。
    assert old_revision_id in profile_store.revisions
    assert profile_store.count_revisions(10) == 1


@pytest.mark.asyncio
async def test_restore_rejects_foreign_revision(profile_store) -> None:
    from app.services.ai.profile import ProfileRevisionNotFound

    with pytest.raises(ProfileRevisionNotFound):
        await restore_profile_revision(profile_store.db, 999, owner_user_id=10)


@pytest.mark.asyncio
async def test_load_draft_rejects_foreign_owner(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10, subject="personal", revision=1
    )
    with pytest.raises(ProfileDraftNotFound):
        await load_owned_draft(profile_store.db, draft["draft_id"], owner_user_id=11)


# ----------------------------------------------------------------------
# 删除：字段级、幂等回放、ideal_partner 事件名
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_field_delete_hides_field_and_increments_subject_revision(profile_store) -> None:
    draft = await profile_store.seed_draft(
        owner_user_id=10,
        subject="personal",
        fields=[{"field_key": "interest_tags", "value": ["看展"], "status": "confirmed"}],
        revision=1,
    )
    before = dict(profile_store.revision_rows[10])
    task = await profile_store.delete_profile_field(
        10, "personal", "interest_tags", "field-delete-key-01"
    )
    assert task["status"] == "queued"
    after = profile_store.revision_rows[10]
    assert after["profile_revision"] == before["profile_revision"] + 1
    assert after["preference_revision"] == before["preference_revision"]
    assert await profile_store.has_outbox_event("ai_profile_field_deleted", 10)
    fields = profile_store.fields_for_draft(draft["draft_id"])
    assert all(f["confirmation_status"] == "deleted" for f in fields)


@pytest.mark.asyncio
async def test_repeated_delete_replays_same_cleanup_task(profile_store) -> None:
    await profile_store.seed_published_profile(owner_user_id=10, subject="personal")
    first = await profile_store.delete_profile(10, "personal", "delete-key-repeat")
    second = await profile_store.delete_profile(10, "personal", "delete-key-repeat")
    assert first["task_id"] == second["task_id"]
    assert first["status"] == "queued"
    # 只有一次删除事件（重复删除只回放同一 task，不重复写 outbox）。
    assert await profile_store.has_outbox_event("ai_profile_deleted", 10)
    assert sum(
        1
        for row in profile_store.outbox_events
        if row["event_type"] == "ai_profile_deleted"
    ) == 1


@pytest.mark.asyncio
async def test_ideal_partner_delete_writes_preference_event(profile_store) -> None:
    await profile_store.seed_published_profile(owner_user_id=10, subject="ideal_partner")
    task = await profile_store.delete_profile(10, "ideal_partner", "del-ip-key-01")
    assert task["status"] == "queued"
    assert await profile_store.has_outbox_event("ai_preference_deleted", 10)
    assert not await profile_store.has_outbox_event("ai_profile_deleted", 10)


@pytest.mark.asyncio
async def test_delete_queued_task_is_persisted(profile_store) -> None:
    await profile_store.seed_published_profile(owner_user_id=10, subject="personal")
    task = await profile_store.delete_profile(10, "personal", "delete-key-persist")
    row = await profile_store.find_task(task["task_id"])
    assert row is not None
    assert row["status"] == "queued"
    payload = json.loads(row["payload_summary"])
    assert set(payload) == {"scope", "resource_id", "version", "purge_deadline"}
    assert payload["scope"] == "profile"
    assert payload["resource_id"] == "profile:10:personal"
