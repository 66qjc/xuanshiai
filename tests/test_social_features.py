import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies import CurrentUser, get_realname_verified_user
from app.api.routes import social as social_routes
from app.main import app
from app.schemas.social import ChatMessageCreate
from app.services import social as social_service


client = TestClient(app)


class RecordingSession:
    def __init__(self, *, active_match: bool = False) -> None:
        self.statements: list[str] = []
        self.active_match = active_match

    async def execute(self, statement: object, _params: object = None) -> "RecordingResult":
        self.statements.append(str(statement))
        return RecordingResult(self.active_match if "SELECT 1 FROM user_match" in str(statement) else None)

    async def commit(self) -> None:
        return None


class RecordingResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar(self) -> object:
        return self.value


def route_dependencies(path: str, method: str) -> set[object]:
    route = next(
        context
        for included in app.routes
        if hasattr(included, "effective_route_contexts")
        for context in included.effective_route_contexts()
        if context.path == path and method in context.methods
    )
    return {dependency.call for dependency in route.dependant.dependencies}


def test_chat_message_validates_message_content() -> None:
    with pytest.raises(ValidationError):
        ChatMessageCreate()
    assert ChatMessageCreate(content="你好").content == "你好"
    assert ChatMessageCreate(type=2, media_url="/storage/chat/a.jpg").type == 2


def test_social_routes_are_registered_and_require_authentication() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/users/{target_id}/like" in paths
    assert "/api/v1/relations/matches" in paths
    assert "/api/v1/chat/sessions/{session_id}/messages" in paths
    assert "/api/v1/chat/sessions" in paths
    assert "/api/v1/notifications" in paths
    assert "/api/v1/security/reports/{target_id}" in paths
    assert "/api/v1/admin/media/{media_id}/review" in paths
    assert "/api/v1/admin/reports/{report_id}/review" in paths
    assert "/api/v1/admin/users/{user_id}/certifications/{kind}/review" in paths

    response = client.get("/api/v1/relations/matches")
    assert response.status_code == 401


def test_social_actions_require_authentication() -> None:
    response = client.put("/api/v1/users/1/like")
    assert response.status_code == 401


def test_social_interactions_and_applications_require_realname() -> None:
    guarded_routes = (
        ("/api/v1/users/{target_id}/like", "PUT"),
        ("/api/v1/users/{target_id}/like", "DELETE"),
        ("/api/v1/users/{target_id}/follow", "PUT"),
        ("/api/v1/users/{target_id}/follow", "DELETE"),
        ("/api/v1/discovery/applications/{target_id}", "POST"),
        ("/api/v1/discovery/applications/{application_id}/accept", "POST"),
        ("/api/v1/discovery/applications/{application_id}/reject", "POST"),
    )
    for path, method in guarded_routes:
        assert get_realname_verified_user in route_dependencies(path, method), (method, path)


def test_reporting_blocking_and_reads_do_not_require_realname() -> None:
    available_routes = (
        ("/api/v1/relations/likes", "GET"),
        ("/api/v1/discovery/applications/incoming", "GET"),
        ("/api/v1/security/blocks/{target_id}", "PUT"),
        ("/api/v1/security/blocks/{target_id}", "DELETE"),
        ("/api/v1/security/reports/{target_id}", "POST"),
    )
    for path, method in available_routes:
        assert get_realname_verified_user not in route_dependencies(path, method), (method, path)


@pytest.mark.asyncio
async def test_liked_by_is_gone_without_revealing_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_if_queried(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("liked-by must not query sender identities")

    monkeypatch.setattr(social_routes, "list_relation", fail_if_queried)
    current = CurrentUser(id=7, session_id=9, phone="13800000000", status=1, realname_status=2)
    with pytest.raises(HTTPException) as exc:
        await social_routes.liked_by(page=1, page_size=20, current=current, db=object())
    assert exc.value.status_code == 410
    assert exc.value.detail == "鍠滄鍒楄〃浠呮湰浜哄彲瑙?"


@pytest.mark.asyncio
async def test_unlike_does_not_revoke_application_match(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_ensure_target(_db: object, _user_id: int, _target_id: int) -> None:
        return None

    monkeypatch.setattr(social_service, "_ensure_target", fake_ensure_target)
    db = RecordingSession()
    response = await social_service.set_like(db, user_id=7, target_id=8, enabled=False)

    assert response.enabled is False
    assert not any("UPDATE user_match" in statement for statement in db.statements)


@pytest.mark.asyncio
async def test_unlike_reports_application_match_that_remains_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_ensure_target(_db: object, _user_id: int, _target_id: int) -> None:
        return None

    monkeypatch.setattr(social_service, "_ensure_target", fake_ensure_target)
    db = RecordingSession(active_match=True)
    response = await social_service.set_like(db, user_id=7, target_id=8, enabled=False)

    assert response.matched is True
    assert not any("UPDATE user_match" in statement for statement in db.statements)
