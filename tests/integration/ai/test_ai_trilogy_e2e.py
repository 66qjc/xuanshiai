"""Task 11 real-DB AI trilogy end-to-end acceptance."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.schemas.ai_common import AiConsentGrantRequest
from app.schemas.ai_profile import (
    ProfileDraftFieldPatchRequest,
    ProfileFieldPatchAction,
    ProfileSubject,
)
from app.services.ai.compatibility import (
    request_compatibility_recompute,
    read_compatibility_snapshot,
)
from app.services.ai.consents import grant_consent, list_consents
from app.services.ai.profile import (
    confirm_profile_draft,
    create_profile_session,
    publish_profile_draft,
    submit_profile_turn,
)
from app.services.ai.search import confirm_search_draft, read_materialized_search_results
from app.services.ai.tasks import claim_tasks
from app.workers.ai_worker import _process


POLICY_REVISION = "ai-policy-2026-08-07-v1"
PROFILE_SCOPE = ("profile_text_extract", "profile-text-v1")
SEARCH_SCOPE = ("search_parse", "search-parse-v1")
COMPAT_SCOPE = ("compatibility_shadow", "compatibility-shadow-v1")
USER_A = 9_876_544_110
USER_B = USER_A + 1


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


async def _cleanup_pair(db: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_search_snapshot WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_search_draft WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_draft_field WHERE draft_id IN (SELECT draft_id FROM ai_profile_draft WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_profile_draft WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_turn WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_session WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_summary WHERE user_id IN (:a, :b)",
        "DELETE FROM ai_profile_revision_field WHERE revision_id IN (SELECT id FROM ai_profile_revision WHERE user_id IN (:a, :b))",
        "DELETE FROM ai_profile_revision WHERE user_id IN (:a, :b)",
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


async def _seed_visible_users(db: AsyncSession) -> None:
    now = _now()
    await db.execute(
        text(
            "INSERT INTO users (id, nickname, gender, birthday, status, is_married) "
            "VALUES (:id, :nickname, :gender, :birthday, 1, 1)"
        ),
        [
            {
                "id": USER_A,
                "nickname": "task11-a",
                "gender": 1,
                "birthday": "1994-01-01",
            },
            {
                "id": USER_B,
                "nickname": "task11-b",
                "gender": 2,
                "birthday": "1996-01-01",
            },
        ],
    )
    await db.execute(
        text(
            "INSERT INTO user_profile "
            "(user_id, height, income, occupation, education_level, residence_city_code, "
            "interest_tags, personality_tags, last_active_at) "
            "VALUES (:user_id, 172, 12000, 'technology', 4, '330100', :tags, '[]', :active)"
        ),
        [
            {
                "user_id": USER_A,
                "tags": json.dumps(["户外", "旅行"], ensure_ascii=False),
                "active": now,
            },
            {
                "user_id": USER_B,
                "tags": json.dumps(["户外", "旅行"], ensure_ascii=False),
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
                "VALUES (:user_id, 0, 0, 0, 0, 0)"
            ),
            {"user_id": user_id},
        )
    await db.commit()


async def _grant_all_scopes(db: AsyncSession, user_id: int) -> dict[str, dict[str, str]]:
    privacy_revision = 0
    for scope, version in (PROFILE_SCOPE, SEARCH_SCOPE, COMPAT_SCOPE):
        granted = await grant_consent(
            db,
            user_id,
            scope,
            AiConsentGrantRequest(
                consent_version=version,
                policy_revision=POLICY_REVISION,
            ),
            f"grant-{user_id}-{scope}",
            privacy_revision,
        )
        privacy_revision = granted.privacy_revision
        await db.commit()
    listed = await list_consents(db, user_id)
    result = {
        item.scope: {
            "scope": item.scope,
            "version": item.version,
            "policy_revision": item.policy_revision,
            "granted_at": item.granted_at.isoformat(),
        }
        for item in listed.consents
    }
    # ``list_consents`` starts a read transaction. Close it before independent
    # worker sessions build projections; otherwise MySQL REPEATABLE READ can
    # keep this session on a pre-worker snapshot.
    await db.commit()
    return result


async def _run_worker_round(
    factory: async_sessionmaker[AsyncSession],
    worker_id: str,
    *,
    limit: int = 20,
) -> list[str]:
    async with factory() as claim_db:
        claimed = await claim_tasks(claim_db, worker_id, _now(), limit)
        await claim_db.commit()
    for task in claimed:
        await _process(None, task, worker_id, session_provider=factory)
    return [task.task_id for task in claimed]


async def _publish_subject_via_real_session(
    factory: async_sessionmaker[AsyncSession],
    user_id: int,
    subject: ProfileSubject,
) -> None:
    async with factory() as db:
        session = await create_profile_session(
            db,
            user_id,
            subject,
            PROFILE_SCOPE[1],
            f"session-{user_id}-{subject.value}",
        )
        accepted = await submit_profile_turn(
            db,
            session.session_id,
            user_id,
            f"turn-{user_id}-{subject.value}",
            "按固定 mock 夹具生成可确认字段。",
            f"extract-{user_id}-{subject.value}",
        )
        assert accepted.task_id
        await db.commit()

    await _run_worker_round(factory, f"worker-extract-{user_id}-{subject.value}")

    async with factory() as db:
        draft_id = await db.scalar(
            text(
                "SELECT draft_id FROM ai_profile_draft "
                "WHERE user_id = :user_id AND subject = :subject "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject.value},
        )
        assert draft_id
        draft = await db.execute(
            text(
                "SELECT expected_revision FROM ai_profile_draft WHERE draft_id = :draft_id"
            ),
            {"draft_id": draft_id},
        )
        expected_revision = int(draft.scalar_one())
        fields = (
            await db.execute(
                text(
                    "SELECT field_key FROM ai_profile_draft_field "
                    "WHERE draft_id = :draft_id ORDER BY field_key"
                ),
                {"draft_id": draft_id},
            )
        ).scalars().all()
        assert fields
        confirmed = await confirm_profile_draft(
            db,
            str(draft_id),
            user_id,
            [
                ProfileDraftFieldPatchRequest(
                    field_key=str(field_key),
                    action=ProfileFieldPatchAction.CONFIRM,
                    expected_revision=expected_revision,
                )
                for field_key in fields
            ],
            expected_revision=expected_revision,
            idempotency_key=f"confirm-{user_id}-{subject.value}",
        )
        published = await publish_profile_draft(
            db,
            str(draft_id),
            user_id,
            expected_revision=confirmed.revision,
            idempotency_key=f"publish-{user_id}-{subject.value}",
        )
        assert published.task_id
        await db.commit()

    await _run_worker_round(factory, f"worker-projection-{user_id}-{subject.value}")


async def _current_revisions(db: AsyncSession, user_id: int) -> dict[str, int]:
    row = (
        await db.execute(
            text(
                "SELECT profile_revision, preference_revision, privacy_revision, relationship_revision, policy_revision "
                "FROM user_revision_state WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        )
    ).mappings().one()
    return {
        "profile": int(row["profile_revision"] or 0),
        "preference": int(row["preference_revision"] or 0),
        "privacy": int(row["privacy_revision"] or 0),
        "relationship": int(row["relationship_revision"] or 0),
        "policy": int(row["policy_revision"] or 0),
    }


async def _prepare_ready_pair(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> tuple[async_sessionmaker[AsyncSession], dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    await _cleanup_pair(real_db_session)
    await _seed_visible_users(real_db_session)
    consent_a = await _grant_all_scopes(real_db_session, USER_A)
    consent_b = await _grant_all_scopes(real_db_session, USER_B)
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    for user_id in (USER_A, USER_B):
        await _publish_subject_via_real_session(factory, user_id, ProfileSubject.PERSONAL)
        await _publish_subject_via_real_session(factory, user_id, ProfileSubject.IDEAL_PARTNER)
    return factory, consent_a, consent_b


@pytest.mark.asyncio
async def test_ai_trilogy_e2e_real_db_closes_profile_search_compatibility_loop(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    factory, consent_a, consent_b = await _prepare_ready_pair(
        real_db_session, real_db_engine
    )

    projection_rows = (
        await real_db_session.execute(
            text(
                "SELECT subject_user_id, projection_kind, visibility_class, status "
                "FROM ai_feature_projection "
                "WHERE subject_user_id IN (:a, :b) AND status = 'active' "
                "ORDER BY subject_user_id, projection_kind"
            ),
            {"a": USER_A, "b": USER_B},
        )
    ).mappings().all()
    assert {
        (int(row["subject_user_id"]), str(row["projection_kind"]))
        for row in projection_rows
    } == {
        (USER_A, "ideal_partner_preference"),
        (USER_A, "personal_compatibility"),
        (USER_A, "personal_searchable"),
        (USER_B, "ideal_partner_preference"),
        (USER_B, "personal_compatibility"),
        (USER_B, "personal_searchable"),
    }
    assert all(str(row["status"]) == "active" for row in projection_rows)

    draft_id = f"task11-search-{uuid.uuid4().hex[:10]}"
    await real_db_session.execute(
        text(
            "INSERT INTO ai_search_draft "
            "(draft_id, user_id, query_text, status, condition_revision, policy_revision, consent_snapshot_json) "
            "VALUES (:draft_id, :user_id, '杭州 本科 26到32岁 户外', 'awaiting_confirmation', 0, :policy, :consent)"
        ),
        {
            "draft_id": draft_id,
            "user_id": USER_A,
            "policy": POLICY_REVISION,
            "consent": json.dumps(consent_a["search_parse"], ensure_ascii=False),
        },
    )
    conditions = (
        ("age", "between", {"min": 26, "max": 32}, "hard"),
        ("city_code", "eq", "330100", "hard"),
        ("education_level", "gte", 4, "hard"),
        ("interest_tags", "contains", "户外", "soft"),
    )
    for no, (field_key, operator, value, kind) in enumerate(conditions):
        await real_db_session.execute(
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
    await real_db_session.commit()

    search_submission = await confirm_search_draft(
        real_db_session,
        draft_id,
        USER_A,
        expected_condition_revision=0,
        idempotency_key="task11-search-confirm",
    )
    await real_db_session.commit()
    await _run_worker_round(factory, "worker-search")

    page = await read_materialized_search_results(
        real_db_session,
        search_submission.snapshot_id,
        USER_A,
        None,
        20,
    )
    assert page.status == "completed"
    assert [item.user_id for item in page.items] == [USER_B]

    stored_search = (
        await real_db_session.execute(
            text(
                "SELECT target_user_id, projection_id, source_revision_json, consent_snapshot_json "
                "FROM ai_search_result WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": search_submission.snapshot_id},
        )
    ).mappings().one()
    assert int(stored_search["target_user_id"]) == USER_B
    assert stored_search["projection_id"] is not None
    assert json.loads(stored_search["source_revision_json"]) == await _current_revisions(
        real_db_session, USER_B
    )
    assert json.loads(stored_search["consent_snapshot_json"])["scope"] == "profile_text_extract"

    revisions_a = await _current_revisions(real_db_session, USER_A)
    revisions_b = await _current_revisions(real_db_session, USER_B)
    compat = await request_compatibility_recompute(
        real_db_session,
        USER_A,
        USER_B,
        expected_viewer_profile_revision=revisions_a["profile"],
        expected_target_profile_revision=revisions_b["profile"],
        idempotency_key="task11-compat",
    )
    await real_db_session.commit()
    await _run_worker_round(factory, "worker-compat")

    compat_read = await read_compatibility_snapshot(real_db_session, USER_A, USER_B)
    assert compat_read.status.value == "ready"
    assert compat_read.compatibility_index is not None
    assert compat_read.coverage >= 0.5
    assert compat_read.reason_codes
    assert compat_read.direction
    compat_row = (
        await real_db_session.execute(
            text(
                "SELECT compatibility_index, coverage, direction_json, reason_codes, display_eligible "
                "FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": compat.snapshot_id},
        )
    ).mappings().one()
    assert float(compat_row["compatibility_index"]) == compat_read.compatibility_index
    assert float(compat_row["coverage"]) == compat_read.coverage
    assert json.loads(compat_row["direction_json"])
    assert json.loads(compat_row["reason_codes"])
    assert int(compat_row["display_eligible"] or 0) == 0

    await real_db_session.execute(
        text(
            "UPDATE user_privacy SET show_profile = 0 WHERE user_id = :user_id"
        ),
        {"user_id": USER_B},
    )
    await real_db_session.commit()
    hidden_page = await read_materialized_search_results(
        real_db_session,
        search_submission.snapshot_id,
        USER_A,
        None,
        20,
    )
    assert hidden_page.items == []
    hidden_compat = await read_compatibility_snapshot(real_db_session, USER_A, USER_B)
    assert hidden_compat.status.value == "blocked"

    await _cleanup_pair(real_db_session)


@pytest.mark.asyncio
async def test_compatibility_recompute_is_idempotent_under_20_requests_and_two_workers(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
) -> None:
    factory, _, _ = await _prepare_ready_pair(real_db_session, real_db_engine)
    revisions_a = await _current_revisions(real_db_session, USER_A)
    revisions_b = await _current_revisions(real_db_session, USER_B)

    async def request_once() -> tuple[str, str]:
        async with factory() as db:
            accepted = await request_compatibility_recompute(
                db,
                USER_A,
                USER_B,
                expected_viewer_profile_revision=revisions_a["profile"],
                expected_target_profile_revision=revisions_b["profile"],
                idempotency_key="task11-compat-shared",
            )
            await db.commit()
            return accepted.task_id, accepted.snapshot_id

    results = await asyncio.gather(*[request_once() for _ in range(20)])
    assert len({task_id for task_id, _ in results}) == 1
    assert len({snapshot_id for _, snapshot_id in results}) == 1
    shared_task_id, shared_snapshot_id = results[0]

    claimed_a, claimed_b = await asyncio.gather(
        _run_worker_round(factory, "worker-a", limit=1),
        _run_worker_round(factory, "worker-b", limit=1),
    )
    assert sorted((len(claimed_a), len(claimed_b))) == [0, 1]

    task_count = await real_db_session.scalar(
        text(
            "SELECT COUNT(*) FROM ai_task WHERE owner_user_id = :user_id "
            "AND task_type = 'compatibility' AND idempotency_key = :key"
        ),
        {"user_id": USER_A, "key": "task11-compat-shared"},
    )
    assert int(task_count or 0) == 1

    snapshot_count = await real_db_session.scalar(
        text(
            "SELECT COUNT(*) FROM ai_compatibility_snapshot WHERE snapshot_id = :snapshot_id"
        ),
        {"snapshot_id": shared_snapshot_id},
    )
    assert int(snapshot_count or 0) == 1
    status = await real_db_session.scalar(
        text("SELECT status FROM ai_task WHERE task_id = :task_id"),
        {"task_id": shared_task_id},
    )
    assert status == "succeeded"

    await _cleanup_pair(real_db_session)
