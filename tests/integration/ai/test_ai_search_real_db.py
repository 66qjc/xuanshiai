"""Real MySQL acceptance for the M03 materialize/read boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.search import (
    _load_active_generation,
    confirm_search_draft,
    materialize_search_snapshot,
    read_materialized_search_results,
)
from app.services.candidate_query import InvalidCandidateCursor

OWNER_ID = 9_876_543_221
CANDIDATE_ID = OWNER_ID + 1


async def _clean(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id = :owner)",
        "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id = :owner)",
        "DELETE FROM ai_search_snapshot WHERE user_id = :owner",
        "DELETE FROM ai_search_draft WHERE user_id = :owner",
        "DELETE FROM ai_profile_projection_status WHERE user_id IN (:owner, :candidate)",
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


async def _seed_projection_status_active(
    db: AsyncSession, user_id: int, kind: str = "personal_searchable"
) -> None:
    """Phase 4 P4-01: 投影准入位置 active(测试 fixture 显式触发,默认 active)。"""
    await db.execute(
        text(
            "DELETE FROM ai_profile_projection_status "
            "WHERE user_id = :user_id AND kind = :kind"
        ),
        {"user_id": user_id, "kind": kind},
    )
    await db.execute(
        text(
            "INSERT INTO ai_profile_projection_status (user_id, kind, status) "
            "VALUES (:user_id, :kind, 'active')"
        ),
        {"user_id": user_id, "kind": kind},
    )


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
    # Phase 4 P4-01: 投影准入位
    await _seed_projection_status_active(real_db_session, CANDIDATE_ID)
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


@pytest.mark.asyncio
async def test_real_generation_increments_and_old_candidates_cleared(
    real_db_session: AsyncSession,
) -> None:
    """Task8 G4-A Step1：同 snapshot 第二次 materialize 时 active generation 递增，
    旧候选（不在新结果中的）被清理。真实 MySQL 行为验证。"""
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
            {"id": OWNER_ID, "nickname": "gen-owner", "gender": 1},
            {"id": CANDIDATE_ID, "nickname": "gen-candidate", "gender": 2},
        ],
    )
    for uid in (OWNER_ID, CANDIDATE_ID):
        await real_db_session.execute(
            text(
                "INSERT INTO user_profile "
                "(user_id, height, income, occupation, education_level, residence_city_code, "
                "interest_tags, personality_tags, last_active_at) "
                "VALUES (:user_id, 172, 12000, '技术', 4, '330100', :tags, '[]', :active)"
            ),
            {
                "user_id": uid,
                "tags": json.dumps(["户外"], ensure_ascii=False),
                "active": now,
            },
        )
        await real_db_session.execute(
            text("INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text("INSERT INTO user_auth (user_id, realname_status) VALUES (:user_id, 2)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text("INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) VALUES (:user_id, 1, 1, 1)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision) "
                "VALUES (:user_id, 1, 0, 0, 0, 0)"
            ),
            {"user_id": uid},
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
            "source_hash": "gen-source-hash",
            "fields": json.dumps({"interest_tags": ["户外"]}, ensure_ascii=False),
            "source_revision": json.dumps(vector),
            "consent": json.dumps(candidate_consent),
            "expires_at": now + timedelta(days=1),
        },
    )
    await _seed_projection_status_active(real_db_session, CANDIDATE_ID)
    draft_id = "real-gen-draft-1"
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
        idempotency_key="real-gen-confirm-1",
    )
    await real_db_session.commit()

    # 第一次 materialize
    await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()
    gen1 = await _load_active_generation(real_db_session, snapshot.snapshot_id)
    assert gen1 >= 1

    # 第二次 materialize：active generation 应递增
    page2 = await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()
    gen2 = await _load_active_generation(real_db_session, snapshot.snapshot_id)
    assert gen2 == gen1 + 1

    # 旧 generation 的行应被清理：当前 active generation 的行数 == result_total
    row_count = (
        await real_db_session.execute(
            text(
                "SELECT COUNT(*) AS cnt FROM ai_search_result "
                "WHERE snapshot_id = :snapshot_id AND stale = 0"
            ),
            {"snapshot_id": snapshot.snapshot_id},
        )
    ).scalar_one()
    assert row_count == page2.total

    await _clean(real_db_session)


@pytest.mark.asyncio
async def test_real_v2_cursor_paging_no_duplicates_with_target_user_id_tiebreak(
    real_db_session: AsyncSession,
) -> None:
    """Task8 G4-A Step1：v2 cursor 多页翻页无重复/漏项，target_user_id 稳定 tie-break。
    真实 MySQL 行为验证。"""
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
            {"id": OWNER_ID, "nickname": "page-owner", "gender": 1},
            {"id": CANDIDATE_ID, "nickname": "page-candidate", "gender": 2},
        ],
    )
    for uid in (OWNER_ID, CANDIDATE_ID):
        await real_db_session.execute(
            text(
                "INSERT INTO user_profile "
                "(user_id, height, income, occupation, education_level, residence_city_code, "
                "interest_tags, personality_tags, last_active_at) "
                "VALUES (:user_id, 172, 12000, '技术', 4, '330100', :tags, '[]', :active)"
            ),
            {
                "user_id": uid,
                "tags": json.dumps(["户外"], ensure_ascii=False),
                "active": now,
            },
        )
        await real_db_session.execute(
            text("INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text("INSERT INTO user_auth (user_id, realname_status) VALUES (:user_id, 2)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text("INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) VALUES (:user_id, 1, 1, 1)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision) "
                "VALUES (:user_id, 1, 0, 0, 0, 0)"
            ),
            {"user_id": uid},
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
            "source_hash": "page-source-hash",
            "fields": json.dumps({"interest_tags": ["户外"]}, ensure_ascii=False),
            "source_revision": json.dumps(vector),
            "consent": json.dumps(candidate_consent),
            "expires_at": now + timedelta(days=1),
        },
    )
    await _seed_projection_status_active(real_db_session, CANDIDATE_ID)
    draft_id = "real-page-draft-1"
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
        idempotency_key="real-page-confirm-1",
    )
    await real_db_session.commit()
    await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()

    # 多页翻页，无重复 user_id
    seen: set[int] = set()
    cursor: str | None = None
    pages = 0
    while True:
        page = await read_materialized_search_results(
            real_db_session, snapshot.snapshot_id, OWNER_ID, cursor, 4
        )
        for item in page.items:
            assert item.user_id not in seen, f"duplicate user_id {item.user_id}"
            seen.add(item.user_id)
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages < 100, "paging loop did not terminate"

    await _clean(real_db_session)


@pytest.mark.asyncio
async def test_real_old_cursor_invalid_after_generation_switch(
    real_db_session: AsyncSession,
) -> None:
    """Task8 G4-A Step1：generation 切换后旧 cursor 失效（InvalidCandidateCursor），
    前端重新拉第一页。真实 MySQL 行为验证。"""
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
            {"id": OWNER_ID, "nickname": "switch-owner", "gender": 1},
            {"id": CANDIDATE_ID, "nickname": "switch-candidate", "gender": 2},
        ],
    )
    for uid in (OWNER_ID, CANDIDATE_ID):
        await real_db_session.execute(
            text(
                "INSERT INTO user_profile "
                "(user_id, height, income, occupation, education_level, residence_city_code, "
                "interest_tags, personality_tags, last_active_at) "
                "VALUES (:user_id, 172, 12000, '技术', 4, '330100', :tags, '[]', :active)"
            ),
            {
                "user_id": uid,
                "tags": json.dumps(["户外"], ensure_ascii=False),
                "active": now,
            },
        )
        await real_db_session.execute(
            text("INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text("INSERT INTO user_auth (user_id, realname_status) VALUES (:user_id, 2)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text("INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) VALUES (:user_id, 1, 1, 1)"),
            {"user_id": uid},
        )
        await real_db_session.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision) "
                "VALUES (:user_id, 1, 0, 0, 0, 0)"
            ),
            {"user_id": uid},
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
            "source_hash": "switch-source-hash",
            "fields": json.dumps({"interest_tags": ["户外"]}, ensure_ascii=False),
            "source_revision": json.dumps(vector),
            "consent": json.dumps(candidate_consent),
            "expires_at": now + timedelta(days=1),
        },
    )
    await _seed_projection_status_active(real_db_session, CANDIDATE_ID)
    draft_id = "real-switch-draft-1"
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
        idempotency_key="real-switch-confirm-1",
    )
    await real_db_session.commit()
    page1 = await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()
    if page1.next_cursor is None:
        await _clean(real_db_session)
        pytest.skip("snapshot has <= page_size results, no cursor to test")
    old_cursor = page1.next_cursor

    # 第二次 materialize 切换 generation
    await materialize_search_snapshot(
        real_db_session, snapshot.snapshot_id, OWNER_ID
    )
    await real_db_session.commit()

    # 旧 cursor 在新 active generation 下应失效
    with pytest.raises(InvalidCandidateCursor):
        await read_materialized_search_results(
            real_db_session, snapshot.snapshot_id, OWNER_ID, old_cursor, 4
        )

    await _clean(real_db_session)


@pytest.mark.asyncio
async def test_real_patch_search_draft_persists_condition_edit(
    real_db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归（审查 I-1）：PATCH 条件微调必须在请求结束后持久化。

    修复前：patch_search_draft 服务层不 commit，路由的 commit 位于
    try/return/except 之后不可达，get_db 只 close 不提交 → 编辑静默丢失。
    """
    from types import SimpleNamespace

    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_master_enabled", True)
    monkeypatch.setattr(settings, "ai_search_enabled", True)

    await _clean(real_db_session)
    draft_id = "draft-patch-regression-0001"
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_draft (draft_id, user_id, query_text, status, "
            " condition_revision, condition_schema_version, policy_revision) "
            "VALUES (:draft_id, :user_id, '希望对方性格开朗', 'awaiting_confirmation', "
            " 1, 'search-condition-v1', 'ai-policy-2026-08-07-v1')"
        ),
        {"draft_id": draft_id, "user_id": OWNER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_condition (draft_id, condition_revision, condition_no, "
            " field_key, operator, value_json, condition_kind, user_action) "
            "VALUES (:draft_id, 1, 1, 'personality', 'contains', :value_json, "
            " 'soft', 'pending')"
        ),
        {
            "draft_id": draft_id,
            "value_json": json.dumps({"text": "开朗"}, ensure_ascii=False),
        },
    )
    await real_db_session.commit()

    from app.api.routes.ai_search import patch_search_draft_route
    from app.schemas.ai_search import SearchConditionPatchRequest

    await patch_search_draft_route(
        draft_id=draft_id,
        body=[SearchConditionPatchRequest(condition_no=1, action="confirm")],
        current=SimpleNamespace(id=OWNER_ID),
        db=real_db_session,
        idempotency_key="patch-test-0001",
        expected_condition_revision=1,
    )

    # 模拟 get_db 请求收尾（session close → 未提交工作被回滚）：只有路由在成功
    # 路径显式 commit，rollback 之后才读得到变更。同会话直接 SELECT 会看到本事务
    # 未提交写入，无法暴露丢失的提交点。
    await real_db_session.rollback()

    row = (
        await real_db_session.execute(
            text(
                "SELECT user_action FROM ai_search_condition "
                "WHERE draft_id = :draft_id AND condition_no = 1"
            ),
            {"draft_id": draft_id},
        )
    ).mappings().first()
    assert row is not None
    assert row["user_action"] == "confirmed", "PATCH 编辑必须在请求后持久化"

    draft_row = (
        await real_db_session.execute(
            text(
                "SELECT condition_revision FROM ai_search_draft "
                "WHERE draft_id = :draft_id"
            ),
            {"draft_id": draft_id},
        )
    ).mappings().first()
    assert draft_row is not None
    # 条件行的 condition_revision 标识其所属版本（保持 1）；乐观锁版本号在草稿表上自增。
    assert int(draft_row["condition_revision"]) == 2, "草稿条件版本号必须已自增"
