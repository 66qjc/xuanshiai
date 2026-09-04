"""墨相师会话历史 GET 路由回归。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.dependencies import CurrentUser, get_current_user
from app.api.routes import ai_moxiang
from app.db.session import get_db
from app.main import app
from app.schemas.ai_profile import ProfileSubject
from app.services.ai.profile import ProfileSessionNotFound


client = TestClient(app)
_PATH = "/api/v1/ai/profile-sessions/{session_id}/turns"


def test_history_get_is_registered_alongside_turn_submission_post() -> None:
    operation = client.get("/openapi.json").json()["paths"][_PATH]

    assert {"get", "post"}.issubset(operation)
    query_parameters = {
        item["name"]: item for item in operation["get"].get("parameters", [])
    }
    assert query_parameters["limit"]["schema"]["default"] == 50
    assert query_parameters["limit"]["schema"]["minimum"] == 1
    assert query_parameters["limit"]["schema"]["maximum"] == 100
    before_schema = query_parameters["before_turn_no"]["schema"]
    integer_schema = next(
        item for item in before_schema["anyOf"] if item.get("type") == "integer"
    )
    assert integer_schema["minimum"] == 1


def test_history_get_requires_authentication_instead_of_returning_405() -> None:
    response = client.get(
        "/api/v1/ai/profile-sessions/3f678d09a78e418f8bcfa5316c8b1c24/turns",
        params={"limit": 50},
    )

    assert response.status_code == 401


def test_history_get_returns_owned_latest_page(monkeypatch) -> None:
    db = object()
    owner = CurrentUser(
        id=7,
        session_id=11,
        phone="13800000000",
        status=1,
        realname_status=0,
    )
    load_owned = AsyncMock(
        return_value=SimpleNamespace(subject=ProfileSubject.PERSONAL)
    )

    class _Repo:
        def __init__(self, actual_db) -> None:
            assert actual_db is db

        async def list_session_turns(self, session_id, before_turn_no, limit):
            assert session_id == "session_123"
            assert before_turn_no is None
            assert limit == 3  # page size 2 + one row to detect an older page
            return (
                {
                    "turn_id": "t-3",
                    "turn_no": 3,
                    "role": "assistant",
                    "answer_text": "第三轮",
                    "client_turn_id": "assistant-t-3",
                    "created_at": "2026-09-03T10:03:00Z",
                },
                {
                    "turn_id": "t-2",
                    "turn_no": 2,
                    "role": "user",
                    "answer_text": "第二轮",
                    "client_turn_id": "client-t-2",
                    "created_at": "2026-09-03T10:02:00Z",
                },
                {
                    "turn_id": "t-1",
                    "turn_no": 1,
                    "role": "user",
                    "answer_text": "第一轮",
                    "client_turn_id": "client-t-1",
                    "created_at": "2026-09-03T10:01:00Z",
                },
            )

    async def _db_override():
        yield db

    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_db] = _db_override
    monkeypatch.setattr(ai_moxiang, "load_owned_session", load_owned, raising=False)
    monkeypatch.setattr(ai_moxiang, "MoxiangStateSqlRepository", _Repo)
    try:
        response = client.get(
            "/api/v1/ai/profile-sessions/session_123/turns", params={"limit": 2}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "session_123",
        "subject": "personal",
        "turns": [
            {
                "turn_id": "t-2",
                "turn_no": 2,
                "role": "user",
                "answer_text": "第二轮",
                "client_turn_id": "client-t-2",
                "created_at": "2026-09-03T10:02:00Z",
            },
            {
                "turn_id": "t-3",
                "turn_no": 3,
                "role": "assistant",
                "answer_text": "第三轮",
                "client_turn_id": "assistant-t-3",
                "created_at": "2026-09-03T10:03:00Z",
            },
        ],
        "next_before_turn_no": 2,
    }
    load_owned.assert_awaited_once_with(db, "session_123", owner.id)


def test_history_get_hides_foreign_or_missing_session(monkeypatch) -> None:
    owner = CurrentUser(
        id=7,
        session_id=11,
        phone=None,
        status=1,
        realname_status=0,
    )

    async def _db_override():
        yield object()

    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_db] = _db_override
    monkeypatch.setattr(
        ai_moxiang,
        "load_owned_session",
        AsyncMock(side_effect=ProfileSessionNotFound()),
        raising=False,
    )
    try:
        response = client.get("/api/v1/ai/profile-sessions/foreign/turns")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROFILE_SESSION_NOT_FOUND"
