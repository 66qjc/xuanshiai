"""WP-C1b compare_compatibility 调用链单测：Mock/OpenAI 兼容 provider + Gateway。

不触 DB：只验证确定性 fixture、failure 注入、归一化与 schema 漂移到
RETRYABLE ProviderError 的转换、Gateway 层错误分类。
"""

from __future__ import annotations

import pytest

from app.services.ai.base import (
    CompatibilityCompareDirection,
    CompatibilityCompareRequest,
    CompatibilityCompareResult,
    ProviderError,
    ProviderErrorKind,
)
from app.services.ai.gateway import AIGateway
from app.services.ai.providers import (
    MockAIProvider,
    normalize_compatibility_compare_payload,
)


def _request() -> CompatibilityCompareRequest:
    return CompatibilityCompareRequest(
        viewer_personal="age=30；city_code=330100；interest_tags=[travel]",
        target_personal="age=31；city_code=330100；interest_tags=[travel, hiking]",
        viewer_ideal="age=[25,40]；relationship_goal=marriage",
        target_ideal="age=[28,35]；relationship_goal=marriage",
        viewer_personal_digest="价值观：欣赏真诚善良的人",
    )


def test_mock_provider_returns_deterministic_compare_fixture() -> None:
    result = __import__("asyncio").run(MockAIProvider().compare_compatibility(_request()))
    assert result.viewer_to_target.score == 72
    assert result.target_to_viewer.score == 68
    assert len(result.viewer_to_target.reasons) == 3
    assert len(result.target_to_viewer.reasons) == 3


@pytest.mark.asyncio
async def test_mock_provider_failure_injection_scoped() -> None:
    provider = MockAIProvider(failures=("compare_compatibility:timeout",))
    with pytest.raises(ProviderError) as exc_info:
        await provider.compare_compatibility(_request())
    assert exc_info.value.kind is ProviderErrorKind.RETRYABLE


@pytest.mark.asyncio
async def test_mock_provider_http_500_injection_scoped() -> None:
    provider = MockAIProvider(failures=("compare_compatibility:http_500",))
    with pytest.raises(ProviderError) as exc_info:
        await provider.compare_compatibility(_request())
    assert exc_info.value.kind is ProviderErrorKind.RETRYABLE


def test_normalize_clamps_score_and_cleans_reasons() -> None:
    normalized = normalize_compatibility_compare_payload(
        {
            "viewer_to_target": {
                "score": "122.6",
                "reasons": [" 理由一 ", "", "理由一", 123, "x" * 80],
            },
            "target_to_viewer": {"score": -5, "reasons": ["a", "b", "c"]},
        }
    )
    assert normalized["viewer_to_target"]["score"] == 100
    reasons = normalized["viewer_to_target"]["reasons"]
    assert reasons == ["理由一", "x" * 50]
    assert normalized["target_to_viewer"]["score"] == 0
    assert normalized["target_to_viewer"]["reasons"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_openai_compat_retryable_on_missing_direction(monkeypatch) -> None:
    """缺一个方向/理由条数不足 → schema 校验失败 → RETRYABLE ProviderError。"""
    from app.services.ai.providers import _OpenAICompatProvider

    provider = _OpenAICompatProvider(
        api_key=None,
        base_url="http://localhost",
        model="test",
        max_tokens=64,
        api_key_env="AI_TEST_KEY",
    )

    async def fake_chat_json(prompt: str):
        return {
            "viewer_to_target": {"score": 72, "reasons": ["只有一条"]}
        }

    monkeypatch.setattr(provider, "_chat_json", fake_chat_json)
    with pytest.raises(ProviderError) as exc_info:
        await provider.compare_compatibility(_request())
    assert exc_info.value.kind is ProviderErrorKind.RETRYABLE


@pytest.mark.asyncio
async def test_openai_compat_success_path(monkeypatch) -> None:
    from app.services.ai.providers import _OpenAICompatProvider

    provider = _OpenAICompatProvider(
        api_key=None,
        base_url="http://localhost",
        model="test",
        max_tokens=64,
        api_key_env="AI_TEST_KEY",
    )

    async def fake_chat_json(prompt: str):
        assert "婚恋匹配分析师" in prompt
        return {
            "viewer_to_target": {"score": 66.4, "reasons": ["a", "b", "c"]},
            "target_to_viewer": {"score": "80", "reasons": ["x", "y", "z"]},
        }

    monkeypatch.setattr(provider, "_chat_json", fake_chat_json)
    result = await provider.compare_compatibility(_request())
    assert result.viewer_to_target.score == 66
    assert result.target_to_viewer.score == 80


@pytest.mark.asyncio
async def test_gateway_compare_schema_violation_is_non_retryable() -> None:
    """provider 返回绕过 pydantic 的非法类型 → Gateway 归类 AI_INPUT_INVALID。"""

    class BrokenProvider:
        async def compare_compatibility(self, request):
            return {"viewer_to_target": "not-a-model"}

    gateway = AIGateway()
    gateway.set_provider(BrokenProvider())  # type: ignore[arg-type]
    outcome = await gateway.compare_compatibility(_stub_context(), _request())
    assert outcome.error_code == "AI_INPUT_INVALID"
    assert outcome.retryable is False


def _stub_context():
    from app.services.ai.base import AITaskContext

    return AITaskContext(
        task_id="it-compare-task",
        request_id="it-compare",
        scene="profile_text_extract",
    )
