"""Real MySQL consent transaction, idempotency and revocation checks."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ai_common import AiConsentGrantRequest
from app.services.ai.consents import (
    ConsentError,
    grant_consent,
    list_consents,
    revoke_consent,
)


USER_ID = 9_876_543_210


async def _clean_user(session: AsyncSession) -> None:
    for statement in (
        "DELETE FROM ai_consent_operation WHERE user_id = :user_id",
        "DELETE FROM ai_consent_grant WHERE user_id = :user_id",
        "DELETE FROM ai_task WHERE owner_user_id = :user_id",
        "DELETE FROM derivation_outbox WHERE aggregate_id = :user_id",
        "DELETE FROM user_revision_state WHERE user_id = :user_id",
    ):
        await session.execute(text(statement), {"user_id": USER_ID})
    await session.commit()


@pytest.mark.asyncio
async def test_real_consent_grant_revoke_is_idempotent_and_revision_bound(
    real_db_session: AsyncSession,
) -> None:
    await _clean_user(real_db_session)
    body = AiConsentGrantRequest(
        consent_version="profile-text-v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )

    granted = await grant_consent(
        real_db_session, USER_ID, "profile_text_extract", body, "grant-key-01", 0
    )
    await real_db_session.commit()
    replay = await grant_consent(
        real_db_session, USER_ID, "profile_text_extract", body, "grant-key-01", 0
    )
    assert replay.model_dump(mode="json") == granted.model_dump(mode="json")

    with pytest.raises(ConsentError) as conflict:
        await grant_consent(
            real_db_session,
            USER_ID,
            "profile_text_extract",
            AiConsentGrantRequest(
                consent_version="profile-text-v2",
                policy_revision="ai-policy-2026-08-07-v1",
            ),
            "grant-key-01",
            0,
        )
    assert conflict.value.code == "AI_CONSENT_IDEMPOTENCY_CONFLICT"

    listed = await list_consents(real_db_session, USER_ID)
    assert listed.privacy_revision == 1
    assert [item.scope for item in listed.consents] == ["profile_text_extract"]

    with pytest.raises(ConsentError) as stale:
        await revoke_consent(
            real_db_session, USER_ID, "profile_text_extract", "revoke-key-01", 0
        )
    assert stale.value.code == "AI_CONSENT_VERSION_CONFLICT"

    revoked = await revoke_consent(
        real_db_session, USER_ID, "profile_text_extract", "revoke-key-01", 1
    )
    await real_db_session.commit()
    assert revoked.status == "revoked"
    assert revoked.cleanup_task_id

    revoke_replay = await revoke_consent(
        real_db_session, USER_ID, "profile_text_extract", "revoke-key-01", 0
    )
    assert revoke_replay.model_dump(mode="json") == revoked.model_dump(mode="json")
    after = await list_consents(real_db_session, USER_ID)
    assert after.consents == []
    assert after.privacy_revision == 2

    task = (
        await real_db_session.execute(
            text(
                "SELECT status, payload_summary FROM ai_task "
                "WHERE task_id = :task_id"
            ),
            {"task_id": revoked.cleanup_task_id},
        )
    ).mappings().one()
    assert task["status"] == "queued"
    assert "consent" in str(task["payload_summary"])
    await _clean_user(real_db_session)
