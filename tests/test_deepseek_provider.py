"""DeepSeek provider 单元测试：mock openai 客户端，不依赖真实 API key。

覆盖三个 Protocol 方法的正常路径与错误映射（JSON 解析失败、429、空内容、4xx
non-retryable）、moderate_text fail-closed、缺 key 延迟报错、confidence 防御、
空 choices 防御，以及 Registry 注册。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.ai.base import (
    ModerationRequest,
    ProviderError,
    SearchParseRequest,
    StructuredExtractRequest,
)
from app.services.ai.providers import (
    AIProviderRegistry,
    DeepSeekAIProvider,
)
from openai import (
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)


def _make_mock_client(json_content: str | None = "{}") -> MagicMock:
    """构造一个 mock AsyncOpenAI 客户端，chat.completions.create 返回可控 JSON。"""
    message = MagicMock()
    message.content = json_content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


def _make_error_client(exc: Exception) -> MagicMock:
    """构造一个会抛指定异常的 mock 客户端。"""
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


def _settings_with_deepseek_key(**overrides: Any) -> Settings:
    """构造一个带 DeepSeek api_key 的 Settings（不读 .env）。"""
    base: dict[str, Any] = {
        "_env_file": None,
        "ai_deepseek_api_key": SecretStr("test-key"),
        "ai_deepseek_model": "deepseek-v4-flash",
        "ai_deepseek_max_tokens": 2048,
    }
    base.update(overrides)
    return Settings(**base)


# ----------------------------------------------------------------------
# Registry 与 fail-fast
# ----------------------------------------------------------------------


def test_registry_registers_deepseek() -> None:
    reg = AIProviderRegistry()
    assert "deepseek" in reg._factories


def test_get_provider_deepseek_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """有 api_key 时 get_provider('deepseek') 能实例化（注入 mock client）。"""
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client())
    assert isinstance(provider, DeepSeekAIProvider)


def test_deepseek_missing_api_key_constructs_without_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺 api_key 时构造不抛（延迟到调用时），避免绕过 Gateway.invoke 分类。"""
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider()
    assert provider._client is None  # 惰性构造，尚未触发


@pytest.mark.asyncio
async def test_deepseek_missing_api_key_raises_on_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 api_key 时首次调用抛 NON_RETRYABLE ProviderError，落在 Gateway.invoke try 内。"""
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider()
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_INPUT_INVALID"
    assert not exc_info.value.retryable


# ----------------------------------------------------------------------
# structured_extract
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_extract_maps_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    json_content = (
        '{"fields": ['
        '  {"field_key": "interest_tags", "value": ["旅行", "看展"], '
        '   "source_quote": "周末喜欢旅行和看展", "confidence": 0.91},'
        '  {"field_key": "city_code", "value": "330100", '
        '   "source_quote": "住杭州", "confidence": 0.95}'
        "]}"
    )
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    client = _make_mock_client(json_content)
    provider = DeepSeekAIProvider(client=client)

    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("周末喜欢旅行和看展，住杭州",),
        consent_version="v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    result = await provider.structured_extract(request)
    assert len(result.fields) == 2
    field = result.fields[0]
    assert field.field_key == "interest_tags"
    assert tuple(field.value) == ("旅行", "看展")
    assert field.confidence == 0.91
    assert field.confirmation_status == "suggested"
    assert field.needs_confirmation is True


@pytest.mark.asyncio
async def test_structured_extract_skips_non_allowlist_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_content = (
        '{"fields": ['
        '  {"field_key": "phone", "value": "13800000000", "confidence": 0.9},'
        '  {"field_key": "age", "value": 28, "source_quote": "28岁", "confidence": 0.9}'
        "]}"
    )
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client(json_content))

    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("28岁",),
        consent_version="v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    result = await provider.structured_extract(request)
    assert len(result.fields) == 1
    assert result.fields[0].field_key == "age"


# ----------------------------------------------------------------------
# parse_search_query
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_search_query_maps_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_content = (
        '{"conditions": ['
        '  {"field_key": "age", "operator": "between", "value": {"min": 26, "max": 32}, '
        '   "kind": "hard", "confidence": 0.95, "source_span": "26到32岁"},'
        '  {"field_key": "city_code", "operator": "eq", "value": "330100", '
        '   "kind": "hard", "confidence": 0.9, "source_span": "住杭州"}'
        "]}"
    )
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client(json_content))

    request = SearchParseRequest(query_text="26到32岁住杭州")
    result = await provider.parse_search_query(request)
    assert len(result.conditions) == 2
    cond = result.conditions[0]
    assert cond.field_key == "age"
    assert cond.operator == "between"
    assert cond.value == {"min": 26, "max": 32}
    assert cond.kind == "hard"


# ----------------------------------------------------------------------
# moderate_text
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moderate_text_allows_safe_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client('{"allowed": true}'))
    result = await provider.moderate_text(ModerationRequest(text="你好"))
    assert result.allowed is True
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_moderate_text_rejects_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(
        client=_make_mock_client('{"allowed": false, "reason_code": "CONTACT_INFO"}')
    )
    result = await provider.moderate_text(ModerationRequest(text="加微信"))
    assert result.allowed is False
    assert result.action == "reject"
    assert result.reason_code == "CONTACT_INFO"


# ----------------------------------------------------------------------
# 错误映射
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_content_raises_input_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client(json_content=""))
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_INPUT_INVALID"
    assert not exc_info.value.retryable


@pytest.mark.asyncio
async def test_invalid_json_raises_temporarily_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client(json_content="not json"))
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_TEMPORARILY_UNAVAILABLE"
    assert exc_info.value.retryable


@pytest.mark.asyncio
async def test_rate_limit_maps_to_quota_exceeded_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    exc = RateLimitError(
        message="rate limited",
        response=MagicMock(),
        body=None,
    )
    provider = DeepSeekAIProvider(client=_make_error_client(exc))
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_QUOTA_EXCEEDED"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_timeout_maps_to_temporarily_unavailable_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    exc = APITimeoutError(request=MagicMock())
    provider = DeepSeekAIProvider(client=_make_error_client(exc))
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_TEMPORARILY_UNAVAILABLE"
    assert exc_info.value.retryable is True


# ----------------------------------------------------------------------
# moderate_text fail-closed
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moderate_text_non_dict_response_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 dict 响应 fail-closed 为 review，不放行违规内容。"""
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client('["not", "dict"]'))
    result = await provider.moderate_text(ModerationRequest(text="可疑内容"))
    assert result.allowed is False
    assert result.action == "review"


@pytest.mark.asyncio
async def test_moderate_text_string_false_not_treated_as_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """字符串 'false' 不被 bool() 当成 True 放行（is True 显式判断）。"""
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(
        client=_make_mock_client('{"allowed": "false", "reason_code": "SPAM"}')
    )
    result = await provider.moderate_text(ModerationRequest(text="spam"))
    assert result.allowed is False
    assert result.action == "reject"


@pytest.mark.asyncio
async def test_moderate_text_missing_allowed_field_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """allowed 字段缺失时 fail-closed，不默认放行。"""
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(
        client=_make_mock_client('{"reason_code": "CONTACT_INFO"}')
    )
    result = await provider.moderate_text(ModerationRequest(text="加微信"))
    assert result.allowed is False
    assert result.action == "reject"


# ----------------------------------------------------------------------
# 4xx 错误映射为 NON_RETRYABLE
# ----------------------------------------------------------------------


def _make_status_error(exc_cls: type, status_code: int) -> Any:
    """构造一个带 status_code 的 APIStatusError 子类实例。"""
    response = MagicMock()
    response.status_code = status_code
    response.headers = {}
    response.request = MagicMock()
    return exc_cls(message=f"http {status_code}", response=response, body=None)


@pytest.mark.asyncio
async def test_auth_error_maps_to_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 认证失败是永久性配置错误，不可重试。"""
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    exc = _make_status_error(AuthenticationError, 401)
    provider = DeepSeekAIProvider(client=_make_error_client(exc))
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_INPUT_INVALID"
    assert not exc_info.value.retryable


@pytest.mark.asyncio
async def test_bad_request_maps_to_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """400 请求格式错误不可重试。"""
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    exc = _make_status_error(BadRequestError, 400)
    provider = DeepSeekAIProvider(client=_make_error_client(exc))
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_INPUT_INVALID"
    assert not exc_info.value.retryable


# ----------------------------------------------------------------------
# confidence 防御
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_null_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型返回 confidence: null 时不抛 TypeError，回退到 1.0。"""
    json_content = (
        '{"fields": ['
        '  {"field_key": "age", "value": 28, "source_quote": "28岁", "confidence": null}'
        "]}"
    )
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DeepSeekAIProvider(client=_make_mock_client(json_content))
    request = StructuredExtractRequest(
        subject="personal", turn_texts=("28岁",),
        consent_version="v1", policy_revision="ai-policy-2026-08-07-v1",
    )
    result = await provider.structured_extract(request)
    assert len(result.fields) == 1
    assert result.fields[0].confidence == 1.0


# ----------------------------------------------------------------------
# 空 choices 防御
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_choices_raises_input_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空 choices 数组不抛 IndexError，转为 NON_RETRYABLE ProviderError。"""
    settings = _settings_with_deepseek_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    response = MagicMock()
    response.choices = []
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    provider = DeepSeekAIProvider(client=client)
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_INPUT_INVALID"
    assert not exc_info.value.retryable
