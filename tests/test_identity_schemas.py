import pytest
from pydantic import ValidationError

from app.schemas.auth import MatchmakerApplicationCreate, MatchmakerReviewRequest, RegistrationIntentUpdate


def test_registration_intent_is_limited_to_supported_values() -> None:
    assert RegistrationIntentUpdate(intent_type="companion").intent_type == "companion"
    with pytest.raises(ValidationError):
        RegistrationIntentUpdate(intent_type="other")


def test_review_requires_reason_for_rejection() -> None:
    with pytest.raises(ValidationError):
        MatchmakerReviewRequest(status=2)
    assert MatchmakerReviewRequest(status=1).status == 1


def test_matchmaker_application_normalizes_legacy_frontend_details() -> None:
    application = MatchmakerApplicationCreate(
        application_type="service_matchmaker",
        real_name="张三",
        phone="13800138000",
        intro="有多年婚恋咨询和沟通经验",
        wechat="matchmaker_demo",
        avatar="/storage/avatar.jpg",
        specialties=["高知青年", "同城牵线"],
        expected_price=199,
        success_cases=[{"description": "脱敏案例", "images": []}],
    )
    assert application.application_details.wechat == "matchmaker_demo"
    assert application.application_details.specialties == ["高知青年", "同城牵线"]
    assert application.application_details.success_cases[0].description == "脱敏案例"
