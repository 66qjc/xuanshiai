"""Real MySQL proof that gateway audit metadata is persisted without raw input."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.ai.audit import GenerationAuditEvent, record_generation_audit
from tests.integration.ai.conftest import TEST_DATABASE_URL


@pytest.mark.asyncio
async def test_generation_audit_is_persisted_and_redacted(
    real_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = f"audit-{uuid4().hex}"
    monkeypatch.setattr(settings, "database_url", TEST_DATABASE_URL)
    monkeypatch.setattr(settings, "ai_audit_enabled", True)

    await record_generation_audit(
        GenerationAuditEvent(
            request_id=request_id,
            task_id="task-audit-1",
            scene="profile_extract",
            provider="mock",
            model="mock-v1",
            prompt_version="prompt-v1",
            schema_version="schema-v1",
            input_revision={"profile": 1, "privacy": 2},
            policy_revision="policy-v1",
            status="succeeded",
            duration_ms=12,
            safety_result={"marker": "must-not-be-raw-input"},
        )
    )
    row = (
        await real_db_session.execute(
            text(
                "SELECT request_id, input_revision_json, safety_result_json "
                "FROM ai_generation_audit WHERE request_id = :request_id"
            ),
            {"request_id": request_id},
        )
    ).mappings().one()
    assert row["request_id"] == request_id
    assert '"profile": 1' in str(row["input_revision_json"])
    assert "must-not-be-raw-input" in str(row["safety_result_json"])
    assert "prompt" not in str(row["safety_result_json"])
    await real_db_session.execute(
        text("DELETE FROM ai_generation_audit WHERE request_id = :request_id"),
        {"request_id": request_id},
    )
    await real_db_session.commit()
