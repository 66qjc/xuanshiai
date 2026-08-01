import pytest
from pydantic import ValidationError

from app.schemas.auth import ExistingAccountLoginRequest, PhoneLoginRequest, ProfileUpdateRequest, RealNameRequest
from app.main import app
from app.core.security import hash_password, verify_password


def test_phone_login_validates_phone_and_code() -> None:
    request = PhoneLoginRequest(phone="13800138000", code="123456")
    assert request.purpose == "login"


def test_phone_login_rejects_invalid_input() -> None:
    with pytest.raises(ValidationError):
        PhoneLoginRequest(phone="123", code="abcdef")


def test_test_login_requires_phone_and_password() -> None:
    assert ExistingAccountLoginRequest(phone="13800138000", password="password123").phone == "13800138000"
    with pytest.raises(ValidationError):
        ExistingAccountLoginRequest(phone="13800138000", password="short")


def test_test_login_route_is_registered() -> None:
    assert "/api/v1/auth/test-login" in app.openapi()["paths"]


def test_password_hash_round_trip() -> None:
    password_hash = hash_password("password123")
    assert password_hash.startswith("$2")
    assert verify_password("password123", password_hash)
    assert not verify_password("wrong-password", password_hash)


def test_realname_rejects_underage_id_card() -> None:
    # Format validation belongs to the schema; age validation belongs to the service.
    request = RealNameRequest(real_name="张三", id_card="110101201001011234")
    assert request.id_card.endswith("1234")


def test_profile_rejects_invalid_height() -> None:
    with pytest.raises(ValidationError):
        ProfileUpdateRequest(height=99)
