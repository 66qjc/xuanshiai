"""Task 11 acceptance contract: M06 bidirectional shadow snapshots.

The three Step 1 tests are mirrored verbatim from the task brief.  Additional
tests pin the frozen eight-dimension weights (统一方案 §9.2), the stable reason
code / evidence-ref alignment (§9.3), hard-gate-before-rules on the read path
(§5.2), snapshot staleness on profile/privacy revision changes (§5.5), and the
shadow discipline: ``compatibility-rule-v1`` writes only
``ai_compatibility_snapshot`` with ``display_eligible=false`` while the legacy
``match_score`` semantics stay ``legacy-rule-v1`` (§9.1/§9.5, §10.4).

``rule_set`` / ``feature_a`` / ``feature_b`` / ``sparse_feature_b`` /
``compatibility_store`` are the fixtures the brief mandates.

Round-1 fix (review I-1): ``test_get_route_commits_stale_marking`` goes through the
GET route (not the store directly) and asserts the read path ``commit()`` executes,
so the documented "读取时落库标记 stale" actually persists in production instead
of being rolled back when ``get_db`` exits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.schemas.ai_common import ProjectionKind
from app.schemas.ai_compatibility import (
    CompatibilitySnapshotStatus,
)
from app.services.ai.compatibility import (
    COMPATIBILITY_ALGORITHM_VERSION,
    COMPATIBILITY_CONSENT_SCOPE,
    COMPATIBILITY_RULES,
    DISCLAIMER,
    CandidateNotVisible,
    CompatibilityResultStale,
    FeatureSet,
    RuleSet,
    build_compatibility_evidence,
    compute_compatibility,
    load_compatibility_features,
    read_compatibility_snapshot,
    request_compatibility_recompute,
    with_evidence_codes,
    write_shadow_snapshot,
)
from app.services.discovery import _card
from app.services.revisions import RevisionVector

client = TestClient(app)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _fields_payload(fields: dict[str, Any]) -> str:
    return json.dumps(fields, ensure_ascii=False)


# ----------------------------------------------------------------------
# 纯函数 fixtures（简报 Step 1）
# ----------------------------------------------------------------------


@pytest.fixture
def rule_set() -> RuleSet:
    return COMPATIBILITY_RULES


@pytest.fixture
def feature_a() -> FeatureSet:
    return FeatureSet(
        profile={
            "age": 30,
            "city_code": "330100",
            "marriage_status": "single",
            "education_level": 3,
            "height_cm": 175,
            "income_band": 20000,
            "interest_tags": ["旅行", "摄影"],
            "relationship_goal": "婚姻",
        },
        preference={
            "age": {"min": 26, "max": 34},
            "city_code": ["330100", "330200"],
            "marriage_status": "single",
            "education_level": {"min": 3},
            "height_cm": {"min": 160, "max": 180},
            "income_band": {"min": 10000},
            "interest_tags": ["旅行", "音乐"],
            "relationship_goal": "婚姻",
        },
    )


@pytest.fixture
def feature_b() -> FeatureSet:
    return FeatureSet(
        profile={
            "age": 28,
            "city_code": "330100",
            "marriage_status": "single",
            "education_level": 4,
            "height_cm": 165,
            "income_band": 15000,
            "interest_tags": ["旅行", "美食"],
            "relationship_goal": "婚姻",
        },
        preference={
            "age": {"min": 28, "max": 36},
            "city_code": ["330100"],
            "marriage_status": "single",
            "education_level": {"min": 3},
            "height_cm": {"min": 170, "max": 185},
            "income_band": {"min": 8000},
            "interest_tags": ["摄影", "旅行"],
            "relationship_goal": "婚姻",
        },
    )


@pytest.fixture
def sparse_feature_b() -> FeatureSet:
    """只确认了 age 偏好的对象：多数维度缺失，只降低 coverage/标 unknown。"""
    return FeatureSet(
        profile={
            "age": 28,
            "city_code": "330100",
            "interest_tags": ["旅行"],
        },
        preference={
            "age": {"min": 26, "max": 32},
        },
    )


# ----------------------------------------------------------------------
# Step 1：简报逐字测试
# ----------------------------------------------------------------------


def test_direction_exchange_is_replayable_and_pair_score_is_bounded(
    rule_set: RuleSet, feature_a: FeatureSet, feature_b: FeatureSet
) -> None:
    forward = compute_compatibility(feature_a, feature_b, rule_set)
    reverse = compute_compatibility(feature_b, feature_a, rule_set)
    assert forward.directions == (reverse.directions[1], reverse.directions[0])
    assert 0 <= forward.pair_score <= 100
    assert forward.coverage >= 0.5


def test_missing_dimensions_reduce_coverage_not_to_a_fake_zero(
    feature_a: FeatureSet, sparse_feature_b: FeatureSet, rule_set: RuleSet
) -> None:
    result = compute_compatibility(feature_a, sparse_feature_b, rule_set)
    assert "DIMENSION_UNKNOWN" in result.reason_codes
    assert result.coverage < 0.5 or result.pair_score is not None


@pytest.mark.asyncio
async def test_shadow_never_overwrites_legacy_match_score(compatibility_store) -> None:
    await compatibility_store.write_shadow(viewer_id=10, target_id=42)
    legacy = await compatibility_store.read_legacy_card(viewer_id=10, target_id=42)
    assert legacy.algorithm_version == "legacy-rule-v1"
    assert legacy.match_score_source == "legacy-rule-v1"


# ----------------------------------------------------------------------
# §9.2 维度权重冻结（非科学概率）
# ----------------------------------------------------------------------


def test_frozen_weights_follow_section_9_2() -> None:
    weights = {rule.key: rule.weight for rule in COMPATIBILITY_RULES.dimensions}
    assert weights == {
        "age": 20.0,
        "city_code": 15.0,
        "marriage_status": 10.0,
        "education_level": 10.0,
        "height_cm": 10.0,
        "income_band": 10.0,
        "interest_tags": 15.0,
        "relationship_goal": 10.0,
    }
    assert COMPATIBILITY_RULES.total_weight == 100.0


def test_off_compatibility_fields_never_enter_the_rules() -> None:
    keys = {rule.key for rule in COMPATIBILITY_RULES.dimensions}
    for banned in ("mbti", "realname_status", "online_status", "is_vip", "is_boosted"):
        assert banned not in keys


def test_preference_conflict_only_lowers_directional_satisfaction(
    rule_set: RuleSet, feature_a: FeatureSet, feature_b: FeatureSet
) -> None:
    # 方向 A→B 与 B→A 允许不同；偏好冲突只影响方向满足度，不绕过门禁。
    forward = compute_compatibility(feature_a, feature_b, rule_set)
    reverse = compute_compatibility(feature_b, feature_a, rule_set)
    assert forward.directions[0] != reverse.directions[0] or forward.directions[1] != reverse.directions[1]


# ----------------------------------------------------------------------
# §9.3 证据与原因码 100% 对齐
# ----------------------------------------------------------------------


def test_every_reason_code_has_exactly_one_evidence_ref(
    rule_set: RuleSet, feature_a: FeatureSet, feature_b: FeatureSet
) -> None:
    result = compute_compatibility(feature_a, feature_b, rule_set)
    refs = build_compatibility_evidence(result)
    assert {ref.reason_code for ref in refs} == set(result.reason_codes)
    for ref in refs:
        assert isinstance(ref.field_keys, tuple)
        # 不存对方敏感原文：evidence 只引用字段 key 与可展示标记。
        assert ref.displayable in (True, False)
        assert ref.limitation


def test_sparse_result_marks_unknown_and_coverage_insufficient(
    rule_set: RuleSet, feature_a: FeatureSet, sparse_feature_b: FeatureSet
) -> None:
    result = compute_compatibility(feature_a, sparse_feature_b, rule_set)
    assert result.status == "coverage_insufficient"
    assert "COVERAGE_INSUFFICIENT" in result.reason_codes
    assert result.pair_score is None
    refs = build_compatibility_evidence(result)
    assert {ref.reason_code for ref in refs} == set(result.reason_codes)


# ----------------------------------------------------------------------
# shadow 快照写入：只写 ai_compatibility_snapshot，display_eligible=false
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class LegacyCard:
    algorithm_version: str
    match_score_source: str
    match_score: float
    match_reason: str


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


class _TaskStore:
    """Minimal ai_task rows for recompute idempotency / replay tests."""

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

    def insert(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params["task_id"])
        now = _now()
        row = {
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
            "max_attempts": 3,
            "next_run_at": None,
            "lease_owner": None,
            "lease_until": None,
            "consent_snapshot_json": params.get("consent_snapshot_json"),
            "source_revision_json": params.get("source_revision_json"),
            "payload_summary": params.get("payload_summary"),
            "error_code": None,
            "error_message": None,
            "result_ref": None,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
        }
        self.tasks[task_id] = row
        self._next_id += 1
        return row


class CompatibilityStore:
    """In-memory store backing the fake session for Task 11 SQL routing."""

    def __init__(self) -> None:
        self.projections: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []
        self.legacy_cards: dict[tuple[int, int], dict[str, Any]] = {}
        self.consents: list[dict[str, Any]] = []
        self.revision_rows: dict[int, dict[str, Any]] = {}
        self.visibility_rows: dict[int, dict[str, Any]] = {}
        self.task_store = _TaskStore()
        self._next_snapshot_id = 1
        self.forced_payload_summary: dict[str, Any] | None = None

    # ---- seed helpers --------------------------------------------------

    def seed_projection(
        self,
        *,
        user_id: int,
        kind: str,
        fields: dict[str, Any],
        revision: RevisionVector,
        status: str = "active",
    ) -> dict[str, Any]:
        row = {
            "id": self._next_snapshot_id,
            "subject_user_id": int(user_id),
            "projection_kind": kind,
            "source_hash": f"hash-{self._next_snapshot_id}",
            "projection_version": "profile-extract-v1",
            "fields_json": _fields_payload(fields),
            "source_revision_json": json.dumps(revision.as_dict(), ensure_ascii=False),
            "profile_revision": revision.profile,
            "preference_revision": revision.preference,
            "privacy_revision": revision.privacy,
            "relationship_revision": revision.relationship,
            "policy_revision": revision.policy,
            "consent_snapshot_json": {
                "scope": "profile_text_extract",
                "version": "profile-text-v1",
                "policy_revision": "ai-policy-2026-08-07-v1",
                "granted_at": "2026-01-01T00:00:00",
            },
            "visibility_class": "self_only" if kind == "ideal_partner_preference" else "searchable",
            "status": status,
            "invalidated_at": None,
            "invalidated_reason": None,
            "expires_at": _now() + timedelta(days=30),
            "purge_after": None,
        }
        self.projections.append(row)
        self._next_snapshot_id += 1
        return row

    # ---- brief Step 1 fixture surface -----------------------------------

    async def write_shadow(self, viewer_id: int, target_id: int) -> str:
        """写一条 shadow 快照（compatibility-rule-v1，display_eligible=false）。

        使用本 store 的假会话走真实服务函数，确保 shadow 只写
        ai_compatibility_snapshot、不触碰旧字段。
        """
        db = CompatibilityFakeSession(self)
        viewer_rev = self.revision_rows[int(viewer_id)]
        target_rev = self.revision_rows[int(target_id)]
        viewer_fs, target_fs = await load_compatibility_features(
            db, int(viewer_id), int(target_id)
        )
        result = compute_compatibility(viewer_fs, target_fs, COMPATIBILITY_RULES)
        result = with_evidence_codes(result, viewer_fs, target_fs, COMPATIBILITY_RULES)
        consent = {
            "viewer": {
                "scope": COMPATIBILITY_CONSENT_SCOPE,
                "version": "compatibility-shadow-v1",
                "policy_revision": "ai-policy-2026-08-07-v1",
                "granted_at": datetime(2026, 1, 1),
                "revoked_at": None,
            },
            "target": {
                "scope": COMPATIBILITY_CONSENT_SCOPE,
                "version": "compatibility-shadow-v1",
                "policy_revision": "ai-policy-2026-08-07-v1",
                "granted_at": datetime(2026, 1, 1),
                "revoked_at": None,
            },
        }
        return await write_shadow_snapshot(
            db,
            int(viewer_id),
            int(target_id),
            result,
            (viewer_rev, target_rev),
            consent,
        )

    async def read_legacy_card(self, viewer_id: int, target_id: int) -> LegacyCard:
        """读取旧推荐卡片的 match_score/match_reason（语义恒为 legacy-rule-v1）。"""
        row = self.legacy_cards[(int(viewer_id), int(target_id))]
        return LegacyCard(
            algorithm_version=row["algorithm_version"],
            match_score_source=row["match_score_source"],
            match_score=row["match_score"],
            match_reason=row["match_reason"],
        )

    def seed_default_profiles(self) -> None:
        viewer_rev = RevisionVector(
            profile=3, preference=2, privacy=1, relationship=0, policy=1
        )
        target_rev = RevisionVector(
            profile=5, preference=4, privacy=2, relationship=0, policy=1
        )
        self.revision_rows[10] = viewer_rev
        self.revision_rows[42] = target_rev
        self.seed_projection(
            user_id=10,
            kind=ProjectionKind.PERSONAL_COMPATIBILITY.value,
            fields={
                "age": 30,
                "city_code": "330100",
                "marriage_status": "single",
                "education_level": 3,
                "height_cm": 175,
                "income_band": 20000,
                "interest_tags": ["旅行", "摄影"],
                "relationship_goal": "婚姻",
            },
            revision=viewer_rev,
        )
        self.seed_projection(
            user_id=10,
            kind=ProjectionKind.IDEAL_PARTNER_PREFERENCE.value,
            fields={
                "age": {"min": 26, "max": 34},
                "city_code": ["330100", "330200"],
                "marriage_status": "single",
                "education_level": {"min": 3},
                "height_cm": {"min": 160, "max": 180},
                "income_band": {"min": 10000},
                "interest_tags": ["旅行", "音乐"],
                "relationship_goal": "婚姻",
            },
            revision=viewer_rev,
        )
        self.seed_projection(
            user_id=42,
            kind=ProjectionKind.PERSONAL_COMPATIBILITY.value,
            fields={
                "age": 28,
                "city_code": "330100",
                "marriage_status": "single",
                "education_level": 4,
                "height_cm": 165,
                "income_band": 15000,
                "interest_tags": ["旅行", "美食"],
                "relationship_goal": "婚姻",
            },
            revision=target_rev,
        )
        self.seed_projection(
            user_id=42,
            kind=ProjectionKind.IDEAL_PARTNER_PREFERENCE.value,
            fields={
                "age": {"min": 28, "max": 36},
                "city_code": ["330100"],
                "marriage_status": "single",
                "education_level": {"min": 3},
                "height_cm": {"min": 170, "max": 185},
                "income_band": {"min": 8000},
                "interest_tags": ["摄影", "旅行"],
                "relationship_goal": "婚姻",
            },
            revision=target_rev,
        )
        self.seed_consent(10, "profile_text_extract")
        self.seed_consent(42, "profile_text_extract")
        self.seed_consent(10, COMPATIBILITY_CONSENT_SCOPE)
        self.seed_consent(42, COMPATIBILITY_CONSENT_SCOPE)
        self.seed_legacy_card(10, 42)

    def seed_consent(self, user_id: int, scope: str) -> None:
        self.consents.append(
            {
                "user_id": int(user_id),
                "scope": scope,
                "version": (
                    "profile-text-v1"
                    if scope == "profile_text_extract"
                    else "compatibility-shadow-v1"
                ),
                "policy_revision": "ai-policy-2026-08-07-v1",
                "granted_at": datetime(2026, 1, 1),
            }
        )

    def seed_legacy_card(self, viewer_id: int, target_id: int) -> None:
        self.legacy_cards[(int(viewer_id), int(target_id))] = {
            "algorithm_version": "legacy-rule-v1",
            "match_score_source": "legacy-rule-v1",
            "match_score": 88.0,
            "match_reason": "同城、年龄相仿",
        }

    def seed_snapshot(self, **overrides: Any) -> dict[str, Any]:
        row = {
            "id": self._next_snapshot_id,
            "snapshot_id": f"cp_seeded_{self._next_snapshot_id}",
            "viewer_user_id": 10,
            "target_user_id": 42,
            "algorithm_version": COMPATIBILITY_ALGORITHM_VERSION,
            "snapshot_hash": "hash-seeded",
            "status": "ready",
            "score_semantics": "rule_based_reference_shadow",
            "compatibility_index": 78.0,
            "coverage": 0.75,
            "direction_json": json.dumps(
                {"viewer_to_target": 82.0, "target_to_viewer": 74.0}
            ),
            "reason_codes": json.dumps(
                ["AGE_MUTUAL_WITHIN_RANGE", "INTEREST_OVERLAP"]
            ),
            "evidence_json": json.dumps([]),
            "profile_revision_pair_json": json.dumps({"viewer": 3, "target": 5}),
            "privacy_revision_pair_json": json.dumps({"viewer": 1, "target": 2}),
            "source_revision_pair_json": json.dumps(
                {
                    "viewer": {
                        "profile": 3,
                        "preference": 2,
                        "privacy": 1,
                        "relationship": 0,
                        "policy": 1,
                    },
                    "target": {
                        "profile": 5,
                        "preference": 4,
                        "privacy": 2,
                        "relationship": 0,
                        "policy": 1,
                    },
                }
            ),
            "consent_snapshot_pair_json": json.dumps(
                {
                    "viewer": {
                        "scope": COMPATIBILITY_CONSENT_SCOPE,
                        "version": "compatibility-shadow-v1",
                        "policy_revision": "ai-policy-2026-08-07-v1",
                        "granted_at": "2026-01-01T00:00:00",
                    },
                    "target": {
                        "scope": COMPATIBILITY_CONSENT_SCOPE,
                        "version": "compatibility-shadow-v1",
                        "policy_revision": "ai-policy-2026-08-07-v1",
                        "granted_at": "2026-01-01T00:00:00",
                    },
                }
            ),
            "experiment_bucket": "shadow",
            "display_eligible": 0,
            "disclaimer": DISCLAIMER,
            "calculated_at": _now() - timedelta(minutes=1),
            "expires_at": _now() + timedelta(minutes=10),
            "invalidated_at": None,
            "purge_after": None,
            "created_at": _now() - timedelta(minutes=1),
        }
        row.update(overrides)
        self.snapshots.append(row)
        self._next_snapshot_id += 1
        return row

    # ---- query helpers --------------------------------------------------

    def active_projections_for(self, user_id: int, kind: str) -> dict[str, Any] | None:
        for row in self.projections:
            if (
                int(row["subject_user_id"]) == int(user_id)
                and row["projection_kind"] == kind
                and row["status"] == "active"
            ):
                return row
        return None

    def insert_snapshot(self, params: dict[str, Any]) -> None:
        row = {
            "snapshot_id": str(params["snapshot_id"]),
            "viewer_user_id": int(params["viewer_user_id"]),
            "target_user_id": int(params["target_user_id"]),
            "algorithm_version": str(params["algorithm_version"]),
            "snapshot_hash": str(params["snapshot_hash"]),
            "status": str(params["status"]),
            "score_semantics": str(params["score_semantics"]),
            "compatibility_index": params.get("compatibility_index"),
            "coverage": params.get("coverage"),
            "direction_json": params.get("direction_json"),
            "reason_codes": params.get("reason_codes"),
            "evidence_json": params.get("evidence_json"),
            "profile_revision_pair_json": params.get("profile_revision_pair_json"),
            "privacy_revision_pair_json": params.get("privacy_revision_pair_json"),
            "source_revision_pair_json": params.get("source_revision_pair_json"),
            "consent_snapshot_pair_json": params.get("consent_snapshot_pair_json"),
            "experiment_bucket": str(params["experiment_bucket"]),
            "display_eligible": int(params["display_eligible"] or 0),
            "disclaimer": params.get("disclaimer"),
            "calculated_at": _now(),
            "expires_at": params.get("expires_at"),
            "invalidated_at": None,
            "purge_after": None,
            "created_at": _now(),
        }
        # 幂等：同 (viewer,target,algorithm,snapshot_hash) 原位更新。
        existing = self._find_snapshot_pair(
            int(params["viewer_user_id"]),
            int(params["target_user_id"]),
            str(params["algorithm_version"]),
            str(params["snapshot_hash"]),
        )
        if existing is not None:
            existing.update(row)
        else:
            row["id"] = self._next_snapshot_id
            self.snapshots.append(row)
            self._next_snapshot_id += 1

    def latest_snapshot(self, viewer_id: int, target_id: int) -> dict[str, Any] | None:
        candidates = [
            row
            for row in self.snapshots
            if int(row["viewer_user_id"]) == int(viewer_id)
            and int(row["target_user_id"]) == int(target_id)
            and row["algorithm_version"] == COMPATIBILITY_ALGORITHM_VERSION
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: (-int(row["id"]),))[0]

    def mark_stale(self, viewer_id: int, target_id: int) -> int:
        changed = 0
        for row in self.snapshots:
            if (
                int(row["viewer_user_id"]) == int(viewer_id)
                and int(row["target_user_id"]) == int(target_id)
                and row["status"] not in ("stale", "blocked")
            ):
                row["status"] = "stale"
                row["invalidated_at"] = _now()
                changed += 1
        return changed

    def _find_snapshot_pair(
        self, viewer_id: int, target_id: int, algorithm_version: str, snapshot_hash: str
    ) -> dict[str, Any] | None:
        for row in self.snapshots:
            if (
                int(row["viewer_user_id"]) == viewer_id
                and int(row["target_user_id"]) == target_id
                and row["algorithm_version"] == algorithm_version
                and row["snapshot_hash"] == snapshot_hash
            ):
                return row
        return None


class CompatibilityFakeSession:
    """Routes Task 11 service SQL by substring onto one CompatibilityStore.

    Any write that is not ``ai_compatibility_snapshot`` (e.g. a stray legacy
    ``match_score`` write) is an error — this is the shadow-isolation gate.
    """

    def __init__(self, store: CompatibilityStore) -> None:
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
        if "INSERT INTO ai_task" in sql:
            row = store.task_store.insert(values)
            return _MappingResult([row])
        if "UPDATE ai_task SET payload_summary" in sql:
            row = store.task_store.tasks.get(values.get("task_id"))
            if row is not None:
                row["payload_summary"] = values.get("payload_summary")
            return _WriteResult(rowcount=1)
        if "SELECT" in sql and "FROM ai_task" in sql and "owner_user_id" in values:
            row = store.task_store.find_by_idempotency(
                int(values["owner_user_id"]),
                str(values["task_type"]),
                str(values["idempotency_key"]),
            )
            return _MappingResult([row] if row else [])
        if "SELECT" in sql and "FROM ai_task" in sql and "task_id" in values:
            row = store.task_store.tasks.get(str(values["task_id"]))
            return _MappingResult([row] if row else [])
        if "INSERT INTO ai_compatibility_snapshot" in sql:
            store.insert_snapshot(values)
            return _WriteResult(rowcount=1)
        if "UPDATE ai_compatibility_snapshot" in sql:
            return _WriteResult(
                rowcount=store.mark_stale(
                    int(values["viewer_user_id"]), int(values["target_user_id"])
                )
            )
        if "INSERT" in sql or "UPDATE" in sql or "DELETE" in sql:
            # shadow 永不写旧字段/旧表（隔离门禁）。
            raise AssertionError(f"shadow must not write legacy tables: {sql}")
        if "FROM ai_feature_projection" in sql:
            wanted = {int(values["uid_viewer"]), int(values["uid_target"])}
            rows = [
                row
                for row in store.projections
                if row["status"] == "active"
                and int(row["subject_user_id"]) in wanted
            ]
            return _MappingResult(rows)
        if "FROM ai_compatibility_snapshot" in sql and "engine" in values:
            # WP-C1 llm 新鲜快照查询（绑定名 viewer/target/engine，与规则读取不同）：
            # 测试 store 无 llm 快照 → 空（fresh miss）。
            return _MappingResult([])
        if "FROM ai_compatibility_snapshot" in sql:
            row = store.latest_snapshot(
                int(values["viewer_user_id"]), int(values["target_user_id"])
            )
            return _MappingResult([row] if row else [])
        if "candidate.id AS candidate_id" in sql:
            row = store.visibility_rows.get(int(values["candidate_id"]))
            if row is None:
                row = {
                    "candidate_id": int(values["candidate_id"]),
                    "viewer_realname_status": 2,
                    "viewer_is_vip": 1,
                    "who_can_see_me": 1,
                    "account_active": 1,
                    "profile_visible": 1,
                    "match_active": 1,
                    "not_restricted": 1,
                    "profile_complete": 1,
                    "media_approved": 1,
                    "blocked": 0,
                }
            return _MappingResult([row])
        if "FROM user_revision_state" in sql:
            row = store.revision_rows.get(int(values["user_id"]))
            return _MappingResult(
                [
                    {
                        "profile_revision": row.profile if row else 0,
                        "preference_revision": row.preference if row else 0,
                        "privacy_revision": row.privacy if row else 0,
                        "relationship_revision": row.relationship if row else 0,
                        "policy_revision": row.policy if row else 0,
                    }
                ]
            )
        if "FROM ai_consent_grant" in sql:
            matches = [
                row
                for row in store.consents
                if int(row["user_id"]) == int(values["user_id"])
                and row["scope"] == str(values["scope"])
                and row.get("revoked_at") is None
            ]
            return _MappingResult(matches)
        if "FROM user_match_recommend" in sql:
            row = store.legacy_cards.get(
                (int(values["viewer_user_id"]), int(values["target_user_id"]))
            )
            return _MappingResult([dict(row)] if row else [])
        raise AssertionError(f"unhandled sql: {sql}")

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def flush(self) -> None:
        pass


@pytest.fixture
def compatibility_store() -> CompatibilityStore:
    store = CompatibilityStore()
    store.seed_default_profiles()
    return store


@pytest.fixture
def compat_db(
    compatibility_store: CompatibilityStore,
) -> CompatibilityFakeSession:
    return CompatibilityFakeSession(compatibility_store)


# ----------------------------------------------------------------------
# 追加：shadow 写入与读取
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_shadow_persists_compatibility_rule_v1_shadow_snapshot(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    viewer_rev = compatibility_store.revision_rows[10]
    target_rev = compatibility_store.revision_rows[42]
    consent = {
        "viewer": {
            "scope": COMPATIBILITY_CONSENT_SCOPE,
            "version": "compatibility-shadow-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
            "granted_at": datetime(2026, 1, 1),
        },
        "target": {
            "scope": COMPATIBILITY_CONSENT_SCOPE,
            "version": "compatibility-shadow-v1",
            "policy_revision": "ai-policy-2026-08-07-v1",
            "granted_at": datetime(2026, 1, 1),
        },
    }
    viewer_fs, target_fs = await load_compatibility_features(
        compat_db, 10, 42
    )
    result = compute_compatibility(viewer_fs, target_fs, COMPATIBILITY_RULES)
    from app.services.ai.compatibility import with_evidence_codes

    result = with_evidence_codes(result, viewer_fs, target_fs, COMPATIBILITY_RULES)
    snapshot_id = await write_shadow_snapshot(
        compat_db, 10, 42, result, (viewer_rev, target_rev), consent
    )

    assert snapshot_id.startswith("cp_")
    assert len(compatibility_store.snapshots) == 1
    row = compatibility_store.snapshots[0]
    assert row["viewer_user_id"] == 10
    assert row["target_user_id"] == 42
    assert row["algorithm_version"] == "compatibility-rule-v1"
    assert row["score_semantics"] == "rule_based_reference_shadow"
    assert row["experiment_bucket"] == "shadow"
    assert row["display_eligible"] == 0
    assert row["status"] == "ready"
    assert row["compatibility_index"] is not None
    assert float(row["coverage"]) == 1.0
    assert json.loads(row["profile_revision_pair_json"]) == {"viewer": 3, "target": 5}
    assert json.loads(row["privacy_revision_pair_json"]) == {"viewer": 1, "target": 2}
    source_pair = json.loads(row["source_revision_pair_json"])
    assert source_pair["viewer"] == viewer_rev.as_dict()
    assert source_pair["target"] == target_rev.as_dict()
    consent_pair = json.loads(row["consent_snapshot_pair_json"])
    assert consent_pair["viewer"]["scope"] == COMPATIBILITY_CONSENT_SCOPE
    assert consent_pair["target"]["scope"] == COMPATIBILITY_CONSENT_SCOPE
    # 原因码含相互满足的稳定码，且每条都有 evidence_refs。
    reason_codes = json.loads(row["reason_codes"])
    assert "AGE_MUTUAL_WITHIN_RANGE" in reason_codes
    assert "INTEREST_OVERLAP" in reason_codes
    evidence = json.loads(row["evidence_json"])
    assert {item["reason_code"] for item in evidence} == set(reason_codes)
    assert evidence[0]["source_revisions"]["viewer"]["profile"] == 3

    # 同一 pair/hash 的重算必须返回并持久化本次请求的 snapshot_id，不能返回
    # 一个未落库的新 ID。
    repeated_id = await write_shadow_snapshot(
        compat_db,
        10,
        42,
        result,
        (viewer_rev, target_rev),
        consent,
        snapshot_id="cp_repeated",
    )
    assert repeated_id == "cp_repeated"
    assert compatibility_store.snapshots[0]["snapshot_id"] == "cp_repeated"


@pytest.mark.asyncio
async def test_read_compatibility_denies_hidden_candidate_with_404_code(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    compatibility_store.seed_snapshot()
    compatibility_store.visibility_rows[42] = {
        "candidate_id": 42,
        "viewer_realname_status": 0,
        "viewer_is_vip": 0,
        "who_can_see_me": 4,
        "account_active": 1,
        "profile_visible": 1,
        "match_active": 1,
        "not_restricted": 1,
        "profile_complete": 1,
        "media_approved": 1,
        "blocked": 0,
    }
    with pytest.raises(CandidateNotVisible) as error:
        await read_compatibility_snapshot(compat_db, 10, 42)
    assert error.value.code == "CANDIDATE_NOT_VISIBLE"


@pytest.mark.asyncio
async def test_read_compatibility_marks_stale_and_persists_on_revision_change(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    """读取路径在 revision 变化时必须落库标记 stale（审查 I-1 修复）。

    修复前 ``_mark_snapshot_stale`` 的 UPDATE 随 ``get_db`` 关闭被回滚，store
    里快照仍为 ``ready``。修复后读路径真正落库：快照在存储里变为 ``stale``，
    这才是"读取时落库标记 stale"的正确语义。
    """
    compatibility_store.seed_snapshot()
    # 目标 profile revision 已推进：旧快照必须落库标 stale，不能当最新解释。
    compatibility_store.revision_rows[42] = RevisionVector(
        profile=6, preference=4, privacy=2, relationship=0, policy=1
    )
    result = await read_compatibility_snapshot(compat_db, 10, 42)
    assert result.status == CompatibilitySnapshotStatus.STALE
    # 修复后读路径真正落库标记 stale，store 里快照状态变为 stale。
    assert compatibility_store.snapshots[0]["status"] == "stale"
    assert compatibility_store.snapshots[0]["invalidated_at"] is not None


def test_get_route_commits_stale_marking_for_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch, compatibility_store: CompatibilityStore
) -> None:
    """GET 路由必须 commit：读取路径的 stale 落库标记才会真实持久化（审查 I-1）。

    修复前 GET handler 未调用 ``db.commit()``，``read_compatibility_snapshot`` 内
    ``_mark_snapshot_stale`` 的 UPDATE 随 ``get_db`` session 关闭被回滚——服务函数
    直接调用（未走路由）的旧测试能通过是因为 fake session 直改 store，暴露不了该
    缺陷。本测试走真实路由，断言读路径 commit 被调用且落库标记生效。
    """
    compatibility_store.seed_snapshot()
    # 目标 profile revision 已推进 → 读取路径执行 _mark_snapshot_stale。
    compatibility_store.revision_rows[42] = RevisionVector(
        profile=6, preference=4, privacy=2, relationship=0, policy=1
    )
    fake_db = CompatibilityFakeSession(compatibility_store)

    async def fake_current_user() -> CurrentUser:
        return CurrentUser(
            id=10,
            session_id=9,
            phone="13800000000",
            status=1,
            realname_status=2,
        )

    def fake_db_dep():
        yield fake_db

    monkeypatch.setattr(settings, "ai_master_enabled", True)
    monkeypatch.setattr(settings, "ai_compatibility_shadow_enabled", True)
    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_db] = fake_db_dep
    try:
        response = client.get("/api/v1/ai/compatibility/42")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)

    # WP-C1 起：stale 属"无可用快照"，路由触发 llm 精算并返回 202+任务。
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == CompatibilitySnapshotStatus.COVERAGE_INSUFFICIENT.value
    assert body["task_id"]
    # 落库标记真实持久化：UPDATE 已执行且路由 commit 被调用。
    assert compatibility_store.snapshots[0]["status"] == "stale"
    assert fake_db.commits >= 1


@pytest.mark.asyncio
async def test_read_compatibility_returns_ready_when_current(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    compatibility_store.seed_snapshot()
    result = await read_compatibility_snapshot(compat_db, 10, 42)
    assert result.status == CompatibilitySnapshotStatus.READY
    assert result.compatibility_index == 78.0
    assert result.algorithm_version == "compatibility-rule-v1"
    assert result.experiment_bucket == "shadow"
    assert result.display_eligible is False
    assert result.disclaimer == DISCLAIMER
    assert result.directions is not None
    assert result.directions.viewer_to_target == 82.0


@pytest.mark.asyncio
async def test_read_compatibility_blocks_when_target_consent_is_revoked(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    compatibility_store.seed_snapshot()
    for consent in compatibility_store.consents:
        if consent["user_id"] == 42 and consent["scope"] == COMPATIBILITY_CONSENT_SCOPE:
            consent["revoked_at"] = _now()
    result = await read_compatibility_snapshot(compat_db, 10, 42)
    assert result.status == CompatibilitySnapshotStatus.BLOCKED
    assert compatibility_store.snapshots[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_read_compatibility_blocks_when_projection_consent_is_revoked(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    compatibility_store.seed_snapshot()
    for consent in compatibility_store.consents:
        if consent["user_id"] == 42 and consent["scope"] == "profile_text_extract":
            consent["revoked_at"] = _now()
    result = await read_compatibility_snapshot(compat_db, 10, 42)
    assert result.status == CompatibilitySnapshotStatus.BLOCKED
    assert compatibility_store.snapshots[0]["status"] == "ready"


@pytest.mark.asyncio
async def test_read_compatibility_blocks_candidate_when_snapshot_blocked(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    compatibility_store.seed_snapshot(status="blocked")
    result = await read_compatibility_snapshot(compat_db, 10, 42)
    assert result.status == CompatibilitySnapshotStatus.BLOCKED
    assert result.compatibility_index is None
    assert result.directions is None


@pytest.mark.asyncio
async def test_read_compatibility_no_snapshot_is_coverage_insufficient(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    result = await read_compatibility_snapshot(compat_db, 10, 42)
    assert result.status == CompatibilitySnapshotStatus.COVERAGE_INSUFFICIENT
    assert result.compatibility_index is None
    assert result.directions is None


# ----------------------------------------------------------------------
# recompute：可见性硬门禁先于版本校验；版本变化 409 RESULT_STALE
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_denies_hidden_candidate(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    compatibility_store.visibility_rows[42] = {
        "candidate_id": 42,
        "viewer_realname_status": 2,
        "viewer_is_vip": 1,
        "who_can_see_me": 1,
        "account_active": 1,
        "profile_visible": 1,
        "match_active": 1,
        "not_restricted": 1,
        "profile_complete": 1,
        "media_approved": 1,
        "blocked": 1,
    }
    with pytest.raises(CandidateNotVisible):
        await request_compatibility_recompute(
            compat_db, 10, 42, 3, 5, "compat-42-01"
        )


@pytest.mark.asyncio
async def test_recompute_rejects_stale_expected_revision(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    with pytest.raises(CompatibilityResultStale):
        await request_compatibility_recompute(
            compat_db, 10, 42, 3, 6, "compat-42-01"
        )


@pytest.mark.asyncio
async def test_recompute_enqueues_compatibility_task(
    compatibility_store: CompatibilityStore,
    compat_db: CompatibilityFakeSession,
) -> None:
    accepted = await request_compatibility_recompute(
        compat_db, 10, 42, 3, 5, "compat-42-01"
    )
    assert accepted.task_id
    assert accepted.snapshot_id.startswith("cp_")
    assert accepted.status == "queued"
    task = compatibility_store.task_store.tasks[accepted.task_id]
    assert task["task_type"] == "compatibility"
    payload = json.loads(task["payload_summary"])
    assert payload["target_user_id"] == 42
    assert payload["snapshot_id"] == accepted.snapshot_id


# ----------------------------------------------------------------------
# 旧推荐卡片标注 legacy-rule-v1（不改变旧 match_score/match_reason 语义）
# ----------------------------------------------------------------------


def test_legacy_discovery_card_is_annotated_legacy_rule_v1() -> None:
    row = {
        "user_id": 1,
        "nickname": "用户",
        "birthday": None,
        "height": 175,
        "education_level": 3,
        "occupation": "工程师",
        "residence_city_code": "310100",
        "income": 20000,
        "same_city": True,
        "is_married": 1,
        "online_status": 1,
        "mbti": "INTJ",
        "interest_tags": '["旅行"]',
        "realname_status": 0,
        "hide_school": 1,
        "hide_company": 1,
        "hide_distance": 1,
        "hide_online_status": 0,
        "is_favorite": 0,
        "is_vip": 0,
        "is_boosted": 0,
    }
    card = _card(row, 50, "资料匹配")
    assert card.match_score == 50
    assert card.match_reason == "资料匹配"
    assert card.algorithm_version == "legacy-rule-v1"
    assert card.match_score_source == "legacy-rule-v1"


# ----------------------------------------------------------------------
# OpenAPI：两个兼容度路径已注册
# ----------------------------------------------------------------------


def test_compatibility_routes_are_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/ai/compatibility/{target_user_id}" in paths
    assert "/api/v1/ai/compatibility/{target_user_id}/recompute" in paths
    recompute = paths["/api/v1/ai/compatibility/{target_user_id}/recompute"]["post"]
    parameters = {item["name"].lower(): item for item in recompute["parameters"]}
    assert parameters["idempotency-key"]["required"] is True


def test_recompute_request_schema_requires_both_revisions() -> None:
    from app.schemas.ai_compatibility import CompatibilityRecomputeRequest

    request = CompatibilityRecomputeRequest(
        expected_viewer_profile_revision=12, expected_target_profile_revision=18
    )
    assert request.expected_viewer_profile_revision == 12
    assert request.expected_target_profile_revision == 18


# ----------------------------------------------------------------------
# 匹配度外显灰度开关（WP-C2 / F11）：写路径 display_eligible 灰度
# ----------------------------------------------------------------------


def test_resolve_display_eligible_modes(monkeypatch):
    """外显灰度（方案 WP-C2/D6）：off 恒 False；on 恒 True；bucket 按稳定哈希命中。"""
    from app.core.config import settings
    from app.services.ai.compatibility import _resolve_display_eligible

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "off")
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 100)
    assert _resolve_display_eligible(12345) is False  # off 优先于 pct

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "on")
    assert _resolve_display_eligible(12345) is True

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "bucket")
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 0)
    assert _resolve_display_eligible(12345) is False

    monkeypatch.setattr(settings, "ai_compatibility_display_mode", "bucket")
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 100)
    assert _resolve_display_eligible(12345) is True

    # 同一用户重复判定必须一致（稳定分桶，不随调用漂移）。
    monkeypatch.setattr(settings, "ai_compatibility_display_bucket_pct", 50)
    first = _resolve_display_eligible(987654321)
    for _ in range(5):
        assert _resolve_display_eligible(987654321) == first
