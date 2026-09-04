"""Real MySQL proof for the Moxiang journey candidate-worker boundary."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import settings
from app.schemas.ai_common import AiConsentGrantRequest
from app.schemas.ai_profile import (
    ProfileDraftFieldPatchRequest,
    ProfileFieldPatchAction,
    ProfileSubject,
)
from app.services.ai.consents import grant_consent
from app.services.ai.journey import (
    maybe_create_build_invite,
    resolve_journey_invite,
    submit_journey_turn,
)
from app.services.ai.profile import (
    confirm_profile_draft,
    create_master_session,
    publish_profile_draft,
)
from app.services.ai.tasks import get_task
from app.workers import ai_worker

USER_ID = 9_876_543_401
FULL_FLOW_USER_ID = USER_ID + 1
CONSENT_VERSION = "profile-text-v1"
POLICY_REVISION = "ai-policy-2026-08-07-v1"


def _json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        assert isinstance(decoded, list)
        return decoded
    raise AssertionError(f"expected JSON list, got {type(value)!r}")


@pytest.mark.asyncio
async def test_real_moxiang_worker_persists_gateway_candidates_only(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One persisted Moxiang turn must become real candidate rows, not a draft."""
    # The integration suite must stay deterministic even if a developer's
    # local .env selects a real provider.
    monkeypatch.setattr(settings, "ai_provider", "mock")
    await grant_consent(
        real_db_session,
        USER_ID,
        "profile_text_extract",
        AiConsentGrantRequest(
            consent_version=CONSENT_VERSION,
            policy_revision=POLICY_REVISION,
        ),
        "moxiang-worker-grant-1",
        0,
    )
    session = await create_master_session(
        real_db_session,
        USER_ID,
        ProfileSubject.PERSONAL,
        CONSENT_VERSION,
    )
    submission = await submit_journey_turn(
        real_db_session,
        session_id=session.session_id,
        owner_user_id=USER_ID,
        client_turn_id="moxiang-worker-turn-1",
        answer_text="我住杭州，周末喜欢旅行和看展，也想认真结婚。",
    )
    await real_db_session.commit()

    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    monkeypatch.setattr(ai_worker, "session_factory", factory)
    assert await ai_worker._run_round("it-moxiang-worker", 1) == (1, 1, 0)

    async with factory() as verify_db:
        task = await get_task(verify_db, submission.task_id)
        assert task is not None
        assert task.status.value == "succeeded"
        assert task.result_ref == f"moxiang-candidate:{submission.task_id}"

        candidates = (
            await verify_db.execute(
                text(
                    "SELECT session_id, user_id, subject, field_kind, field_key, "
                    "value_json, confidence, source_turn_ids, consent_version, "
                    "policy_revision, status FROM ai_profile_candidate "
                    "WHERE session_id = :session_id ORDER BY id"
                ),
                {"session_id": session.session_id},
            )
        ).mappings().all()
        assert candidates
        assert all(row["user_id"] == USER_ID for row in candidates)
        assert all(row["subject"] == "personal" for row in candidates)
        assert all(row["status"] == "active" for row in candidates)
        assert all(row["consent_version"] == CONSENT_VERSION for row in candidates)
        assert all(row["policy_revision"] == POLICY_REVISION for row in candidates)
        assert all(
            submission.turn.turn_id in _json_list(row["source_turn_ids"])
            for row in candidates
        )
        interest = next(
            row
            for row in candidates
            if row["field_kind"] == "structured" and row["field_key"] == "interest_tags"
        )
        assert _json_list(interest["value_json"]) == ["旅行", "看展"]
        assert float(interest["confidence"]) == pytest.approx(0.91)

        draft_count = (
            await verify_db.execute(
                text("SELECT COUNT(*) FROM ai_profile_draft WHERE user_id = :user_id"),
                {"user_id": USER_ID},
            )
        ).scalar_one()
        assert draft_count == 0


@pytest.mark.asyncio
async def test_real_moxiang_candidates_can_be_confirmed_published_and_projected(
    real_db_session: AsyncSession,
    real_db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journey candidates must reach the same confirmed-published projections as other drafts."""
    monkeypatch.setattr(settings, "ai_provider", "mock")
    await grant_consent(
        real_db_session,
        FULL_FLOW_USER_ID,
        "profile_text_extract",
        AiConsentGrantRequest(
            consent_version=CONSENT_VERSION,
            policy_revision=POLICY_REVISION,
        ),
        "moxiang-full-flow-grant-1",
        0,
    )
    session = await create_master_session(
        real_db_session,
        FULL_FLOW_USER_ID,
        ProfileSubject.PERSONAL,
        CONSENT_VERSION,
    )
    for turn_no, answer in enumerate(
        (
            "我住杭州，周末喜欢旅行和看展。",
            "我从事互联网技术工作，也喜欢户外活动。",
            "我想认真交往，以结婚为目标。",
            "我目前未婚，本科学历，身高一米七二。",
        ),
        start=1,
    ):
        await submit_journey_turn(
            real_db_session,
            session_id=session.session_id,
            owner_user_id=FULL_FLOW_USER_ID,
            client_turn_id=f"moxiang-full-flow-turn-{turn_no}",
            answer_text=answer,
        )
    await real_db_session.commit()

    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    monkeypatch.setattr(ai_worker, "session_factory", factory)
    assert await ai_worker._run_round("it-moxiang-full-candidates", 4) == (4, 4, 0)

    invite = await maybe_create_build_invite(
        real_db_session,
        session_id=session.session_id,
        user_id=FULL_FLOW_USER_ID,
        subject="personal",
    )
    assert invite is not None
    assert invite.effective_turn_count == 4
    assert invite.dimension_count >= 3
    accepted, draft_id = await resolve_journey_invite(
        real_db_session,
        invite_id=invite.invite_id,
        user_id=FULL_FLOW_USER_ID,
        resolution="accepted",
    )
    assert accepted.status == "accepted"
    assert draft_id is not None

    field_rows = (
        await real_db_session.execute(
            text(
                "SELECT field_key FROM ai_profile_draft_field WHERE draft_id = :draft_id "
                "AND field_kind = 'structured' ORDER BY field_key"
            ),
            {"draft_id": draft_id},
        )
    ).scalars().all()
    assert len(field_rows) >= 7
    confirmed = await confirm_profile_draft(
        real_db_session,
        draft_id,
        FULL_FLOW_USER_ID,
        [
            ProfileDraftFieldPatchRequest(
                field_key=str(field_key),
                action=ProfileFieldPatchAction.CONFIRM,
                expected_revision=0,
            )
            for field_key in field_rows
        ],
        expected_revision=0,
        idempotency_key="moxiang-full-flow-confirm-1",
    )
    published = await publish_profile_draft(
        real_db_session,
        draft_id,
        FULL_FLOW_USER_ID,
        expected_revision=confirmed.revision,
        idempotency_key="moxiang-full-flow-publish-1",
    )
    assert published.task_id
    await real_db_session.commit()

    claimed, completed, failed = await ai_worker._run_round(
        "it-moxiang-full-projection", 5
    )
    assert claimed >= 1
    assert completed >= 1
    assert failed == 0

    async with factory() as verify_db:
        projections = (
            await verify_db.execute(
                text(
                    "SELECT projection_kind, status FROM ai_feature_projection "
                    "WHERE subject_user_id = :user_id AND status = 'active'"
                ),
                {"user_id": FULL_FLOW_USER_ID},
            )
        ).mappings().all()
        assert {
            str(row["projection_kind"])
            for row in projections
        } == {"personal_searchable", "personal_compatibility"}
