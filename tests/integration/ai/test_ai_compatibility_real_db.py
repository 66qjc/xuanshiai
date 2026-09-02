"""Real MySQL acceptance for the M06 compatibility shadow boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ai_compatibility import CompatibilitySnapshotStatus
from app.schemas.discovery import DiscoveryFilters
from app.services.ai.compatibility import (
    COMPATIBILITY_CONSENT_SCOPE,
    compatibility_execute_handler,
    read_compatibility_snapshot,
    request_compatibility_recompute,
)
from app.services.ai.tasks import get_task
from app.services.discovery import get_discovery_page

VIEWER_ID = 9_876_543_231
TARGET_ID = VIEWER_ID + 1


async def _clean(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_compatibility_snapshot WHERE viewer_user_id = :viewer OR target_user_id = :target",
        "DELETE FROM ai_task WHERE owner_user_id IN (:viewer, :target)",
        "DELETE FROM ai_feature_projection WHERE subject_user_id IN (:viewer, :target)",
        "DELETE FROM ai_profile_projection_status WHERE user_id IN (:viewer, :target)",
        "DELETE FROM ai_consent_grant WHERE user_id IN (:viewer, :target)",
        "DELETE FROM user_revision_state WHERE user_id IN (:viewer, :target)",
        "DELETE FROM user_profile_completion WHERE user_id IN (:viewer, :target)",
        "DELETE FROM user_privacy WHERE user_id IN (:viewer, :target)",
        "DELETE FROM user_auth WHERE user_id IN (:viewer, :target)",
        "DELETE FROM user_profile WHERE user_id IN (:viewer, :target)",
        "DELETE FROM users WHERE id IN (:viewer, :target)",
    ):
        await db.execute(statement=text(statement), params={"viewer": VIEWER_ID, "target": TARGET_ID})
    await db.commit()


@pytest.mark.asyncio
async def test_real_compatibility_persists_pair_provenance_and_blocks_revocation(
    real_db_session: AsyncSession,
) -> None:
    await _clean(real_db_session)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    vector_viewer = {
        "profile": 3,
        "preference": 2,
        "privacy": 1,
        "relationship": 0,
        "policy": 1,
    }
    vector_target = {
        "profile": 5,
        "preference": 4,
        "privacy": 2,
        "relationship": 0,
        "policy": 1,
    }
    profile_consent = {
        "scope": "profile_text_extract",
        "version": "profile-text-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    compatibility_consent = {
        "scope": COMPATIBILITY_CONSENT_SCOPE,
        "version": "compatibility-shadow-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    await real_db_session.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:viewer, 'compat-viewer', 1, '1990-01-01', 1, 1), "
            "(:target, 'compat-target', 2, '1992-01-01', 1, 1)"
        ),
        {"viewer": VIEWER_ID, "target": TARGET_ID},
    )
    for user_id in (VIEWER_ID, TARGET_ID):
        await real_db_session.execute(
            text(
                "INSERT INTO user_profile (user_id, height, income, occupation, "
                "education_level, residence_city_code, interest_tags, personality_tags, last_active_at) "
                "VALUES (:user_id, 172, 12000, 'engineer', 4, '330100', :tags, '[]', :active)"
            ),
            {
                "user_id": user_id,
                "tags": json.dumps(["travel"], ensure_ascii=False),
                "active": now,
            },
        )
        await real_db_session.execute(
            text("INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"),
            {"user_id": user_id},
        )
        await real_db_session.execute(
            text("INSERT INTO user_auth (user_id, realname_status) VALUES (:user_id, 2)"),
            {"user_id": user_id},
        )
        await real_db_session.execute(
            text(
                "INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) "
                "VALUES (:user_id, 1, 1, 1)"
            ),
            {"user_id": user_id},
        )
        vector = vector_viewer if user_id == VIEWER_ID else vector_target
        await real_db_session.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, "
                "relationship_revision, policy_revision) VALUES (:user_id, :profile, :preference, "
                ":privacy, :relationship, :policy)"
            ),
            {"user_id": user_id, **vector},
        )
        await real_db_session.execute(
            text(
                "INSERT INTO ai_consent_grant (user_id, scope, version, policy_revision, granted_at) "
                "VALUES (:user_id, :scope, :version, :policy, :granted_at)"
            ),
            [
                {
                    "user_id": user_id,
                    "scope": profile_consent["scope"],
                    "version": profile_consent["version"],
                    "policy": profile_consent["policy_revision"],
                    "granted_at": now,
                },
                {
                    "user_id": user_id,
                    "scope": compatibility_consent["scope"],
                    "version": compatibility_consent["version"],
                    "policy": compatibility_consent["policy_revision"],
                    "granted_at": now,
                },
            ],
        )

    projection_fields = {
        "age": 30,
        "city_code": "330100",
        "marriage_status": "single",
        "education_level": 4,
        "height_cm": 172,
        "income_band": 12000,
        "interest_tags": ["travel"],
        "relationship_goal": "marriage",
    }
    preference_fields = {
        "age": {"min": 25, "max": 40},
        "city_code": ["330100"],
        "marriage_status": "single",
        "education_level": {"min": 3},
        "height_cm": {"min": 160, "max": 185},
        "income_band": {"min": 8000},
        "interest_tags": ["travel"],
        "relationship_goal": "marriage",
    }
    for user_id, vector in ((VIEWER_ID, vector_viewer), (TARGET_ID, vector_target)):
        for kind, fields, visibility in (
            ("personal_compatibility", projection_fields, "searchable"),
            ("ideal_partner_preference", preference_fields, "self_only"),
        ):
            await real_db_session.execute(
                text(
                    "INSERT INTO ai_feature_projection "
                    "(subject_user_id, projection_kind, source_hash, projection_version, fields_json, "
                    "source_revision_json, profile_revision, preference_revision, privacy_revision, "
                    "relationship_revision, policy_revision, consent_snapshot_json, visibility_class, "
                    "status, expires_at) VALUES (:user_id, :kind, :source_hash, 'profile-extract-v1', "
                    ":fields, :source_revision, :profile, :preference, :privacy, :relationship, :policy, "
                    ":consent, :visibility, 'active', :expires_at)"
                ),
                {
                    "user_id": user_id,
                    "kind": kind,
                    "source_hash": f"compat-{user_id}-{kind}",
                    "fields": json.dumps(fields, ensure_ascii=False),
                    "source_revision": json.dumps(vector),
                    **vector,
                    "consent": json.dumps(profile_consent, ensure_ascii=False),
                    "visibility": visibility,
                    "expires_at": now + timedelta(days=1),
                },
            )
            # Phase 4 P4-01: 投影准入位(测试 fixture 显式触发,默认 active)
            await real_db_session.execute(
                text(
                    "DELETE FROM ai_profile_projection_status "
                    "WHERE user_id = :user_id AND kind = :kind"
                ),
                {"user_id": user_id, "kind": kind},
            )
            await real_db_session.execute(
                text(
                    "INSERT INTO ai_profile_projection_status "
                    "(user_id, kind, status, source_revision) "
                    "VALUES (:user_id, :kind, 'active', 0)"
                ),
                {"user_id": user_id, "kind": kind},
            )
    await real_db_session.commit()

    accepted = await request_compatibility_recompute(
        real_db_session,
        VIEWER_ID,
        TARGET_ID,
        expected_viewer_profile_revision=vector_viewer["profile"],
        expected_target_profile_revision=vector_target["profile"],
        idempotency_key="real-compat-handler-phase2",
    )
    await real_db_session.commit()
    task = await get_task(real_db_session, accepted.task_id)
    assert task is not None
    handler_result = await compatibility_execute_handler(
        real_db_session, task, "integration-worker"
    )
    assert handler_result is not None
    assert handler_result[0] == f"compatibility-snapshot:{accepted.snapshot_id}"
    await real_db_session.commit()
    snapshot_id = accepted.snapshot_id
    handler_snapshot = (
        await real_db_session.execute(
            text(
                "SELECT status, source_revision_pair_json, consent_snapshot_pair_json "
                "FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": accepted.snapshot_id},
        )
    ).mappings().one()
    assert handler_snapshot["status"] == "ready"
    assert json.loads(handler_snapshot["source_revision_pair_json"])["viewer"] == vector_viewer
    assert json.loads(handler_snapshot["source_revision_pair_json"])["target"] == vector_target
    assert json.loads(handler_snapshot["consent_snapshot_pair_json"])["viewer"]["version"] == "compatibility-shadow-v1"

    # G4-B shadow 守卫：写入恒定 display_eligible=0 + shadow 桶 + shadow 语义，
    # 算法版本恒为 compatibility-rule-v1（后端保证不外显可读新兼容度）。
    shadow_row = (
        await real_db_session.execute(
            text(
                "SELECT display_eligible, experiment_bucket, score_semantics, algorithm_version "
                "FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": accepted.snapshot_id},
        )
    ).mappings().one()
    assert int(shadow_row["display_eligible"] or 0) == 0
    assert shadow_row["experiment_bucket"] == "shadow"
    assert shadow_row["score_semantics"] == "rule_based_reference_shadow"
    assert shadow_row["algorithm_version"] == "compatibility-rule-v1"

    ready = await read_compatibility_snapshot(real_db_session, VIEWER_ID, TARGET_ID)
    assert ready.status == CompatibilitySnapshotStatus.READY
    assert ready.display_eligible is False

    # legacy 分数语义不被 shadow 写入破坏：旧推荐流卡片仍恒为 legacy-rule-v1。
    legacy_page = await get_discovery_page(
        real_db_session, VIEWER_ID, DiscoveryFilters(), plaza=False
    )
    assert legacy_page.items, "shadow 写入后旧推荐流仍应产出候选卡片"
    assert all(
        card.algorithm_version == "legacy-rule-v1"
        and card.match_score_source == "legacy-rule-v1"
        for card in legacy_page.items
    )

    await real_db_session.execute(
        text(
            "UPDATE ai_consent_grant SET revoked_at = UTC_TIMESTAMP() "
            "WHERE user_id = :target AND scope = :scope"
        ),
        {"target": TARGET_ID, "scope": COMPATIBILITY_CONSENT_SCOPE},
    )
    await real_db_session.commit()
    blocked = await read_compatibility_snapshot(real_db_session, VIEWER_ID, TARGET_ID)
    assert blocked.status == CompatibilitySnapshotStatus.BLOCKED
    status = (
        await real_db_session.execute(
            text("SELECT status FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"),
            {"snapshot_id": snapshot_id},
        )
    ).scalar_one()
    assert status == "ready"

    await _clean(real_db_session)
