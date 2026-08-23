import json

import pytest
from pydantic import ValidationError

from app.api.routes import ai_advisor
from app.schemas.ai_advisor import AdvisorAdviceRequest, AdvisorSessionCreate
from app.services.ai_advisor import _normalize_result, _risk_level
from app.services.ai_provider import _mock_response


def test_advisor_routes_are_registered() -> None:
    paths = {route.path: route.methods for route in ai_advisor.router.routes}
    assert paths["/ai/advisor/sessions"] == {"GET"}
    assert paths["/ai/advisor/sessions/{session_id}"] == {"DELETE"}
    assert paths["/ai/advisor/sessions/{session_id}/advice"] == {"POST"}
    assert paths["/ai/advisor/messages/{message_id}/feedback"] == {"POST"}


def test_advisor_request_contracts() -> None:
    request = AdvisorAdviceRequest(
        scenario="reply",
        incoming_message="hello",
        include_history=True,
        chat_session_id=1,
    )
    assert request.max_suggestions == 3
    assert AdvisorSessionCreate().advisor_type == "relationship"
    with pytest.raises(ValidationError):
        AdvisorAdviceRequest(scenario="unknown", incoming_message="hello")
    with pytest.raises(ValidationError):
        AdvisorAdviceRequest(scenario="reply", incoming_message="hello", max_suggestions=4)


def test_advisor_mock_returns_structured_json() -> None:
    raw = _mock_response([{"role": "user", "content": "ADVISOR_ADVICE"}], json_mode=True)
    data = json.loads(raw)
    assert data["suggestions"]
    assert data["risk_level"] == "none"


def test_advisor_result_normalization_limits_suggestions() -> None:
    request = AdvisorAdviceRequest(scenario="reply", incoming_message="hello", max_suggestions=1)
    data = _normalize_result({
        "analysis": "brief analysis",
        "suggestions": [
            {"content": "first", "style": "natural", "reason": "reason"},
            {"content": "second", "style": "warm", "reason": "reason"},
        ],
        "risk_level": "none",
    }, request)
    assert len(data["suggestions"]) == 1


def test_advisor_detects_high_risk_terms() -> None:
    assert _risk_level("\u8bf7\u628a\u9a8c\u8bc1\u7801\u53d1\u7ed9\u6211") == "high"
    assert _risk_level("ordinary conversation") == "none"
