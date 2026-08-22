"""Schema contracts that must hold on the real MySQL bootstrap path."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.ai_schema import AI_TABLES
from tests.integration.ai.conftest import TEST_DATABASE_NAME


async def _table_names(db: AsyncSession) -> set[str]:
    # AI tables are prefixed ``ai_``, but ``voice_transcript`` (registered in
    # AI_TABLES for the voice-transcribe task) carries a ``voice_`` prefix.
    result = await db.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :schema "
            "AND (table_name LIKE 'ai\\_%' OR table_name = 'voice_transcript')"
        ),
        {"schema": TEST_DATABASE_NAME},
    )
    return {str(row[0]) for row in result.all()}


async def _columns(db: AsyncSession, table_name: str) -> set[str]:
    result = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table"
        ),
        {"schema": TEST_DATABASE_NAME, "table": table_name},
    )
    return {str(row[0]) for row in result.all()}


@pytest.mark.asyncio
async def test_real_bootstrap_registers_all_ai_tables(
    real_db_session: AsyncSession,
) -> None:
    assert await _table_names(real_db_session) >= set(AI_TABLES)


@pytest.mark.asyncio
async def test_real_schema_matches_profile_and_result_write_contract(
    real_db_session: AsyncSession,
) -> None:
    required_columns = {
        "ai_consent_grant": {"updated_at"},
        "ai_profile_turn": {"turn_id", "updated_at"},
        "ai_search_result": {
            "projection_id",
            "source_hash",
            "consent_snapshot_json",
            "source_revision_json",
            "updated_at",
        },
        "ai_compatibility_snapshot": {"updated_at"},
    }
    missing = {
        table: sorted(columns - await _columns(real_db_session, table))
        for table, columns in required_columns.items()
    }
    missing = {table: columns for table, columns in missing.items() if columns}
    assert not missing, f"real AI schema is missing required columns: {missing}"


@pytest.mark.asyncio
async def test_real_schema_allows_multiple_closed_profile_sessions(
    real_db_session: AsyncSession,
) -> None:
    """Closing a session must not consume the user's historical-session slot."""

    marker = uuid.uuid4().hex[:12]
    user_id = 9_700_000_000 + int(marker[:6], 16)
    for index in range(2):
        session_id = f"it-{marker}-{index}"
        await real_db_session.execute(
            text(
                "INSERT INTO ai_profile_session "
                "(session_id, user_id, subject, input_mode, status, active_status, "
                "consent_version, policy_revision, profile_revision, preference_revision) "
                "VALUES (:session_id, :user_id, 'personal', 'text', 'draft', 1, "
                "'v1', 'ai-policy-2026-08-07-v1', 0, 0)"
            ),
            {"session_id": session_id, "user_id": user_id},
        )
        await real_db_session.commit()
        await real_db_session.execute(
            text(
                "UPDATE ai_profile_session SET active_status = 0, status = 'cancelled', "
                "ended_at = UTC_TIMESTAMP() WHERE session_id = :session_id"
            ),
            {"session_id": session_id},
        )
        await real_db_session.commit()
