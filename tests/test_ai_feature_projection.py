"""Task 9 acceptance contract: field allowlist, revision equality and
versioned feature projections.

The two Step 1 tests are mirrored verbatim from the task brief.  The remaining
tests pin the additional requirements: unsupported subjects are rejected,
non-allowlisted fields never enter a projection, ``ideal_partner_preference``
is ``self_only`` (never returnable as a candidate profile), the full revision
vector is persisted on write, old events cannot overwrite newer projections,
and the Task 8 placeholder cleanup handlers are replaced by real projection
invalidation driven by the derivation-outbox consumer loop.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.schemas.ai_common import ProjectionKind, ProjectionVisibility
from app.services.ai.features import (
    PROFILE_ALLOWLIST,
    ProjectionBuildError,
    build_feature_projection,
    build_projection_payload,
    invalidate_projection,
    projection_is_current,
)
from app.services.derivation_outbox import (
    CLEANUP_HANDLERS,
    run_cleanup_consumer_round,
)
from app.services.revisions import RevisionVector


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class _WriteResult:
    def __init__(self, *, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _MappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_MappingResult":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class ProjectionStore:
    """In-memory store backing the fake session for Task 9 SQL routing."""

    def __init__(self) -> None:
        self.revisions: dict[int, dict[str, Any]] = {}
        self.revision_fields: list[dict[str, Any]] = []
        self.consents: list[dict[str, Any]] = []
        self.projections: list[dict[str, Any]] = []
        self.search_results: list[dict[str, Any]] = []
        self.compat_snapshots: list[dict[str, Any]] = []
        self.revision_row: dict[str, Any] = {
            "profile_revision": 1,
            "preference_revision": 0,
            "privacy_revision": 0,
            "relationship_revision": 0,
            "policy_revision": 0,
        }
        self.receipts: list[tuple[str, str]] = []
        self._next_projection_id = 1

    # ---- seed helpers ---------------------------------------------------

    def seed_revision(
        self,
        user_id: int = 10,
        subject: str = "personal",
        revision_no: int = 1,
        revision_id: int | None = None,
        source_revision: RevisionVector | None = None,
    ) -> int:
        rid = revision_id or revision_no
        self.revisions[rid] = {
            "id": rid,
            "user_id": int(user_id),
            "subject": subject,
            "revision_no": int(revision_no),
            "policy_revision": "ai-policy-2026-08-07-v1",
            "source_revision_json": json.dumps(
                (source_revision or RevisionVector(profile=1)).as_dict(),
                ensure_ascii=False,
            ),
            "published_at": _now(),
        }
        return rid

    def seed_revision_field(
        self,
        revision_id: int,
        field_key: str,
        value: Any,
        subject: str = "personal",
    ) -> None:
        self.revision_fields.append(
            {
                "revision_id": int(revision_id),
                "field_key": field_key,
                "subject": subject,
                "value_json": json.dumps(value, ensure_ascii=False),
                "schema_version": "profile-extract-v1",
            }
        )

    def seed_consent(
        self, user_id: int = 10, scope: str = "profile_text_extract"
    ) -> None:
        self.consents.append(
            {
                "user_id": int(user_id),
                "scope": scope,
                "version": "profile-text-v1",
                "policy_revision": "ai-policy-2026-08-07-v1",
                "granted_at": _now() - timedelta(days=1),
            }
        )

    def seed_projection(
        self,
        *,
        subject_user_id: int = 10,
        kind: str = "personal_searchable",
        revision: RevisionVector | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        vector = revision or RevisionVector(profile=1)
        row = {
            "id": self._next_projection_id,
            "subject_user_id": int(subject_user_id),
            "projection_kind": kind,
            "source_hash": f"hash-{self._next_projection_id}",
            "projection_version": "profile-extract-v1",
            "fields_json": "{}",
            "source_revision_json": json.dumps(vector.as_dict(), ensure_ascii=False),
            "profile_revision": vector.profile,
            "preference_revision": vector.preference,
            "privacy_revision": vector.privacy,
            "relationship_revision": vector.relationship,
            "policy_revision": vector.policy,
            "consent_snapshot_json": {"scope": "profile_text_extract"},
            "visibility_class": "searchable",
            "status": status,
            "invalidated_at": None,
            "invalidated_reason": None,
            "expires_at": None,
            "purge_after": None,
        }
        self._next_projection_id += 1
        self.projections.append(row)
        return row

    def seed_outbox(
        self,
        *,
        event_id: str = "evt-1",
        user_id: int = 10,
        event_type: str = "ai_profile_field_deleted",
        source_revision: RevisionVector,
        priority: int = 40,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "aggregate_type": "user",
            "aggregate_id": int(user_id),
            "event_type": event_type,
            "changed_fields": '["interest_tags"]',
            "source_revision_json": json.dumps(
                source_revision.as_dict(), ensure_ascii=False
            ),
            "occurred_at": _now(),
            "priority": priority,
            "lease_until": None,
        }

    def seed_search_result(self, target_user_id: int = 10) -> dict[str, Any]:
        row = {"target_user_id": int(target_user_id), "stale": 0}
        self.search_results.append(row)
        return row

    def seed_compat_snapshot(self, user_id: int = 10) -> dict[str, Any]:
        row = {
            "viewer_user_id": int(user_id),
            "target_user_id": int(user_id) + 1,
            "status": "ready",
            "invalidated_at": None,
        }
        self.compat_snapshots.append(row)
        return row

    # ---- query helpers --------------------------------------------------

    def latest_revision(self, user_id: int, subject: str) -> dict[str, Any] | None:
        candidates = [
            row
            for row in self.revisions.values()
            if row["user_id"] == int(user_id) and row["subject"] == subject
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: (-int(row["revision_no"]), -int(row["id"])))[0]

    def revision_fields_for(self, revision_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.revision_fields
            if row["revision_id"] == int(revision_id)
        ]

    def latest_consent(self, user_id: int, scope: str) -> dict[str, Any] | None:
        candidates = [
            row
            for row in self.consents
            if row["user_id"] == int(user_id) and row["scope"] == scope
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: row["granted_at"], reverse=True)[0]

    def insert_projection(self, params: dict[str, Any]) -> dict[str, Any]:
        """Upsert semantics：同唯一键
        (subject_user_id, projection_kind, source_hash, projection_version)
        的行被原位更新为新的 active 状态，而非新增一行（镜像真实 SQL 的
        INSERT ... ON DUPLICATE KEY UPDATE）。"""
        existing = self._find_projection(
            int(params["subject_user_id"]),
            str(params["projection_kind"]),
            str(params["source_hash"]),
            str(params["projection_version"]),
        )
        new_values = {
            "fields_json": params.get("fields_json"),
            "source_revision_json": params.get("source_revision_json"),
            "profile_revision": int(params.get("profile_revision") or 0),
            "preference_revision": int(params.get("preference_revision") or 0),
            "privacy_revision": int(params.get("privacy_revision") or 0),
            "relationship_revision": int(params.get("relationship_revision") or 0),
            "policy_revision": int(params.get("policy_revision") or 0),
            "consent_snapshot_json": params.get("consent_snapshot_json"),
            "visibility_class": str(params["visibility_class"]),
            "status": "active",
            "invalidated_at": None,
            "invalidated_reason": None,
            "expires_at": params.get("expires_at"),
            "purge_after": None,
        }
        if existing is not None:
            existing.update(new_values)
            return existing
        row = {
            "id": self._next_projection_id,
            "subject_user_id": int(params["subject_user_id"]),
            "projection_kind": str(params["projection_kind"]),
            "source_hash": str(params["source_hash"]),
            "projection_version": str(params["projection_version"]),
        }
        row.update(new_values)
        self._next_projection_id += 1
        self.projections.append(row)
        return row

    def _find_projection(
        self, subject_user_id: int, projection_kind: str, source_hash: str, projection_version: str
    ) -> dict[str, Any] | None:
        for row in self.projections:
            if (
                int(row["subject_user_id"]) == int(subject_user_id)
                and row["projection_kind"] == str(projection_kind)
                and row["source_hash"] == str(source_hash)
                and row["projection_version"] == str(projection_version)
            ):
                return row
        return None

    def invalidate_all_for_kind(
        self, subject_user_id: int, projection_kind: str, source_hash: str | None = None
    ) -> int:
        """差异化替换语义：重建路径只把该 kind 内 source_hash 不同的 active
        行标 invalidated（同 source_hash 的行由 upsert 原位更新回 active）。"""
        changed = 0
        for row in self.projections:
            if (
                int(row["subject_user_id"]) == int(subject_user_id)
                and row["projection_kind"] == str(projection_kind)
                and row["status"] == "active"
                and (source_hash is None or row["source_hash"] != source_hash)
            ):
                row["status"] = "invalidated"
                row["invalidated_at"] = _now()
                row["invalidated_reason"] = "rebuild"
                row["purge_after"] = _now() + timedelta(days=30)
                changed += 1
        return changed

    def invalidate_projections(self, params: dict[str, Any]) -> int:
        changed = 0
        stored_dims = (
            "profile_revision",
            "preference_revision",
            "privacy_revision",
            "relationship_revision",
            "policy_revision",
        )
        source = {
            dim: int(params[dim]) for dim in stored_dims
        }
        for row in self.projections:
            if int(row["subject_user_id"]) != int(params["user_id"]):
                continue
            if row["status"] != "active":
                continue
            if params.get("projection_kind") is not None and row[
                "projection_kind"
            ] != str(params["projection_kind"]):
                continue
            stored = {dim: int(row[dim]) for dim in stored_dims}
            # 与真实 SQL 一致：仅当 stored 逐维 <= source 且至少一维严格落后才失效。
            strictly_behind = stored != source and all(
                stored[dim] <= source[dim] for dim in stored_dims
            )
            if not strictly_behind:
                continue
            row["status"] = "invalidated"
            row["invalidated_at"] = _now()
            row["invalidated_reason"] = params.get("reason")
            row["purge_after"] = _now() + timedelta(days=30)
            changed += 1
        return changed

    def mark_search_stale(self, user_id: int) -> int:
        changed = 0
        for row in self.search_results:
            if int(row["target_user_id"]) == int(user_id) and int(row["stale"] or 0) == 0:
                row["stale"] = 1
                changed += 1
        return changed

    def mark_compat_stale(self, user_id: int) -> int:
        changed = 0
        for row in self.compat_snapshots:
            if row["status"] in ("stale", "blocked"):
                continue
            if int(row["viewer_user_id"]) == int(user_id) or int(
                row["target_user_id"]
            ) == int(user_id):
                row["status"] = "stale"
                row["invalidated_at"] = _now()
                changed += 1
        return changed

    def active_projections_for(self, user_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.projections
            if int(row["subject_user_id"]) == int(user_id) and row["status"] == "active"
        ]


class ProjectionFakeSession:
    """Routes Task 9 service SQL by substring onto one ProjectionStore."""

    def __init__(self, store: ProjectionStore) -> None:
        self.store = store
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0

    async def execute(
        self, statement: object, params: dict[str, Any] | None = None
    ) -> _MappingResult | _WriteResult:
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        store = self.store
        if "FROM ai_profile_revision" in sql and "ORDER BY r.revision_no" in sql:
            return _MappingResult(
                [store.latest_revision(int(values["user_id"]), str(values["subject"]))]
                or []
            )
        if "FROM ai_profile_revision_field" in sql:
            return _MappingResult(store.revision_fields_for(int(values["revision_id"])))
        if "FROM ai_consent_grant" in sql:
            return _MappingResult([store.latest_consent(int(values["user_id"]), str(values["scope"]))] or [])
        if "INSERT INTO ai_feature_projection" in sql:
            store.insert_projection(values)
            return _WriteResult(rowcount=1)
        if "UPDATE ai_feature_projection" in sql:
            if "subject_user_id" in values and "projection_kind" in values:
                # build_feature_projection 的重建路径：只失效同 kind 不同
                # source_hash 的 active 行（同 source_hash 走 upsert 原位更新）。
                return _WriteResult(
                    rowcount=store.invalidate_all_for_kind(
                        int(values["subject_user_id"]),
                        str(values["projection_kind"]),
                        str(values["source_hash"]),
                    )
                )
            return _WriteResult(rowcount=store.invalidate_projections(values))
        if "FROM user_revision_state" in sql:
            return _MappingResult([dict(store.revision_row)])
        if "UPDATE ai_search_result" in sql:
            return _WriteResult(rowcount=store.mark_search_stale(int(values["user_id"])))
        if "UPDATE ai_compatibility_snapshot" in sql:
            return _WriteResult(rowcount=store.mark_compat_stale(int(values["user_id"])))
        if "UPDATE derivation_outbox" in sql:
            return _WriteResult(rowcount=1)
        if "derivation_consumer_receipt" in sql and "INSERT" in sql:
            receipt = (str(values["event_id"]), str(values["consumer_name"]))
            if receipt in store.receipts:
                return _WriteResult(rowcount=0)
            store.receipts.append(receipt)
            return _WriteResult(rowcount=1)
        if "FROM derivation_outbox" in sql and "LEFT JOIN derivation_consumer_receipt" in sql:
            rows = store.outbox_rows if hasattr(store, "outbox_rows") else []
            return _MappingResult(rows)
        raise AssertionError(f"unhandled sql: {sql}")

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def store() -> ProjectionStore:
    return ProjectionStore()


@pytest.fixture
def db(store: ProjectionStore) -> ProjectionFakeSession:
    return ProjectionFakeSession(store)


# ----------------------------------------------------------------------
# Step 1: 简报逐字测试
# ----------------------------------------------------------------------


def test_projection_contains_confirmed_allowlisted_fields_only() -> None:
    payload = build_projection_payload(
        subject="personal",
        confirmed_fields=[
            {"field_key": "interest_tags", "value": ["旅行"]},
            {"field_key": "realname_status", "value": 2},
        ],
    )
    assert payload == {"interest_tags": ["旅行"]}


def test_projection_is_stale_when_any_revision_changes() -> None:
    stored = RevisionVector(profile=4, preference=1, privacy=3, relationship=2, policy=1)
    current = RevisionVector(profile=5, preference=1, privacy=3, relationship=2, policy=1)
    assert projection_is_current(stored, current) is False


# ----------------------------------------------------------------------
# build_projection_payload 边界
# ----------------------------------------------------------------------


def test_allowlist_is_frozen_to_the_ten_profile_fields() -> None:
    assert PROFILE_ALLOWLIST == frozenset(
        {
            "age",
            "city_code",
            "marriage_status",
            "education_level",
            "height_cm",
            "income_band",
            "occupation_group",
            "interest_tags",
            "lifestyle_tags",
            "relationship_goal",
        }
    )


def test_build_projection_payload_rejects_unsupported_subject() -> None:
    with pytest.raises(ValueError):
        build_projection_payload("organization", [])
    with pytest.raises(ValueError):
        build_projection_payload("", [])


def test_build_projection_payload_drops_non_allowlisted_and_auth_fields() -> None:
    payload = build_projection_payload(
        subject="ideal_partner",
        confirmed_fields=[
            {"field_key": "age", "value": 28},
            {"field_key": "height_cm", "value": 165},
            {"field_key": "realname_status", "value": 2},
            {"field_key": "id_card_masked", "value": "330***"},
        ],
    )
    assert payload == {"age": 28, "height_cm": 165}


def test_build_projection_payload_keeps_tag_list_values() -> None:
    payload = build_projection_payload(
        subject="personal",
        confirmed_fields=[
            {"field_key": "interest_tags", "value": ["旅行", "摄影"]},
            {"field_key": "lifestyle_tags", "value": ["早起"]},
        ],
    )
    assert payload == {"interest_tags": ["旅行", "摄影"], "lifestyle_tags": ["早起"]}


# ----------------------------------------------------------------------
# projection_is_current 全向量相等
# ----------------------------------------------------------------------


def test_projection_is_current_requires_full_vector_equality() -> None:
    base = RevisionVector(profile=4, preference=1, privacy=3, relationship=2, policy=1)
    assert projection_is_current(base, RevisionVector(**base.as_dict())) is True
    assert (
        projection_is_current(
            base, RevisionVector(profile=4, preference=2, privacy=3, relationship=2, policy=1)
        )
        is False
    )
    assert (
        projection_is_current(
            base, RevisionVector(profile=4, preference=1, privacy=4, relationship=2, policy=1)
        )
        is False
    )
    assert (
        projection_is_current(
            base, RevisionVector(profile=4, preference=1, privacy=3, relationship=3, policy=1)
        )
        is False
    )
    assert (
        projection_is_current(
            base, RevisionVector(profile=4, preference=1, privacy=3, relationship=2, policy=2)
        )
        is False
    )


# ----------------------------------------------------------------------
# build_feature_projection 落库与可见性
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_feature_projection_persists_allowlisted_fields(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    rid = store.seed_revision(user_id=10, subject="personal", revision_no=1)
    store.seed_revision_field(rid, "interest_tags", ["旅行"], subject="personal")
    store.seed_revision_field(rid, "realname_status", 2, subject="personal")
    store.seed_consent(10)

    projection = await build_feature_projection(
        db, 10, ProjectionKind.PERSONAL_SEARCHABLE, RevisionVector(profile=1)
    )

    assert projection.subject_user_id == 10
    assert projection.projection_kind == ProjectionKind.PERSONAL_SEARCHABLE
    assert projection.fields == {"interest_tags": ["旅行"]}
    assert projection.visibility_class == ProjectionVisibility.SEARCHABLE
    assert projection.source_revision == RevisionVector(profile=1)
    assert projection.status == "active"
    assert projection.consent_snapshot["scope"] == "profile_text_extract"
    assert len(store.projections) == 1
    stored = store.projections[0]
    assert json.loads(stored["fields_json"]) == {"interest_tags": ["旅行"]}
    assert stored["profile_revision"] == 1
    assert stored["visibility_class"] == "searchable"
    assert stored["consent_snapshot_json"] is not None


@pytest.mark.asyncio
async def test_build_feature_projection_ideal_partner_is_self_only(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    rid = store.seed_revision(
        user_id=10,
        subject="ideal_partner",
        revision_no=1,
        source_revision=RevisionVector(preference=1),
    )
    store.seed_revision_field(rid, "city_code", "330100", subject="ideal_partner")
    store.seed_consent(10)

    projection = await build_feature_projection(
        db, 10, ProjectionKind.IDEAL_PARTNER_PREFERENCE, RevisionVector(preference=1)
    )

    assert projection.visibility_class == ProjectionVisibility.SELF_ONLY
    # self_only 投影绝不能作为候选资料返回（由可见性类强制，读路径不得放行）。
    assert projection.visibility_class is not ProjectionVisibility.SEARCHABLE


@pytest.mark.asyncio
async def test_build_feature_projection_requires_consent(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    rid = store.seed_revision(user_id=10, subject="personal", revision_no=1)
    store.seed_revision_field(rid, "interest_tags", ["旅行"], subject="personal")

    with pytest.raises(ProjectionBuildError):
        await build_feature_projection(
            db, 10, ProjectionKind.PERSONAL_COMPATIBILITY, RevisionVector(profile=1)
        )
    assert store.projections == []


@pytest.mark.asyncio
async def test_build_feature_projection_without_confirmed_fields_writes_nothing(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    store.seed_revision(user_id=10, subject="personal", revision_no=1)
    store.seed_consent(10)

    with pytest.raises(ProjectionBuildError):
        await build_feature_projection(
            db, 10, ProjectionKind.PERSONAL_SEARCHABLE, RevisionVector(profile=1)
        )
    # 投影生成失败不产生空白「成功」投影。
    assert store.projections == []


@pytest.mark.asyncio
async def test_build_feature_projection_invalidates_previous_same_kind_projection(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    old = store.seed_projection(
        subject_user_id=10,
        kind="personal_searchable",
        revision=RevisionVector(profile=1),
        status="active",
    )
    rid = store.seed_revision(
        user_id=10, subject="personal", revision_no=2, source_revision=RevisionVector(profile=2)
    )
    store.seed_revision_field(rid, "interest_tags", ["旅行"], subject="personal")
    store.seed_consent(10)

    projection = await build_feature_projection(
        db, 10, ProjectionKind.PERSONAL_SEARCHABLE, RevisionVector(profile=2)
    )

    assert old["status"] == "invalidated"
    assert projection.source_revision == RevisionVector(profile=2)
    assert len(store.active_projections_for(10)) == 1
    assert store.active_projections_for(10)[0]["source_hash"] != old["source_hash"]


@pytest.mark.asyncio
async def test_build_feature_projection_same_source_hash_rebuild_is_upsert(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    """同版本重建（同 revision + 同 payload → 同 source_hash）不能撞唯一键。

    唯一键 uk_ai_feature_projection 不含 status，若重建先无条件失效再 INSERT，
    同 source_hash 的旧行已占槽位 → IntegrityError 且无任何 active 投影。
    修复后走 upsert：旧行被原位更新回 active，不新增、不失效、不冲突。
    """
    rid = store.seed_revision(user_id=10, subject="personal", revision_no=1)
    store.seed_revision_field(rid, "interest_tags", ["旅行"], subject="personal")
    store.seed_consent(10)

    first = await build_feature_projection(
        db, 10, ProjectionKind.PERSONAL_SEARCHABLE, RevisionVector(profile=1)
    )
    original_id = store.projections[0]["id"]
    assert len(store.projections) == 1

    # 同 revision + 同 payload：source_hash 必须一致，重建不得抛唯一键冲突。
    second = await build_feature_projection(
        db, 10, ProjectionKind.PERSONAL_SEARCHABLE, RevisionVector(profile=1)
    )

    assert first.source_hash == second.source_hash
    assert len(store.projections) == 1  # 原位更新，不新增行
    assert len(store.active_projections_for(10)) == 1  # 不留下无效状态
    stored = store.projections[0]
    assert stored["id"] == original_id  # 旧行被更新而非冲突
    assert stored["status"] == "active"
    assert stored["invalidated_at"] is None
    assert stored["invalidated_reason"] is None
    assert stored["purge_after"] is None
    assert stored["expires_at"] is not None
    assert json.loads(stored["fields_json"]) == {"interest_tags": ["旅行"]}
    assert stored["profile_revision"] == 1


# ----------------------------------------------------------------------
# invalidate_projection 版本向量失效语义
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_projection_marks_stale_projections_only(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    store.seed_projection(
        subject_user_id=10, kind="personal_searchable", revision=RevisionVector(profile=1)
    )
    current = store.seed_projection(
        subject_user_id=10, kind="personal_compatibility", revision=RevisionVector(profile=2)
    )
    other_user = store.seed_projection(
        subject_user_id=11, kind="personal_searchable", revision=RevisionVector(profile=1)
    )

    count = await invalidate_projection(
        db, 10, "ai_profile_field_deleted", RevisionVector(profile=2)
    )

    assert count == 1
    assert store.projections[0]["status"] == "invalidated"
    assert store.projections[0]["invalidated_reason"] == "ai_profile_field_deleted"
    # 与当前版本向量一致的投影不被误标。
    assert current["status"] == "active"
    # 其他用户的投影不受影响。
    assert other_user["status"] == "active"


@pytest.mark.asyncio
async def test_invalidate_projection_old_event_cannot_overwrite_newer_projection(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    # 新投影建立在更高版本向量上；旧事件的 source_revision 落后于它。
    newer = store.seed_projection(
        subject_user_id=10, kind="personal_searchable", revision=RevisionVector(profile=5)
    )

    count = await invalidate_projection(
        db, 10, "ai_profile_deleted", RevisionVector(profile=3)
    )

    assert count == 0
    assert newer["status"] == "active"


@pytest.mark.asyncio
async def test_invalidate_projection_supports_kind_scoping(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    store.seed_projection(
        subject_user_id=10, kind="personal_searchable", revision=RevisionVector(profile=1)
    )
    store.seed_projection(
        subject_user_id=10, kind="personal_compatibility", revision=RevisionVector(profile=1)
    )

    count = await invalidate_projection(
        db,
        10,
        "ai_profile_deleted",
        RevisionVector(profile=2),
        projection_kind=ProjectionKind.PERSONAL_SEARCHABLE,
    )

    assert count == 1
    assert store.projections[0]["status"] == "invalidated"
    assert store.projections[1]["status"] == "active"


# ----------------------------------------------------------------------
# outbox 消费者：占位 handler 覆盖 + 版本守卫 + 收据幂等
# ----------------------------------------------------------------------


def test_cleanup_handlers_are_real_invalidation_consumers() -> None:
    # Task 8 的占位 handler 已被真实实现覆盖（先覆盖，后启用消费循环）。
    assert callable(CLEANUP_HANDLERS.get("ai_profile_deleted"))
    assert callable(CLEANUP_HANDLERS.get("ai_preference_deleted"))
    assert callable(CLEANUP_HANDLERS.get("ai_profile_field_deleted"))
    names = {
        CLEANUP_HANDLERS.get(key).__name__ if CLEANUP_HANDLERS.get(key) else ""
        for key in ("ai_profile_deleted", "ai_preference_deleted", "ai_profile_field_deleted")
    }
    assert "_placeholder" not in " ".join(name for name in names)


@pytest.mark.asyncio
async def test_cleanup_consumer_supersedes_stale_events(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    # 投影已按 profile=2 构建；删除事件记录的是更早的 profile=1。
    store.seed_projection(
        subject_user_id=10, kind="personal_searchable", revision=RevisionVector(profile=2)
    )
    store.revision_row = {
        "profile_revision": 2,
        "preference_revision": 0,
        "privacy_revision": 0,
        "relationship_revision": 0,
        "policy_revision": 0,
    }
    store.outbox_rows = [
        store.seed_outbox(
            event_id="evt-old",
            event_type="ai_profile_field_deleted",
            source_revision=RevisionVector(profile=1),
        )
    ]

    stats = await run_cleanup_consumer_round(
        db, "worker-1", now=_now(), limit=10
    )

    assert stats["superseded"] == 1
    assert stats["applied"] == 0
    # 旧事件不能覆盖新投影：新投影保持 active。
    assert store.active_projections_for(10)
    # superseded 事件也写收据，避免无限重试。
    assert ("evt-old", "cleanup") in store.receipts


@pytest.mark.asyncio
async def test_cleanup_consumer_applies_current_events_and_invalidates(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    store.seed_projection(
        subject_user_id=10, kind="personal_searchable", revision=RevisionVector(profile=1)
    )
    store.seed_search_result(target_user_id=10)
    store.seed_compat_snapshot(user_id=10)
    store.revision_row = {
        "profile_revision": 2,
        "preference_revision": 0,
        "privacy_revision": 0,
        "relationship_revision": 0,
        "policy_revision": 0,
    }
    store.outbox_rows = [
        store.seed_outbox(
            event_id="evt-current",
            event_type="ai_profile_field_deleted",
            source_revision=RevisionVector(profile=2),
        )
    ]

    stats = await run_cleanup_consumer_round(
        db, "worker-1", now=_now(), limit=10
    )

    assert stats["applied"] == 1
    assert stats["superseded"] == 0
    assert store.active_projections_for(10) == []
    assert store.projections[0]["invalidated_reason"] == "ai_profile_field_deleted"
    # 派生结果一并标 stale。
    assert store.search_results[0]["stale"] == 1
    assert store.compat_snapshots[0]["status"] == "stale"
    assert ("evt-current", "cleanup") in store.receipts


@pytest.mark.asyncio
async def test_cleanup_consumer_duplicate_receipt_is_intercepted(
    store: ProjectionStore, db: ProjectionFakeSession
) -> None:
    store.revision_row = {
        "profile_revision": 2,
        "preference_revision": 0,
        "privacy_revision": 0,
        "relationship_revision": 0,
        "policy_revision": 0,
    }
    store.outbox_rows = [
        store.seed_outbox(
            event_id="evt-dup",
            event_type="ai_profile_field_deleted",
            source_revision=RevisionVector(profile=2),
        )
    ]
    store.receipts.append(("evt-dup", "cleanup"))

    stats = await run_cleanup_consumer_round(
        db, "worker-1", now=_now(), limit=10
    )

    assert stats["applied"] == 0
    assert stats["duplicate"] == 1
