"""Public AI consent route contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.api.dependencies import CurrentUser, get_current_user
from app.api.routes import ai_consents
from app.core.logging import request_id_context
from app.db.session import get_db
from app.main import app
from app.schemas.ai_common import (
    AiConsentListResponse,
    AiConsentOperationResponse,
    AiConsentRead,
)


class _CommitStore:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _setup(store: _CommitStore) -> None:
    async def current_user() -> CurrentUser:
        return CurrentUser(
            id=42,
            session_id=9,
            phone="13800000000",
            status=1,
            realname_status=2,
        )

    def db_override():
        yield store

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = db_override


def _teardown() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)
    request_id_context.set("-")


def test_consent_routes_validate_headers_and_commit_mutations(monkeypatch) -> None:
    store = _CommitStore()
    _setup(store)
    client = TestClient(app)
    granted_at = datetime.now(UTC).replace(tzinfo=None)
    consent = AiConsentRead(
        scope="profile_text_extract",
        version="v1",
        policy_revision="policy-v1",
        granted_at=granted_at,
    )

    async def fake_grant(
        db,
        user_id,
        scope,
        body,
        idempotency_key,
        expected_privacy_revision,
    ):
        assert user_id == 42
        assert scope == "profile_text_extract"
        assert body.consent_version == "v1"
        assert idempotency_key == "consent-key-1"
        assert expected_privacy_revision == 0
        return AiConsentOperationResponse(
            operation_id="grant-op",
            scope="profile_text_extract",
            operation="grant",
            status="active",
            consent=consent,
            privacy_revision=1,
        )

    async def fake_revoke(
        db,
        user_id,
        scope,
        idempotency_key,
        expected_privacy_revision,
    ):
        assert user_id == 42
        assert scope == "profile_text_extract"
        assert idempotency_key == "consent-key-1"
        assert expected_privacy_revision == 1
        return AiConsentOperationResponse(
            operation_id="revoke-op",
            scope="profile_text_extract",
            operation="revoke",
            status="revoked",
            cleanup_task_id="cleanup-op",
            privacy_revision=2,
        )

    monkeypatch.setattr(ai_consents, "grant_consent", fake_grant)
    monkeypatch.setattr(ai_consents, "revoke_consent", fake_revoke)
    headers = {
        "Idempotency-Key": "consent-key-1",
        "X-Expected-Privacy-Revision": "0",
    }
    try:
        granted = client.put(
            "/api/v1/ai/consents/profile_text_extract",
            json={"consent_version": "v1", "policy_revision": "policy-v1"},
            headers=headers,
        )
        assert granted.status_code == 200
        assert granted.json()["privacy_revision"] == 1

        revoked = client.delete(
            "/api/v1/ai/consents/profile_text_extract",
            headers={**headers, "X-Expected-Privacy-Revision": "1"},
        )
        assert revoked.status_code == 202
        assert revoked.json()["cleanup_task_id"] == "cleanup-op"
        assert store.commits == 2

        invalid = client.put(
            "/api/v1/ai/consents/profile_text_extract",
            json={"consent_version": "v1", "policy_revision": "policy-v1"},
            headers={"Idempotency-Key": "short", "X-Expected-Privacy-Revision": "0"},
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "AI_INPUT_INVALID"
    finally:
        _teardown()


def test_get_consents_returns_current_grants(monkeypatch) -> None:
    store = _CommitStore()
    _setup(store)

    async def fake_list(*args, **kwargs):
        return AiConsentListResponse(consents=[], privacy_revision=3)

    monkeypatch.setattr(ai_consents, "list_consents", fake_list)
    try:
        response = TestClient(app).get("/api/v1/ai/consents")
    finally:
        _teardown()

    assert response.status_code == 200
    assert response.json() == {"consents": [], "privacy_revision": 3}
    assert store.commits == 0
