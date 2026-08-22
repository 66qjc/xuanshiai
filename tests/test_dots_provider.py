"""Dots provider 单元测试：mock openai 客户端，不依赖真实 API key。

Dots 与 DeepSeek 共享 ``_OpenAICompatProvider`` 基类，异常映射/解析防御已在
``test_deepseek_provider.py`` 全量覆盖；本文件聚焦 Dots 特有的接线：registry
注册、ai_dots_* 配置解析、客户端 base_url/model 构造、缺 key 延迟报错，以及
经 Dots 子类的共享路径冒烟（structured_extract / moderate fail-closed / 可重
试映射 / ai_model_name 审计元数据）。
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
    StructuredExtractRequest,
)
from app.services.ai.providers import (
    AIProviderRegistry,
    DotsAIProvider,
    get_provider,
)
from openai import RateLimitError


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


def _settings_with_dots_key(**overrides: Any) -> Settings:
    """构造一个带 Dots api_key 的 Settings（不读 .env）。"""
    base: dict[str, Any] = {
        "_env_file": None,
        "ai_dots_api_key": SecretStr("test-dots-key"),
        "ai_dots_base_url": "https://note3-prev-api.askdiandian.com/v1",
        "ai_dots_model": "dots3-note-prev",
        "ai_dots_max_tokens": 4096,
    }
    base.update(overrides)
    return Settings(**base)


# ----------------------------------------------------------------------
# Registry 与配置接线
# ----------------------------------------------------------------------


def test_registry_registers_dots() -> None:
    reg = AIProviderRegistry()
    assert "dots" in reg._factories


def test_get_provider_dots_resolves_dots_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_provider('dots') 从 ai_dots_* 解析 base_url/model/max_tokens。"""
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = get_provider("dots")
    assert isinstance(provider, DotsAIProvider)
    assert provider._base_url == "https://note3-prev-api.askdiandian.com/v1"
    assert provider._model == "dots3-note-prev"
    assert provider._max_tokens == 4096


def test_dots_builds_real_client_with_dots_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """惰性构造的真实 AsyncOpenAI 客户端使用 Dots 的 base_url 与 key。"""
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider()
    assert provider._client is None  # 惰性构造，尚未触发
    client = provider._ensure_client()
    assert str(client.base_url).rstrip("/") == (
        "https://note3-prev-api.askdiandian.com/v1"
    )


def test_dots_missing_api_key_constructs_without_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 api_key 时构造不抛（延迟到调用时），避免绕过 Gateway.invoke 分类。"""
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider()
    assert provider._client is None


@pytest.mark.asyncio
async def test_dots_missing_api_key_raises_on_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 api_key 时首次调用抛 NON_RETRYABLE ProviderError，落在 Gateway.invoke try 内。"""
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider()
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
# 共享路径冒烟（经 Dots 子类）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_structured_extract_maps_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    json_content = (
        '{"fields": ['
        '  {"field_key": "interest_tags", "value": ["旅行", "看展"], '
        '   "source_quote": "周末喜欢旅行和看展", "confidence": 0.91}'
        "]}"
    )
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    client = _make_mock_client(json_content)
    provider = DotsAIProvider(client=client)

    request = StructuredExtractRequest(
        subject="personal",
        turn_texts=("周末喜欢旅行和看展",),
        consent_version="v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )
    result = await provider.structured_extract(request)
    assert len(result.fields) == 1
    field = result.fields[0]
    assert field.field_key == "interest_tags"
    assert tuple(field.value) == ("旅行", "看展")
    assert field.confidence == 0.91
    # 请求参数透传 dots 模型与 max_tokens。
    kwargs = client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == "dots3-note-prev"
    assert kwargs["max_tokens"] == 4096
    assert kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_moderate_text_non_dict_response_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 dict 响应 fail-closed 为 review，不放行违规内容。"""
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider(client=_make_mock_client('["not", "dict"]'))
    result = await provider.moderate_text(ModerationRequest(text="可疑内容"))
    assert result.allowed is False
    assert result.action == "review"


@pytest.mark.asyncio
async def test_rate_limit_maps_to_quota_exceeded_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 经共享异常映射转为 AI_QUOTA_EXCEEDED（可重试）。"""
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    exc = RateLimitError(
        message="rate limited",
        response=MagicMock(),
        body=None,
    )
    provider = DotsAIProvider(client=_make_error_client(exc))
    with pytest.raises(ProviderError) as exc_info:
        await provider.structured_extract(
            StructuredExtractRequest(
                subject="personal", turn_texts=("test",),
                consent_version="v1", policy_revision="v1",
            )
        )
    assert exc_info.value.code == "AI_QUOTA_EXCEEDED"
    assert exc_info.value.retryable is True


def _make_error_client(exc: Exception) -> MagicMock:
    """构造一个会抛指定异常的 mock 客户端。"""
    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=exc)
    return client


# ----------------------------------------------------------------------
# 审计元数据
# ----------------------------------------------------------------------


def test_ai_model_name_returns_dots_model() -> None:
    """ai_provider=dots 时审计元数据返回 ai_dots_model。"""
    settings = _settings_with_dots_key(ai_provider="dots")
    assert settings.ai_model_name == "dots3-note-prev"


# ----------------------------------------------------------------------
# ideal_weights 别名归一化（dots 实测输出 dimension/weight 的防御）
# ----------------------------------------------------------------------


def _narrative_payload_with_alias_weights() -> str:
    """构造 ideal_weights 使用 dimension/weight 别名的叙事 JSON。"""
    return (
        '{"persona_title": "温柔稳定且拥有自己世界的人",'
        ' "persona_tags": ["真诚", "稳定", "有目标"],'
        ' "insight": "你更看重对方在重要时刻的回应，而不是日常的高频陪伴。",'
        ' "dimensions": ['
        '  {"key": "relationship", "icon": "♡", "title": "感情观",'
        '   "summary": "希望建立稳定、长期，但彼此保留个人空间的关系。"},'
        '  {"key": "personality", "icon": "☀", "title": "性格",'
        '   "summary": "期待对方情绪稳定，熟悉以后愿意表达。"},'
        '  {"key": "lifestyle", "icon": "⌂", "title": "生活方式",'
        '   "summary": "希望对方有相对规律、安静、有自己节奏的生活。"},'
        '  {"key": "future", "icon": "↗", "title": "人生规划",'
        '   "summary": "希望另一半也拥有自己的目标与方向。"}],'
        ' "ideal_weights": ['
        '  {"dimension": "价值观", "weight": 30},'
        '  {"dimension": "沟通方式", "weight": 25},'
        '  {"dimension": "情绪稳定", "weight": 20},'
        '  {"dimension": "生活节奏", "weight": 15},'
        '  {"dimension": "外在条件", "weight": 10}],'
        ' "recent_change": null,'
        ' "history_observations": []}'
    )


def _narrative_payload_with_drifts() -> str:
    """构造含三类实测漂移的叙事 JSON（summary 别名/纯文本 recent_change/字符串 revision_id）。"""
    return (
        '{"persona_title": "热爱自然的稳重婚姻追求者",'
        ' "persona_tags": ["成熟稳重", "户外爱好者"],'
        ' "insight": "这是一个位于杭州、热爱爬山摄影的个体，追求稳定的婚姻关系。",'
        ' "dimensions": ['
        '  {"key": "relationship", "icon": "♡", "title": "感情观",'
        '   "summary": "以结婚为明确目标，追求稳定和长期的伴侣关系。"},'
        '  {"key": "personality", "icon": "☀", "title": "性格",'
        '   "string": "可能性格开朗、热爱冒险，从爬山和摄影兴趣中可见其积极向上。"},'
        '  {"key": "lifestyle", "icon": "⌂", "title": "生活方式",'
        '   "summary": "生活方式活跃健康，偏好户外活动如爬山。"},'
        '  {"key": "future", "icon": "↗", "title": "人生规划",'
        '   "summary": "人生规划清晰，以婚姻为核心目标。"}],'
        ' "ideal_weights": [],'
        ' "recent_change": "与上一版本相比，所有关键字段保持一致，无明显变化趋势。",'
        ' "history_observations": ['
        '  {"revision_id": "version2", "keywords": ["年龄", "城市"],'
        '   "observation": "在版本2中，用户画像确认为杭州居民，追求稳定生活。"}]}'
    )


@pytest.mark.asyncio
async def test_narrative_common_drifts_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """summary 写成 string、recent_change 纯文本、revision_id=version2 均归一通过校验。"""
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider(client=_make_mock_client(_narrative_payload_with_drifts()))
    result = await provider.generate_narrative(_narrative_request())
    # personality 维度的 string 被映射为 summary
    personality = [d for d in result.dimensions if d.key == "personality"][0]
    assert "热爱冒险" in personality.summary
    # 纯文本 recent_change 归一为 null
    assert result.recent_change is None
    # "version2" → 2
    assert result.history_observations[0].revision_id == 2


@pytest.mark.asyncio
async def test_narrative_unrecoverable_drift_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归一化覆盖不到的漂移抛可重试 ProviderError，让 Worker 重新生成。"""
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider(
        client=_make_mock_client('{"unexpected": "shape"}')
    )
    with pytest.raises(ProviderError) as exc_info:
        await provider.generate_narrative(_narrative_request())
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_narrative_alias_weights_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dots 输出 {dimension, weight} 时归一为 {key, label, percent}，不抛 ValidationError。"""
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider(
        client=_make_mock_client(_narrative_payload_with_alias_weights())
    )
    request = _narrative_request()
    result = await provider.generate_narrative(request)
    assert [(w.key, w.label, w.percent) for w in result.ideal_weights] == [
        ("values", "价值观", 30),
        ("communication", "沟通方式", 25),
        ("emotion", "情绪稳定", 20),
        ("lifestyle", "生活节奏", 15),
        ("appearance", "外在条件", 10),
    ]


@pytest.mark.asyncio
async def test_narrative_canonical_weights_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已是规范 key/label/percent 的项原样保留，不被归一化改写。"""
    payload = _narrative_payload_with_alias_weights().replace(
        '{"dimension": "价值观", "weight": 30}',
        '{"key": "values", "label": "价值观", "percent": 88}',
    )
    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider(client=_make_mock_client(payload))
    result = await provider.generate_narrative(_narrative_request())
    assert result.ideal_weights[0].key == "values"
    assert result.ideal_weights[0].percent == 88


def _narrative_request() -> Any:
    from app.services.ai.base import NarrativeRequest

    return NarrativeRequest(
        subject="ideal_partner",
        current_fields=({"field_key": "age", "value": {"min": 26, "max": 33},
                         "display_value": "26-33岁"},),
        previous_fields=(),
        history_summaries=(),
        consent_version="v1",
        policy_revision="ai-policy-2026-08-07-v1",
    )


# ----------------------------------------------------------------------
# generate_reply（语音对话回复生成）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_reply_returns_reply_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_reply 解析 {"reply": "..."} JSON 并返回 ReplyResult。"""
    from app.services.ai.base import ReplyRequest

    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    json_content = '{"reply_text": "收到，28岁啦。你平时喜欢做什么呀？"}'
    provider = DotsAIProvider(client=_make_mock_client(json_content))

    request = ReplyRequest(
        transcript="我今年28岁",
        field_key="age",
        known_fields=({"field_key": "age", "value": 28},),
    )
    result = await provider.generate_reply(request)
    assert result.reply_text == "收到，28岁啦。你平时喜欢做什么呀？"


@pytest.mark.asyncio
async def test_generate_reply_empty_reply_raises_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空回复触发 schema 校验失败，抛可重试 ProviderError。"""
    from app.services.ai.base import ReplyRequest

    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider(client=_make_mock_client('{"reply_text": ""}'))

    request = ReplyRequest(transcript="test", field_key="age")
    with pytest.raises(ProviderError) as exc_info:
        await provider.generate_reply(request)
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_generate_reply_non_dict_raises_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 dict 响应无法通过 ReplyResult 校验，抛可重试 ProviderError。"""
    from app.services.ai.base import ReplyRequest

    settings = _settings_with_dots_key()
    monkeypatch.setattr("app.services.ai.providers.settings", settings)
    provider = DotsAIProvider(client=_make_mock_client('["not", "dict"]'))

    request = ReplyRequest(transcript="test", field_key="age")
    with pytest.raises(ProviderError) as exc_info:
        await provider.generate_reply(request)
    assert exc_info.value.retryable is True
