"""Task 11 privacy / block / revoke / delete matrix on real MySQL."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ai_common import AiConsentGrantRequest
from app.schemas.ai_profile import ProfileSubject
from app.schemas.social import BlockRequest
from app.services.ai.compatibility import (
    COMPATIBILITY_CONSENT_SCOPE,
    compatibility_execute_handler,
    read_compatibility_snapshot,
    request_compatibility_recompute,
)
from app.services.ai.consents import grant_consent, list_consents, revoke_consent
from app.services.ai.profile import delete_ai_profile
from app.services.ai.search import (
    confirm_search_draft,
    materialize_search_snapshot,
    read_materialized_search_results,
)
from app.services.ai.tasks import get_task
from app.services.social import set_block
from app.services.ai.compatibility import CandidateNotVisible


POLICY_REVISION = "ai-policy-2026-08-07-v1"
USER_A = 9_876_544_310
USER_B = USER_A + 1


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


async def _cleanup(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id = :a)",
        "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id = :a)",
        "DELETE FROM ai_search_snapshot WHERE user_id = :a",
        "DELETE FROM ai_search_draft WHERE user_id = :a",
        "DELETE FROM ai_compatibility_snapshot WHERE viewer_user_id IN (:a, :b) OR target_user_id IN (:a, :b)",
        "DELETE FROM ai_feature_projection WHERE subject_user_id IN (:a, :b)",
        "DELETE FROM ai_task WHERE owner_user_id IN (:a, :b)",
        "DELETE FROM ai_consent_operation WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_consent_grant WHERE user_id IN (:a, :b)",
        "DELETE FROM derivation_outbox WHERE aggregate_id IN (:a, :b)",
        "DELETE r FROM derivation_consumer_receipt r "
        "JOIN derivation_outbox o ON o.event_id = r.event_id "
        "WHERE o.aggregate_id IN (:a, :b)",
        "DELETE FROM user_block WHERE user_id IN (:a, :b) OR target_user_id IN (:a, :b)",
        "DELETE FROM user_revision_state WHERE user_id IN (:a, :b)",
        "DELETE FROM user_profile_completion WHERE user_id IN (:a, :b)",
        "DELETE FROM user_privacy WHERE user_id IN (:a, :b)",
        "DELETE FROM user_auth WHERE user_id IN (:a, :b)",
        "DELETE FROM user_profile WHERE user_id IN (:a, :b)",
        "DELETE FROM users WHERE id IN (:a, :b)",
    ):
        await db.execute(text(statement), {"a": USER_A, "b": USER_B})
    await db.commit()


async def _grant_scope(
    db: AsyncSession,
    user_id: int,
    scope: str,
    version: str,
    expected_revision: int,
) -> int:
    granted = await grant_consent(
        db,
        user_id,
        scope,
        AiConsentGrantRequest(
            consent_version=version,
            policy_revision=POLICY_REVISION,
        ),
        f"grant-{user_id}-{scope}",
        expected_revision,
    )
    await db.commit()
    return granted.privacy_revision


async def _seed_ready_reads(db: AsyncSession) -> tuple[str, str]:
    await _cleanup(db)
    now = _now()
    await db.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:a, 'privacy-a', 1, '1994-01-01', 1, 1), "
            "(:b, 'privacy-b', 2, '1996-01-01', 1, 1)"
        ),
        {"a": USER_A, "b": USER_B},
    )
    await db.execute(
        text(
            "INSERT INTO user_profile "
            "(user_id, height, income, occupation, education_level, residence_city_code, interest_tags, personality_tags, last_active_at) "
            "VALUES (:user_id, 172, 12000, 'technology', 4, '330100', :tags, '[]', :active)"
        ),
        [
            {
                "user_id": USER_A,
                "tags": json.dumps(["户外"], ensure_ascii=False),
                "active": now,
            },
            {
                "user_id": USER_B,
                "tags": json.dumps(["户外"], ensure_ascii=False),
                "active": now,
            },
        ],
    )
    for user_id in (USER_A, USER_B):
        await db.execute(
            text("INSERT INTO user_profile_completion (user_id, score) VALUES (:user_id, 100)"),
            {"user_id": user_id},
        )
        await db.execute(
            text("INSERT INTO user_auth (user_id, realname_status) VALUES (:user_id, 2)"),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO user_privacy (user_id, show_profile, match_status, who_can_see_me) "
                "VALUES (:user_id, 1, 1, 1)"
            ),
            {"user_id": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO user_revision_state "
                "(user_id, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision) "
                "VALUES (:user_id, 1, 1, 0, 0, 0)"
            ),
            {"user_id": user_id},
        )
    await db.commit()

    privacy_a = 0
    privacy_b = 0
    for scope, version in (
        ("search_parse", "search-parse-v1"),
        ("profile_text_extract", "profile-text-v1"),
        (COMPATIBILITY_CONSENT_SCOPE, "compatibility-shadow-v1"),
    ):
        privacy_a = await _grant_scope(db, USER_A, scope, version, privacy_a)
        privacy_b = await _grant_scope(db, USER_B, scope, version, privacy_b)
    consent_a = {
        item.scope: item for item in (await list_consents(db, USER_A)).consents
    }
    consent_b = {
        item.scope: item for item in (await list_consents(db, USER_B)).consents
    }

    viewer_revision = {
        "profile": 1,
        "preference": 1,
        "privacy": privacy_a,
        "relationship": 0,
        "policy": 0,
    }
    target_revision = {
        "profile": 1,
        "preference": 1,
        "privacy": privacy_b,
        "relationship": 0,
        "policy": 0,
    }
    searchable_fields = {
        "interest_tags": ["户外"],
        "city_code": "330100",
        "education_level": 4,
    }
    compat_fields = {
        "age": 30,
        "city_code": "330100",
        "marriage_status": "single",
        "education_level": 4,
        "height_cm": 172,
        "income_band": 12000,
        "interest_tags": ["旅行"],
        "relationship_goal": "marriage",
    }
    pref_fields = {
        "age": {"min": 26, "max": 32},
        "city_code": ["330100"],
        "marriage_status": ("single",),
        "education_level": {"min": 3},
        "height_cm": {"min": 160, "max": 180},
        "income_band": {"min": 10000},
        "interest_tags": ("旅行",),
        "relationship_goal": ("marriage",),
    }
    for user_id, revision in ((USER_A, viewer_revision), (USER_B, target_revision)):
        await db.execute(
            text(
                "INSERT INTO ai_feature_projection "
                "(subject_user_id, projection_kind, source_hash, projection_version, fields_json, "
                "source_revision_json, profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision, "
                "consent_snapshot_json, visibility_class, status, expires_at) "
                "VALUES (:user_id, 'personal_searchable', :hash, 'profile-extract-v1', :fields, :revision, :profile, :preference, :privacy, :relationship, :policy, :consent, 'searchable', 'active', :expires_at), "
                "(:user_id, 'personal_compatibility', :compat_hash, 'profile-extract-v1', :compat_fields, :revision, :profile, :preference, :privacy, :relationship, :policy, :consent, 'searchable', 'active', :expires_at), "
                "(:user_id, 'ideal_partner_preference', :pref_hash, 'profile-extract-v1', :pref_fields, :revision, :profile, :preference, :privacy, :relationship, :policy, :consent, 'self_only', 'active', :expires_at)"
            ),
            {
                "user_id": user_id,
                "hash": f"searchable-{user_id}",
                "compat_hash": f"compat-{user_id}",
                "pref_hash": f"pref-{user_id}",
                "fields": json.dumps(searchable_fields, ensure_ascii=False),
                "compat_fields": json.dumps(compat_fields, ensure_ascii=False),
                "pref_fields": json.dumps(pref_fields, ensure_ascii=False),
                "revision": json.dumps(revision, ensure_ascii=False),
                "profile": revision["profile"],
                "preference": revision["preference"],
                "privacy": revision["privacy"],
                "relationship": revision["relationship"],
                "policy": revision["policy"],
                "consent": json.dumps(
                    {
                        "scope": "profile_text_extract",
                        "version": consent_a["profile_text_extract"].version
                        if user_id == USER_A
                        else consent_b["profile_text_extract"].version,
                        "policy_revision": POLICY_REVISION,
                        "granted_at": (
                            consent_a["profile_text_extract"].granted_at
                            if user_id == USER_A
                            else consent_b["profile_text_extract"].granted_at
                        ).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                "expires_at": now + timedelta(days=1),
            },
        )

    draft_id = f"privacy-search-{uuid.uuid4().hex[:10]}"
    await db.execute(
        text(
            "INSERT INTO ai_search_draft "
            "(draft_id, user_id, query_text, status, condition_revision, policy_revision, consent_snapshot_json) "
            "VALUES (:draft_id, :user_id, '杭州 本科 26到32岁 户外', 'awaiting_confirmation', 0, :policy, :consent)"
        ),
        {
            "draft_id": draft_id,
            "user_id": USER_A,
            "policy": POLICY_REVISION,
            "consent": json.dumps(
                {
                    "scope": "search_parse",
                    "version": consent_a["search_parse"].version,
                    "policy_revision": POLICY_REVISION,
                    "granted_at": consent_a["search_parse"].granted_at.isoformat(),
                },
                ensure_ascii=False,
            ),
        },
    )
    for no, (field_key, operator, value, kind) in enumerate(
        (
            ("age", "between", {"min": 26, "max": 32}, "hard"),
            ("city_code", "eq", "330100", "hard"),
            ("education_level", "gte", 4, "hard"),
            ("interest_tags", "contains", "户外", "soft"),
        )
    ):
        await db.execute(
            text(
                "INSERT INTO ai_search_condition "
                "(draft_id, condition_revision, condition_no, field_key, operator, value_json, condition_kind, confidence, user_action) "
                "VALUES (:draft_id, 0, :condition_no, :field_key, :operator, :value_json, :kind, 1, 'confirmed')"
            ),
            {
                "draft_id": draft_id,
                "condition_no": no,
                "field_key": field_key,
                "operator": operator,
                "value_json": json.dumps(value, ensure_ascii=False),
                "kind": kind,
            },
        )
    await db.commit()
    search_snapshot = await confirm_search_draft(
        db, draft_id, USER_A, expected_condition_revision=0, idempotency_key="privacy-search-confirm"
    )
    await db.commit()
    await materialize_search_snapshot(db, search_snapshot.snapshot_id, USER_A)
    await db.commit()

    compat = await request_compatibility_recompute(
        db,
        USER_A,
        USER_B,
        expected_viewer_profile_revision=1,
        expected_target_profile_revision=1,
        idempotency_key="privacy-compat",
    )
    await db.commit()
    task = await get_task(db, compat.task_id)
    assert task is not None
    await compatibility_execute_handler(db, task, "privacy-worker")
    await db.commit()
    return search_snapshot.snapshot_id, compat.snapshot_id


@pytest.mark.asyncio
async def test_privacy_matrix_block_revoke_delete_real_db(
    real_db_session: AsyncSession,
) -> None:
    search_snapshot_id, compat_snapshot_id = await _seed_ready_reads(real_db_session)

    visible = await read_materialized_search_results(
        real_db_session, search_snapshot_id, USER_A, None, 20
    )
    assert [item.user_id for item in visible.items] == [USER_B]
    compat_ready = await read_compatibility_snapshot(real_db_session, USER_A, USER_B)
    assert compat_ready.status.value == "ready"

    await set_block(
        real_db_session,
        USER_A,
        USER_B,
        BlockRequest(reason="task11-block"),
        True,
    )
    blocked_search = await read_materialized_search_results(
        real_db_session, search_snapshot_id, USER_A, None, 20
    )
    assert blocked_search.items == []
    # Blocking is a hard visibility boundary: the compatibility endpoint must
    # not reveal that a snapshot exists, so it returns the uniform 404 domain
    # error rather than exposing a ``blocked`` status.
    with pytest.raises(CandidateNotVisible):
        await read_compatibility_snapshot(real_db_session, USER_A, USER_B)
    blocked_status = await real_db_session.scalar(
        text(
            "SELECT status FROM ai_compatibility_snapshot "
            "WHERE snapshot_id = :snapshot_id"
        ),
        {"snapshot_id": compat_snapshot_id},
    )
    assert blocked_status == "ready"

    await _cleanup(real_db_session)
    search_snapshot_id, compat_snapshot_id = await _seed_ready_reads(real_db_session)
    privacy_before = int(
        await real_db_session.scalar(
            text("SELECT privacy_revision FROM user_revision_state WHERE user_id = :user_id"),
            {"user_id": USER_B},
        )
        or 0
    )
    revoked = await revoke_consent(
        real_db_session,
        USER_B,
        "profile_text_extract",
        "privacy-revoke-profile",
        privacy_before,
    )
    await real_db_session.commit()
    assert revoked.cleanup_task_id
    revoked_search = await read_materialized_search_results(
        real_db_session, search_snapshot_id, USER_A, None, 20
    )
    assert revoked_search.items == []
    revoked_compat = await read_compatibility_snapshot(real_db_session, USER_A, USER_B)
    assert revoked_compat.status.value in {"blocked", "stale"}

    await _cleanup(real_db_session)
    search_snapshot_id, compat_snapshot_id = await _seed_ready_reads(real_db_session)
    cleanup = await delete_ai_profile(
        real_db_session,
        USER_B,
        ProfileSubject.PERSONAL,
        "privacy-delete-profile",
    )
    await real_db_session.commit()
    assert cleanup.task_id
    after_delete_search = await read_materialized_search_results(
        real_db_session, search_snapshot_id, USER_A, None, 20
    )
    assert after_delete_search.items == []
    deleted_status = await real_db_session.scalar(
        text(
            "SELECT status FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"
        ),
        {"snapshot_id": compat_snapshot_id},
    )
    assert deleted_status == "blocked"

    await _cleanup(real_db_session)
