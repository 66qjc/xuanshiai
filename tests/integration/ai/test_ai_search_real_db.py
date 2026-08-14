"""Real MySQL acceptance for the M03 materialize/read boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.search import (
    confirm_search_draft,
    materialize_search_snapshot,
    read_materialized_search_results,
)

OWNER_ID = 9_876_543_221
CANDIDATE_ID = OWNER_ID + 1


async def _clean(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id = :owner)",
        "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id = :owner)",
        "DELETE FROM ai_search_snapshot WHERE user_id = :owner",
        "DELETE FROM ai_search_draft WHERE user_id = :owner",
        "DELETE FROM ai_task WHERE owner_user_id = :owner",
        "DELETE FROM ai_feature_projection WHERE subject_user_id = :candidate",
        "DELETE FROM ai_consent_grant WHERE user_id IN (:owner, :candidate)",
        "DELETE FROM user_revision_state WHERE user_id IN (:owner, :candidate)",
        "DELETE FROM user_profile_completion WHERE user_id IN (:owner, :candidate)",
        "DELETE FROM user_privacy WHERE user_id IN (:owner, :candidate)",
        "DELETE FROM user_auth WHERE user_id IN (:owner, :candidate)",
        "DELETE FROM user_profile WHERE user_id IN (:owner, :candidate)",
        "DELETE FROM users WHERE id IN (:owner, :candidate)",
    ):
        await db.execute(
            text(statement), {"owner": OWNER_ID, "candidate": CANDIDATE_ID}
        )
    await db.commit()


@pytest.mark.asyncio
async def test_real_search_materializes_provenance_and_reads_without_recompute(
    real_db_session: AsyncSession,
) -> None:
    await _clean(real_db_session)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    owner_consent = {
        "scope": "search_parse",
        "version": "search-parse-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    candidate_consent = {
        "scope": "profile_text_extract",
        "version": "profile-text-v1",
        "policy_revision": "ai-policy-2026-08-07-v1",
        "granted_at": now.isoformat(),
    }
    vector = {"profile": 1, "preference": 0, "privacy": 0, "relationship": 0, "policy": 0}
    await real_db_session.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:id, :nickname, :gender, '1990-01-01', 1, 1)"
        ),
        [
            {"id": OWNER_ID, "nickname": "search-owner", "gender": 1},
            {"id": CANDIDATE_ID, "nickname": "search-candidate", "gender": 2},
        ],
    )
    await real_db_session.execute(
        text(
            "INSERT INTO user_profile "
            "(user_id, height, income, occupation, education_level, residence_city_code, "
            "interest_tags, personality_tags, last_active_at) "
            "VALUES (:user_id, 172, 12000, '技术', 4, '330100', :tags, '[]', :active)"
        ),
        {
            "user_id": OWNER_ID,
            "tags": json.dumps(["户外"], ensure_ascii=False),
            "active": now,
        },
    )
    await real_db_session.execute(
        text(
            "INSERT INTO user_profile "
            "(user_id, height, income, occupation, education_level, residence_city_code, "
            "interest_tags, personality_tags, last_active_at) "
            "VALUES (:user_id, 172, 12000, '技术', 4, '330100', :tags, '[]', :active)"
        ),
        {
            "user_id": CANDIDATE_ID,
            "tags": json.dumps(["户外"], ensure_ascii=False),
            "active": now,
        },
    )
    for user_id in (OWNER_ID, CANDIDATE_ID):
        await real_db_session.execute(
            text(
                "INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"
            ),
            {"user_id": user_id},
        )
        await real_db_session.execute(
            text("INSERT INTO user_auth (user_id, realname_status) VALUES (:user_id, 2)"),
            {"user_id": user_id},
        )
        await real_db_session.execute(
            text("INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) VALUES (:user_id, 1, 1, 1)"),
            {"user_id": user_id},
        )
    await real_db_session.execute(
        text(
            "INSERT INTO user_revision_state "
            "(user_id, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision) "
            "VALUES (:user_id, 1, 0, 0, 0, 0)"
        ),
        {"user_id": OWNER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO user_revision_state "
            "(user_id, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision) "
            "VALUES (:user_id, 1, 0, 0, 0, 0)"
        ),
        {"user_id": CANDIDATE_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_consent_grant (user_id, scope, version, policy_revision, granted_at) "
            "VALUES (:user_id, :scope, :version, :policy, :granted_at)"
        ),
        [
            {"user_id": OWNER_ID, "scope": owner_consent["scope"], "version": owner_consent["version"], "policy": owner_consent["policy_revision"], "granted_at": now},
            {"user_id": CANDIDATE_ID, "scope": candidate_consent["scope"], "version": candidate_consent["version"], "policy": candidate_consent["policy_revision"], "granted_at": now},
        ],
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_feature_projection "
            "(subject_user_id, projection_kind, source_hash, projection_version, fields_json, "
            "source_revision_json, profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision, consent_snapshot_json, visibility_class, "
            "status, expires_at) VALUES (:user_id, 'personal_searchable', :source_hash, "
            "'profile-extract-v1', :fields, :source_revision, 1, 0, 0, 0, 0, :consent, "
            "'searchable', 'active', :expires_at)"
        ),
        {
            "user_id": CANDIDATE_ID,
            "source_hash": "real-search-source-hash",
            "fields": json.dumps({"interest_tags": ["户外"]}, ensure_ascii=False),
            "source_revision": json.dumps(vector),
            "consent": json.dumps(candidate_consent),
            "expires_at": now + timedelta(days=1),
        },
    )
    draft_id = "real-search-draft-phase2"
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_draft "
            "(draft_id, user_id, query_text, status, condition_revision, policy_revision, consent_snapshot_json) "
            "VALUES (:draft_id, :user_id, '户外', 'awaiting_confirmation', 0, :policy, :consent)"
        ),
        {
            "draft_id": draft_id,
            "user_id": OWNER_ID,
            "policy": owner_consent["policy_revision"],
            "consent": json.dumps(owner_consent),
        },
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_condition "
            "(draft_id, condition_revision, condition_no, field_key, operator, value_json, "
            "condition_kind, confidence, user_action) VALUES (:draft_id, 0, 0, "
            "'interest_tags', 'contains', :value, 'soft', 1, 'confirmed')"
        ),
        {"draft_id": draft_id, "value": json.dumps("户外", ensure_ascii=False)},
    )
    await real_db_session.commit()

    snapshot = await confirm_search_draft(
        real_db_session,
        draft_id,
        OWNER_ID,
        expected_condition_revision=0,
        idempotency_key="real-search-confirm-phase2",
    )
    await real_db_session.commit()
    materialized = await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    assert materialized.total == 1
    assert materialized.items[0].user_id == CANDIDATE_ID
    await real_db_session.commit()

    read_page = await read_materialized_search_results(
        real_db_session, snapshot.snapshot_id, OWNER_ID, None, 20
    )
    assert [item.user_id for item in read_page.items] == [CANDIDATE_ID]
    stored = (
        await real_db_session.execute(
            text(
                "SELECT projection_id, source_hash, consent_snapshot_json, source_revision_json "
                "FROM ai_search_result WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot.snapshot_id},
        )
    ).mappings().one()
    assert stored["projection_id"] is not None
    assert stored["source_hash"] == "real-search-source-hash"
    assert json.loads(stored["consent_snapshot_json"])["version"] == "profile-text-v1"
    assert json.loads(stored["source_revision_json"]) == vector

    await real_db_session.execute(
        text(
            "UPDATE ai_consent_grant SET revoked_at = UTC_TIMESTAMP() "
            "WHERE user_id = :user_id AND scope = 'profile_text_extract'"
        ),
        {"user_id": CANDIDATE_ID},
    )
    await real_db_session.commit()
    revoked_read = await read_materialized_search_results(
        real_db_session, snapshot.snapshot_id, OWNER_ID, None, 20
    )
    assert revoked_read.items == []

    await _clean(real_db_session)
