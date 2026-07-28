import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.services.auth import SmsStore, normalize_user_agent
from app.services.sms.providers import MockSmsProvider
from app.services.wechat.providers import MockWechatProvider


def test_mock_sms_provider_accepts_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "testing")
    monkeypatch.setattr(settings, "sms_mock_code", "123456")

    asyncio.run(MockSmsProvider().send_code("17870810285", "login", "123456"))


def test_mock_sms_provider_rejects_wrong_configured_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "testing")
    monkeypatch.setattr(settings, "sms_mock_code", "123456")

    with pytest.raises(HTTPException):
        asyncio.run(MockSmsProvider().send_code("17870810285", "login", "654321"))


def test_sms_store_uses_mock_code_and_invalidates_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "testing")
    monkeypatch.setattr(settings, "sms_provider", "mock")
    monkeypatch.setattr(settings, "sms_mock_code", "123456")
    store = SmsStore()

    asyncio.run(store.issue("17870810285", "login", "127.0.0.1", "test-device"))
    asyncio.run(store.verify("17870810285", "login", "123456"))

    with pytest.raises(HTTPException):
        asyncio.run(store.verify("17870810285", "login", "123456"))


def test_mock_wechat_provider_maps_code_to_stable_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "testing")
    monkeypatch.setattr(settings, "wechat_mock_openid_prefix", "mock-openid-")

    identity = asyncio.run(MockWechatProvider().exchange_code("mock-code-001"))

    assert identity == {
        "openid": "mock-openid-001",
        "unionid": "mock-unionid-001",
    }


def test_mock_wechat_provider_rejects_non_mock_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "environment", "testing")

    with pytest.raises(HTTPException):
        asyncio.run(MockWechatProvider().exchange_code("real-wechat-code"))


def test_production_rejects_mock_providers() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            sms_provider="mock",
        )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            environment="production",
            wechat_provider="mock",
        )


def test_normalize_user_agent_fits_login_log_device_column() -> None:
    user_agent = "wechat-devtools/" + ("x" * 400)

    normalized = normalize_user_agent(user_agent)

    assert len(normalized) == 255
    assert normalized == user_agent[:255]
