"""Real MySQL acceptance for synchronous invalidation and physical AI cleanup."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.derivation_outbox import purge_ai_resources

USER_ID = 9_876_543_299


async def _clean(db: AsyncSession) -> None:
    statements = (
        "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id = :user_id)",
        "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id = :user_id)",
        "DELETE FROM ai_search_snapshot WHERE user_id = :user_id",
        "DELETE FROM ai_search_draft WHERE user_id = :user_id",
        "DELETE FROM ai_profile_draft_field WHERE draft_id IN (SELECT draft_id FROM ai_profile_draft WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_draft WHERE user_id = :user_id",
        "DELETE FROM ai_profile_turn WHERE user_id = :user_id",
        "DELETE FROM ai_profile_session WHERE user_id = :user_id",
        "DELETE FROM ai_profile_summary WHERE user_id = :user_id",
        "DELETE FROM ai_profile_revision_field WHERE revision_id IN (SELECT id FROM ai_profile_revision WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_revision WHERE user_id = :user_id",
        "DELETE FROM ai_feature_projection WHERE subject_user_id = :user_id",
        "DELETE FROM ai_compatibility_snapshot WHERE viewer_user_id = :user_id OR target_user_id = :user_id",
        "DELETE FROM ai_task WHERE owner_user_id = :user_id",
    )
    for statement in statements:
        await db.execute(text(statement), {"user_id": USER_ID})
    await db.commit()


@pytest.mark.asyncio
async def test_real_user_cleanup_removes_content_keeps_revision_header(
    real_db_session: AsyncSession,
) -> None:
    await _clean(real_db_session)
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_session "
            "(session_id, user_id, subject, consent_version, policy_revision) "
            "VALUES ('cleanup-session', :user_id, 'personal', 'v1', 'policy-v1')"
        ),
        {"user_id": USER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_turn "
            "(turn_id, session_id, client_turn_id, user_id, turn_no, answer_text) "
            "VALUES ('cleanup-turn', 'cleanup-session', 'client-1', :user_id, 1, 'SECRET_ANSWER')"
        ),
        {"user_id": USER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_draft "
            "(draft_id, user_id, subject, policy_revision) "
            "VALUES ('cleanup-draft', :user_id, 'personal', 'policy-v1')"
        ),
        {"user_id": USER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_draft_field "
            "(draft_id, field_key, subject, value_json, display_value, confidence, content_hash) "
            "VALUES ('cleanup-draft', 'interest_tags', 'personal', :value, 'SECRET_DRAFT', 0.9, :hash)"
        ),
        {"value": json.dumps(["SECRET_DRAFT"]), "hash": "a" * 64},
    )
    revision = await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_revision "
            "(user_id, subject, revision_no, policy_revision) "
            "VALUES (:user_id, 'personal', 1, 'policy-v1')"
        ),
        {"user_id": USER_ID},
    )
    revision_id = revision.lastrowid
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_revision_field "
            "(revision_id, field_key, subject, value_json, display_value, content_hash) "
            "VALUES (:revision_id, 'interest_tags', 'personal', :value, 'SECRET_REVISION', :hash)"
        ),
        {
            "revision_id": revision_id,
            "value": json.dumps(["SECRET_REVISION"]),
            "hash": "b" * 64,
        },
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_summary (user_id, subject, summary_text, content_hash) "
            "VALUES (:user_id, 'personal', 'SECRET_SUMMARY', :hash)"
        ),
        {"user_id": USER_ID, "hash": "c" * 64},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_feature_projection "
            "(subject_user_id, projection_kind, source_hash, projection_version, "
            "fields_json, source_revision_json, consent_snapshot_json, status) "
            "VALUES (:user_id, 'personal_searchable', :hash, 'v1', :fields, :revision, :consent, 'invalidated')"
        ),
        {
            "user_id": USER_ID,
            "hash": "d" * 64,
            "fields": json.dumps({"interest_tags": ["SECRET_PROJECTION"]}),
            "revision": json.dumps({"profile": 1}),
            "consent": json.dumps({"scope": "profile_text_extract"}),
        },
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_draft "
            "(draft_id, user_id, query_text, policy_revision) "
            "VALUES ('cleanup-search-draft', :user_id, 'SECRET_QUERY', 'policy-v1')"
        ),
        {"user_id": USER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_condition "
            "(draft_id, condition_no, field_key, operator, condition_kind) "
            "VALUES ('cleanup-search-draft', 0, 'interest_tags', 'contains', 'soft')"
        )
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_snapshot "
            "(snapshot_id, user_id, snapshot_hash, policy_revision) "
            "VALUES ('cleanup-snapshot', :user_id, :hash, 'policy-v1')"
        ),
        {"user_id": USER_ID, "hash": "e" * 64},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_result "
            "(snapshot_id, target_user_id, rank_position) "
            "VALUES ('cleanup-snapshot', :user_id, 1)"
        ),
        {"user_id": USER_ID},
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_compatibility_snapshot "
            "(snapshot_id, viewer_user_id, target_user_id, snapshot_hash) "
            "VALUES ('cleanup-compat', :user_id, :target_id, :hash)"
        ),
        {"user_id": USER_ID, "target_id": USER_ID + 1, "hash": "f" * 64},
    )
    await real_db_session.commit()

    await purge_ai_resources(
        real_db_session, USER_ID, scope="profile", subject="personal"
    )
    await real_db_session.commit()

    for table in (
        "ai_profile_turn",
        "ai_profile_session",
        "ai_profile_draft",
        "ai_profile_summary",
        "ai_search_draft",
        "ai_search_snapshot",
        "ai_search_result",
        "ai_compatibility_snapshot",
        "ai_feature_projection",
    ):
        count = await real_db_session.scalar(
            text(f"SELECT COUNT(*) FROM {table} WHERE " + (
                "user_id = :user_id" if table not in {"ai_feature_projection", "ai_compatibility_snapshot", "ai_search_result"} else (
                    "subject_user_id = :user_id" if table == "ai_feature_projection" else (
                        "viewer_user_id = :user_id OR target_user_id = :user_id" if table == "ai_compatibility_snapshot" else "target_user_id = :user_id"
                    )
                )
            )),
            {"user_id": USER_ID},
        )
        assert count == 0, table

    field = (
        await real_db_session.execute(
            text(
                "SELECT value_json, display_value, source_turn_ids, source_span, content_hash "
                "FROM ai_profile_revision_field WHERE revision_id = :revision_id"
            ),
            {"revision_id": revision_id},
        )
    ).mappings().one()
    assert all(
        field[column] is None
        for column in ("value_json", "display_value", "source_turn_ids", "source_span")
    )
    assert field["content_hash"] == "b" * 64
    await _clean(real_db_session)
