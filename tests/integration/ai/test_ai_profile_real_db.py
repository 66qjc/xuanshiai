"""Real MySQL acceptance for the M04 publish -> revision -> projection chain."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.profile import profile_projection_handler, publish_profile_draft
from app.services.ai.tasks import get_task

USER_ID = 9_876_543_211


async def _clean_user(db: AsyncSession) -> None:
    statements = (
        "DELETE FROM ai_feature_projection WHERE subject_user_id = :user_id",
        "DELETE FROM ai_profile_draft_field WHERE draft_id IN "
        "(SELECT draft_id FROM ai_profile_draft WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_draft WHERE user_id = :user_id",
        "DELETE FROM ai_profile_revision_field WHERE revision_id IN "
        "(SELECT id FROM ai_profile_revision WHERE user_id = :user_id)",
        "DELETE FROM ai_profile_revision WHERE user_id = :user_id",
        "DELETE FROM ai_task WHERE owner_user_id = :user_id",
        "DELETE FROM derivation_outbox WHERE aggregate_id = :user_id",
        "DELETE FROM user_revision_state WHERE user_id = :user_id",
        "DELETE FROM ai_consent_grant WHERE user_id = :user_id",
    )
    for statement in statements:
        await db.execute(text(statement), {"user_id": USER_ID})
    await db.commit()


@pytest.mark.asyncio
async def test_real_publish_pins_revision_consent_and_projection(
    real_db_session: AsyncSession,
) -> None:
    await _clean_user(real_db_session)
    # ai_consent_grant.granted_at is a MySQL DATETIME without fractional
    # precision; pin the fixture to the same canonical value used by reads.
    granted_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await real_db_session.execute(
        text(
            "INSERT INTO ai_consent_grant "
            "(user_id, scope, version, policy_revision, granted_at) "
            "VALUES (:user_id, 'profile_text_extract', 'profile-text-v1', "
            "'ai-policy-2026-08-07-v1', :granted_at)"
        ),
        {"user_id": USER_ID, "granted_at": granted_at},
    )
    draft_id = "real-draft-profile-phase2"
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_draft "
            "(draft_id, user_id, subject, status, expected_revision, "
            "consent_snapshot_json, policy_revision, prompt_version, schema_version) "
            "VALUES (:draft_id, :user_id, 'personal', 'draft', 0, :consent, "
            "'ai-policy-2026-08-07-v1', 'profile-extract-prompt-v1', 'profile-extract-v1')"
        ),
        {
            "draft_id": draft_id,
            "user_id": USER_ID,
            "consent": json.dumps(
                {
                    "scope": "profile_text_extract",
                    "version": "profile-text-v1",
                    "policy_revision": "ai-policy-2026-08-07-v1",
                    "granted_at": granted_at.isoformat(),
                },
                ensure_ascii=False,
            ),
        },
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_profile_draft_field "
            "(draft_id, field_key, subject, value_json, display_value, source_type, "
            "source_turn_ids, source_span, confidence, visibility, consent_scope, "
            "schema_version, prompt_version, content_hash, confirmation_status) "
            "VALUES (:draft_id, 'interest_tags', 'personal', :value_json, '旅行', "
            "'user_answer', :turn_ids, '周末喜欢旅行', 0.91, 'self', "
            "'profile_text_extract', 'profile-extract-v1', 'profile-extract-prompt-v1', "
            ":content_hash, 'confirmed')"
        ),
        {
            "draft_id": draft_id,
            "value_json": json.dumps(["旅行"], ensure_ascii=False),
            "turn_ids": json.dumps(["real-turn-phase2"], ensure_ascii=False),
            "content_hash": "a" * 64,
        },
    )
    await real_db_session.commit()

    submission = await publish_profile_draft(
        real_db_session,
        draft_id,
        USER_ID,
        expected_revision=0,
        idempotency_key="real-publish-phase2",
    )
    assert submission.revision is not None
    await real_db_session.commit()

    task = await get_task(real_db_session, submission.task_id)
    assert task is not None
    assert task.source_revision_json == {
        "profile": 1,
        "preference": 0,
        "privacy": 0,
        "relationship": 0,
        "policy": 0,
    }
    assert task.consent_snapshot_json["version"] == "profile-text-v1"
    assert task.payload_summary["published_revision_id"] == submission.revision.revision_id

    revision_field = (
        await real_db_session.execute(
            text(
                "SELECT source_turn_ids, source_span FROM ai_profile_revision_field "
                "WHERE revision_id = :revision_id"
            ),
            {"revision_id": submission.revision.revision_id},
        )
    ).mappings().one()
    assert "real-turn-phase2" in str(revision_field["source_turn_ids"])
    assert revision_field["source_span"] == "周末喜欢旅行"

    result_ref = await profile_projection_handler(
        real_db_session, task, "integration-worker"
    )
    assert result_ref is not None
    await real_db_session.commit()
    projection = (
        await real_db_session.execute(
            text(
                "SELECT projection_kind, fields_json, source_revision_json, "
                "consent_snapshot_json, visibility_class, status "
                "FROM ai_feature_projection WHERE subject_user_id = :user_id "
                "AND projection_kind = 'personal_searchable' AND status = 'active'"
            ),
            {"user_id": USER_ID},
        )
    ).mappings().one()
    assert json.loads(projection["fields_json"]) == {"interest_tags": ["旅行"]}
    assert json.loads(projection["source_revision_json"]) == task.source_revision_json
    assert json.loads(projection["consent_snapshot_json"])["version"] == "profile-text-v1"
    assert projection["visibility_class"] == "searchable"

    await _clean_user(real_db_session)
