from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.main import app
from app.api.dependencies import CurrentUser, get_realname_verified_user
from app.schemas.admin import ReportReviewRequest
from app.schemas.restrictions import RestrictionCreate
from app.schemas.social import ChatMessageResponse


client = TestClient(app)


def test_governance_routes_are_registered() -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/chat/messages/{message_id}/recall" in paths
    assert "/api/v1/admin/users/{user_id}/restrictions" in paths
    assert "/api/v1/admin/users/{user_id}/restrictions/{restriction_id}" in paths


def test_restriction_requires_valid_time_order() -> None:
    with pytest.raises(ValidationError):
        RestrictionCreate(
            restriction_type="TOTAL_BAN",
            reason_code="SERIOUS_VIOLATION",
            reason="严重违规",
            ends_at="2026-08-02T00:00:00Z",
            starts_at="2026-08-03T00:00:00Z",
        )


def test_chat_response_keeps_legacy_revoke_field_and_new_fields() -> None:
    response = ChatMessageResponse(
        id=1,
        session_id=2,
        from_user_id=3,
        to_user_id=4,
        type=1,
        content="消息已撤回",
        media_url=None,
        is_read=True,
        revoked=True,
        is_recalled=True,
        recalled_at="2026-08-03T10:02:00Z",
        created_at="2026-08-03T10:00:00Z",
    )
    assert response.revoked is True
    assert response.is_recalled is True


def test_double_verification_requires_realname_and_face() -> None:
    import asyncio
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        asyncio.run(get_realname_verified_user(CurrentUser(1, 1, "13800000000", 1, 2, 0)))
    assert asyncio.run(get_realname_verified_user(CurrentUser(1, 1, "13800000000", 1, 2, 1))).id == 1


def test_report_restriction_action_requires_explicit_penalty_fields() -> None:
    with pytest.raises(ValidationError):
        ReportReviewRequest(status=1, result="confirmed", action="restrict_user")
    request = ReportReviewRequest(
        status=1,
        result="confirmed",
        action="restrict_user",
        restriction_type="MESSAGE_RESTRICTED",
        restriction_reason_code="HARASSMENT",
    )
    assert request.restriction_type == "MESSAGE_RESTRICTED"
