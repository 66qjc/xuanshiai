"""G2-C 场景 3：删除/撤回后立即重建，旧 cleanup 不得删除新资源（代际 fence）。

真实 MySQL 验收：`purge_ai_resources` 的破坏性 DELETE 只作用于同步半部已标记
的旧代行（draft=deleted、session active_status=0、result stale=1、snapshot
invalidated、compat blocked）；重建后的新行不带标记，即使旧 cleanup 任务晚到
也必须完整保留。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.derivation_outbox import purge_ai_resources

USER_PROFILE_SCOPE = 9_876_544_330
USER_CONSENT_SCOPE = 9_876_544_331


async def _clean(db: AsyncSession, user_id: int) -> None:
    for statement in (
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
        "DELETE FROM ai_consent_grant WHERE user_id = :user_id",
        "DELETE FROM derivation_outbox WHERE aggregate_id = :user_id",
        "DELETE FROM user_revision_state WHERE user_id = :user_id",
    ):
        await db.execute(text(statement), {"user_id": user_id})
    await db.commit()


async def _seed_generation(
    db: AsyncSession, user_id: int, tag: str, *, marked: bool
) -> None:
    """Seed one generation of AI resources; ``marked`` rows carry the soft-delete
    markers the synchronous half writes before the cleanup task runs."""
    session_status = "cancelled" if marked else "draft"
    session_active = 0 if marked else 1
    draft_status = "deleted" if marked else "draft"
    search_draft_status = "invalidated" if marked else "parsing"
    snapshot_status = "invalidated" if marked else "completed"
    snapshot_invalidated = "UTC_TIMESTAMP()" if marked else "NULL"
    result_stale = 1 if marked else 0
    compat_status = "blocked" if marked else "ready"
    projection_status = "invalidated" if marked else "active"

    await db.execute(
        text(
            "INSERT INTO ai_profile_session "
            "(session_id, user_id, subject, consent_version, policy_revision, status, active_status) "
            f"VALUES ('{tag}-session', :user_id, 'personal', 'profile-text-v1', "
            f"'ai-policy-2026-08-07-v1', '{session_status}', {session_active})"
        ),
        {"user_id": user_id},
    )
    await db.execute(
        text(
            "INSERT INTO ai_profile_turn "
            "(turn_id, session_id, client_turn_id, user_id, turn_no, answer_text) "
            f"VALUES ('{tag}-turn', '{tag}-session', '{tag}-client', :user_id, 1, 'answer-{tag}')"
        ),
        {"user_id": user_id},
    )
    await db.execute(
        text(
            "INSERT INTO ai_profile_draft "
            "(draft_id, user_id, subject, session_id, status, policy_revision) "
            f"VALUES ('{tag}-draft', :user_id, 'personal', '{tag}-session', "
            f"'{draft_status}', 'ai-policy-2026-08-07-v1')"
        ),
        {"user_id": user_id},
    )
    await db.execute(
        text(
            "INSERT INTO ai_profile_draft_field "
            "(draft_id, field_key, subject, value_json, display_value, confidence, "
            "content_hash, confirmation_status) "
            f"VALUES ('{tag}-draft', 'interest_tags', 'personal', "
            ":value, 'draft-{tag}', 0.8, :hash, 'suggested')"
        ),
        {"user_id": user_id, "value": json.dumps([f"tag-{tag}"]), "hash": ("a" * 64)},
    )
    await db.execute(
        text(
            "INSERT INTO ai_profile_summary "
            "(user_id, subject, draft_id, summary_text, content_hash) "
            f"VALUES (:user_id, 'personal', '{tag}-draft', 'summary-{tag}', :hash)"
        ),
        {"user_id": user_id, "hash": "b" * 64},
    )
    revision = await db.execute(
        text(
            "INSERT INTO ai_profile_revision "
            "(user_id, subject, revision_no, draft_id, policy_revision) "
            f"VALUES (:user_id, 'personal', :rev_no, '{tag}-draft', 'ai-policy-2026-08-07-v1')"
        ),
        {"user_id": user_id, "rev_no": 1 if marked else 2},
    )
    revision_id = revision.lastrowid
    await db.execute(
        text(
            "INSERT INTO ai_profile_revision_field "
            "(revision_id, field_key, subject, value_json, display_value, content_hash) "
            f"VALUES (:revision_id, 'interest_tags', 'personal', :value, 'rev-{tag}', :hash)"
        ),
        {
            "revision_id": revision_id,
            "user_id": user_id,
            "value": json.dumps([f"rev-{tag}"]),
            "hash": "c" * 64,
        },
    )
    await db.execute(
        text(
            "INSERT INTO ai_feature_projection "
            "(subject_user_id, projection_kind, source_hash, projection_version, "
            "fields_json, source_revision_json, consent_snapshot_json, status) "
            f"VALUES (:user_id, 'personal_searchable', :hash, 'v1', :fields, :revision, :consent, '{projection_status}')"
        ),
        {
            "user_id": user_id,
            "hash": ("d" if marked else "1") * 64,
            "fields": json.dumps({"interest_tags": [f"proj-{tag}"]}),
            "revision": json.dumps({"profile": 1}),
            "consent": json.dumps({"scope": "profile_text_extract"}),
        },
    )
    await db.execute(
        text(
            "INSERT INTO ai_search_draft "
            "(draft_id, user_id, query_text, policy_revision, status) "
            f"VALUES ('{tag}-sd', :user_id, 'query-{tag}', 'ai-policy-2026-08-07-v1', '{search_draft_status}')"
        ),
        {"user_id": user_id},
    )
    await db.execute(
        text(
            "INSERT INTO ai_search_condition "
            "(draft_id, condition_no, field_key, operator, condition_kind) "
            f"VALUES ('{tag}-sd', 0, 'interest_tags', 'contains', 'soft')"
        )
    )
    await db.execute(
        text(
            "INSERT INTO ai_search_snapshot "
            "(snapshot_id, user_id, draft_id, snapshot_hash, policy_revision, status, invalidated_at) "
            f"VALUES ('{tag}-snap', :user_id, '{tag}-sd', :hash, 'ai-policy-2026-08-07-v1', "
            f"'{snapshot_status}', {snapshot_invalidated})"
        ),
        {"user_id": user_id, "hash": "e" * 64},
    )
    await db.execute(
        text(
            "INSERT INTO ai_search_result "
            "(snapshot_id, target_user_id, rank_position, stale) "
            f"VALUES ('{tag}-snap', :target_id, 1, {result_stale})"
        ),
        {"user_id": user_id, "target_id": user_id + 10},
    )
    await db.execute(
        text(
            "INSERT INTO ai_compatibility_snapshot "
            "(snapshot_id, viewer_user_id, target_user_id, snapshot_hash, status) "
            f"VALUES ('{tag}-compat', :user_id, :target_id, :hash, '{compat_status}')"
        ),
        {"user_id": user_id, "target_id": user_id + 10, "hash": ("f" if marked else "2") * 64},
    )
    await db.commit()


async def _assert_old_generation_purged(db: AsyncSession, user_id: int, tag: str) -> None:
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_profile_session WHERE session_id = :sid"),
        {"sid": f"{tag}-session"},
    ) == 0
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_profile_turn WHERE turn_id = :tid"),
        {"tid": f"{tag}-turn"},
    ) == 0
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_profile_draft WHERE draft_id = :did"),
        {"did": f"{tag}-draft"},
    ) == 0
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_profile_summary WHERE draft_id = :did"),
        {"did": f"{tag}-draft"},
    ) == 0
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_search_draft WHERE draft_id = :did"),
        {"did": f"{tag}-sd"},
    ) == 0
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_search_snapshot WHERE snapshot_id = :sid"),
        {"sid": f"{tag}-snap"},
    ) == 0
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_search_result WHERE snapshot_id = :sid"),
        {"sid": f"{tag}-snap"},
    ) == 0
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_compatibility_snapshot WHERE snapshot_id = :sid"),
        {"sid": f"{tag}-compat"},
    ) == 0
    # 旧代 revision 头保留（审计），字段值被抹除。
    header = (
        await db.execute(
            text(
                "SELECT COUNT(*) AS count FROM ai_profile_revision "
                "WHERE user_id = :uid AND draft_id = :did"
            ),
            {"uid": user_id, "did": f"{tag}-draft"},
        )
    ).mappings().one()
    assert int(header["count"]) == 1
    scrubbed = (
        await db.execute(
            text(
                "SELECT value_json, display_value FROM ai_profile_revision_field "
                "WHERE revision_id IN (SELECT id FROM ai_profile_revision "
                "WHERE user_id = :uid AND draft_id = :did)"
            ),
            {"uid": user_id, "did": f"{tag}-draft"},
        )
    ).mappings().one()
    assert scrubbed["value_json"] is None
    assert scrubbed["display_value"] is None


async def _assert_new_generation_survives(db: AsyncSession, user_id: int, tag: str) -> None:
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_profile_session WHERE session_id = :sid"),
        {"sid": f"{tag}-session"},
    ) == 1
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_profile_turn WHERE turn_id = :tid"),
        {"tid": f"{tag}-turn"},
    ) == 1
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_profile_draft WHERE draft_id = :did AND status <> 'deleted'"),
        {"did": f"{tag}-draft"},
    ) == 1
    draft_field = (
        await db.execute(
            text(
                "SELECT value_json FROM ai_profile_draft_field WHERE draft_id = :did"
            ),
            {"did": f"{tag}-draft"},
        )
    ).mappings().one()
    assert draft_field["value_json"] is not None
    summary = (
        await db.execute(
            text("SELECT summary_text FROM ai_profile_summary WHERE user_id = :uid"),
            {"uid": user_id},
        )
    ).mappings().one()
    assert summary["summary_text"] == f"summary-{tag}"
    revision_field = (
        await db.execute(
            text(
                "SELECT value_json FROM ai_profile_revision_field "
                "WHERE revision_id IN (SELECT id FROM ai_profile_revision "
                "WHERE user_id = :uid AND draft_id = :did)"
            ),
            {"uid": user_id, "did": f"{tag}-draft"},
        )
    ).mappings().one()
    assert revision_field["value_json"] is not None
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_search_draft WHERE draft_id = :did AND status = 'parsing'"),
        {"did": f"{tag}-sd"},
    ) == 1
    assert await db.scalar(
        text(
            "SELECT COUNT(*) FROM ai_search_snapshot WHERE snapshot_id = :sid "
            "AND invalidated_at IS NULL AND status = 'completed'"
        ),
        {"sid": f"{tag}-snap"},
    ) == 1
    assert await db.scalar(
        text("SELECT COUNT(*) FROM ai_search_result WHERE snapshot_id = :sid AND stale = 0"),
        {"sid": f"{tag}-snap"},
    ) == 1
    assert await db.scalar(
        text(
            "SELECT COUNT(*) FROM ai_compatibility_snapshot "
            "WHERE snapshot_id = :sid AND status = 'ready'"
        ),
        {"sid": f"{tag}-compat"},
    ) == 1
    assert await db.scalar(
        text(
            "SELECT COUNT(*) FROM ai_feature_projection "
            "WHERE subject_user_id = :uid AND status = 'active'"
        ),
        {"uid": user_id},
    ) == 1


@pytest.mark.asyncio
async def test_profile_delete_purge_spares_rebuilt_resources(
    real_db_session: AsyncSession,
) -> None:
    user_id = USER_PROFILE_SCOPE
    await _clean(real_db_session, user_id)
    await _seed_generation(real_db_session, user_id, "old-profile", marked=True)
    await _seed_generation(real_db_session, user_id, "new-profile", marked=False)

    await purge_ai_resources(
        real_db_session, user_id, scope="profile", subject="personal"
    )
    await real_db_session.commit()

    await _assert_old_generation_purged(real_db_session, user_id, "old-profile")
    await _assert_new_generation_survives(real_db_session, user_id, "new-profile")
    await _clean(real_db_session, user_id)


@pytest.mark.asyncio
async def test_consent_revoke_purge_spares_rebuilt_resources(
    real_db_session: AsyncSession,
) -> None:
    user_id = USER_CONSENT_SCOPE
    await _clean(real_db_session, user_id)
    await _seed_generation(real_db_session, user_id, "old-consent", marked=True)
    await _seed_generation(real_db_session, user_id, "new-consent", marked=False)

    await purge_ai_resources(
        real_db_session, user_id, scope="consent_profile", subject="personal"
    )
    await real_db_session.commit()

    await _assert_old_generation_purged(real_db_session, user_id, "old-consent")
    await _assert_new_generation_survives(real_db_session, user_id, "new-consent")
    await _clean(real_db_session, user_id)
