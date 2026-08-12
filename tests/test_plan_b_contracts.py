from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.dependencies import CurrentMatchmakerAdmin
from app.main import app
from app.schemas.matchmaker_admin import MatchmakerAdminAccount
from app.schemas.matchmaker_admin_account import MatchmakerAdminAccountCreate, MatchmakerAdminAccountUpdate
from app.schemas.matchmaker_member_admin import MatchmakerMemberCreate
from app.schemas.meeting import MeetingScheduleCreate
from app.schemas.social import ChatSessionRequestCreate
from app.services import matchmaker_admin_account as account_service


client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]


def _admin(permissions: set[str]) -> CurrentMatchmakerAdmin:
    return CurrentMatchmakerAdmin(
        account=MatchmakerAdminAccount(
            id=1,
            username="admin",
            display_name="管理员",
            matchmaker_user_id=None,
            status=1,
            last_login_at=None,
        ),
        session_id=1,
        permissions=frozenset(permissions),
    )


def test_matchmaker_admin_permission_guard() -> None:
    _admin({"finance.read"}).require("finance.read")
    _admin({"*"}).require("anything")
    with pytest.raises(HTTPException) as exc:
        _admin(set()).require("finance.read")
    assert exc.value.status_code == 403


def test_member_adult_validation_uses_full_date() -> None:
    today = date.today()
    try:
        cutoff = today.replace(year=today.year - 18)
    except ValueError:
        cutoff = today.replace(year=today.year - 18, day=28)
    assert MatchmakerMemberCreate(phone="13800138000", nickname="成年用户", gender=1, birthday=cutoff)
    with pytest.raises(ValueError):
        MatchmakerMemberCreate(
            phone="13800138000",
            nickname="未成年用户",
            gender=1,
            birthday=cutoff + timedelta(days=1),
        )


def test_plan_b_message_and_matchmaker_routes_are_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/admin/matchmaker/applications",
        "/api/v1/admin/matchmaker/applications/{application_id}",
        "/api/v1/chat/sessions/{session_id}/messages/cursor",
        "/api/v1/chat/sessions/{session_id}/pin",
        "/api/v1/chat/sessions/{session_id}/restore",
        "/api/v1/chat/sessions/{session_id}/requests",
        "/api/v1/chat/session-requests/{request_id}",
        "/api/v1/notifications/unread-summary",
    }
    assert expected <= set(paths)


def test_structured_chat_request_contract() -> None:
    assert ChatSessionRequestCreate(request_type="WECHAT").expire_hours == 48
    with pytest.raises(ValueError):
        ChatSessionRequestCreate(request_type="UNKNOWN")


def test_plan_b_database_extensions_are_registered() -> None:
    from app.db.business_schema import BUSINESS_TABLES

    assert {
        "chat_session_request",
    } <= set(BUSINESS_TABLES)


def test_accept_application_writes_system_greeting() -> None:
    source = (ROOT / "app" / "services" / "discovery.py").read_text(encoding="utf-8")
    assert "你们已经互相同意认识，可以开始聊天了。" in source
    assert "type = 6" in source


def test_sensitive_routes_use_permission_guards() -> None:
    files = {
        "identity": ROOT / "app" / "api" / "routes" / "identity.py",
        "accounts": ROOT / "app" / "api" / "routes" / "matchmaker_admin_account.py",
        "finance": ROOT / "app" / "api" / "routes" / "finance.py",
        "meeting": ROOT / "app" / "api" / "routes" / "meeting.py",
    }
    text = {name: path.read_text(encoding="utf-8") for name, path in files.items()}
    assert 'admin.require("matchmaker.application.review")' in text["identity"]
    assert 'current.require("matchmaker.account.manage")' in text["accounts"]
    assert 'admin.require("finance.write")' in text["finance"]
    assert 'admin.require("meeting.write")' in text["meeting"]


@pytest.mark.asyncio
async def test_admin_account_create_persists_data_scope(monkeypatch) -> None:
    result = MagicMock()
    result.scalar.return_value = None
    result.lastrowid = 7
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    expected = object()
    monkeypatch.setattr(account_service, "hash_password", lambda _password: "hashed")
    monkeypatch.setattr(account_service, "get_account", AsyncMock(return_value=expected))

    actual = await account_service.create_account(
        db,
        MatchmakerAdminAccountCreate(
            username="scope_admin",
            password="password123",
            display_name="Scope Admin",
            matchmaker_user_id=12,
            data_scope="ORGANIZATION",
            organization_id=9,
        ),
        actor_id=1,
    )

    insert_call = db.execute.await_args_list[1]
    sql = str(insert_call.args[0])
    params = insert_call.args[1]
    assert "data_scope" in sql and "organization_id" in sql
    assert params["data_scope"] == "ORGANIZATION"
    assert params["organization_id"] == 9
    assert actual is expected


@pytest.mark.asyncio
async def test_admin_account_update_supports_scope_clear_and_unbind(monkeypatch) -> None:
    db = SimpleNamespace(execute=AsyncMock(return_value=MagicMock()), commit=AsyncMock())
    monkeypatch.setattr(account_service, "get_account", AsyncMock(return_value=object()))

    await account_service.update_account(
        db,
        account_id=7,
        body=MatchmakerAdminAccountUpdate(
            matchmaker_user_id=None,
            data_scope="SELF",
            organization_id=None,
        ),
        actor_id=1,
    )

    update_call = db.execute.await_args_list[0]
    sql = str(update_call.args[0])
    params = update_call.args[1]
    assert "matchmaker_user_id = :matchmaker_user_id" in sql
    assert "data_scope = :data_scope" in sql
    assert "organization_id = :organization_id" in sql
    assert params["matchmaker_user_id"] is None
    assert params["data_scope"] == "SELF"
    assert params["organization_id"] is None


@pytest.mark.asyncio
async def test_meeting_schedule_rejects_unbound_matchmaker_admin() -> None:
    from app.api.routes.meeting import schedule

    with pytest.raises(HTTPException) as exc:
        await schedule(
            request_id=1,
            body=MeetingScheduleCreate(
                organizer_id=1,
                scheduled_at="2026-08-13T10:00:00",
                location="Test location",
            ),
            admin=_admin({"meeting.write"}),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 409
