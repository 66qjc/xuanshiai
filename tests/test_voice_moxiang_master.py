"""墨相师编排器单测：mock AIGateway + VoiceGateway + provider，
验证多轮历史、流式回复、TTS 合成与状态流转。

覆盖：
- stream_reply 正常路径（流式 yield content → 历史累积）
- 多轮历史累积与截断（超过上限时只保留最近轮次）
- narrative_context 注入
- synthesize_current TTS 正常/失败
- bump_generation 作废正在进行的回复
"""

from __future__ import annotations

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.voice.base import SynthesizeResult
from app.services.voice.gateway import VoiceInvokeOutcome
from app.services.voice.master_orchestrator import (
    MasterState,
    MoxiangMasterOrchestrator,
)


def _make_mock_ai_gateway() -> MagicMock:
    return MagicMock()


def _make_mock_voice_gateway(
    synth_result: SynthesizeResult | None = None,
) -> MagicMock:
    gateway = MagicMock()
    if synth_result is not None:
        outcome = VoiceInvokeOutcome(result=synth_result)
    else:
        outcome = VoiceInvokeOutcome(result=SynthesizeResult(
            audio_url="/storage/tts/test.wav",
            audio_format="wav",
            duration_ms=2000,
            expires_at=None,
        ))
    gateway.synthesize = AsyncMock(return_value=outcome)
    return gateway


class _FakeProvider:
    """假 AI provider，stream_chat 产出可控的分段内容。"""

    def __init__(self, chunks: list[tuple[str, str]]):
        self._chunks = chunks

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
    ) -> AsyncIterator[tuple[str, str]]:
        for kind, text in self._chunks:
            yield (kind, text)


@pytest.mark.asyncio
async def test_stream_reply_normal_path():
    """流式回复正常路径：yield content 段，回复完成后历史累积。"""
    provider = _FakeProvider([
        ("content", "你好"),
        ("content", "呀"),
        ("finish", "stop"),
    ])
    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    with patch(
        "app.services.voice.master_orchestrator.get_provider",
        return_value=provider,
    ), patch(
        "app.services.voice.master_orchestrator.settings"
    ) as mock_settings:
        mock_settings.ai_provider = "mock"
        mock_settings.ai_voice_provider = "aliyun"
        mock_settings.ai_voice_model_name = "test"
        results = []
        async for kind, text in orchestrator.stream_reply(
            "我今年28", request_id="req-1"
        ):
            results.append((kind, text))

    content_parts = [t for k, t in results if k == "content"]
    assert "".join(content_parts) == "你好呀"
    assert orchestrator._last_reply_text == "你好呀"
    assert orchestrator.state == MasterState.IDLE
    # 历史应累积 1 轮（user + assistant）
    assert len(orchestrator._history) == 2
    assert orchestrator._history[0] == {"role": "user", "content": "我今年28"}
    assert orchestrator._history[1] == {"role": "assistant", "content": "你好呀"}


@pytest.mark.asyncio
async def test_multi_turn_history_accumulation():
    """多轮对话历史累积，超过上限时截断到最近轮次。"""
    provider = _FakeProvider([("content", "嗯"), ("finish", "stop")])
    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    with patch(
        "app.services.voice.master_orchestrator.get_provider",
        return_value=provider,
    ), patch(
        "app.services.voice.master_orchestrator.settings"
    ) as mock_settings:
        mock_settings.ai_provider = "mock"
        mock_settings.ai_voice_provider = "aliyun"
        mock_settings.ai_voice_model_name = "test"
        # 模拟 15 轮对话
        for i in range(15):
            async for _ in orchestrator.stream_reply(f"msg-{i}"):
                pass

    # 上限 12 轮 = 24 条消息
    assert len(orchestrator._history) == 24
    # 保留最近 12 轮
    assert orchestrator._history[0] == {"role": "user", "content": "msg-3"}
    assert orchestrator._history[-1] == {"role": "assistant", "content": "嗯"}


@pytest.mark.asyncio
async def test_narrative_context_injected():
    """narrative_context 设置后应出现在组装的 messages 中。"""
    provider = _FakeProvider([("content", "好的"), ("finish", "stop")])
    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    orchestrator.set_narrative_context("用户画像标题：慢热的人")
    captured_messages: list[dict[str, str]] = []

    original_stream_chat = provider.stream_chat

    async def capture_stream_chat(messages, *, json_mode=False):
        captured_messages.extend(messages)
        async for item in original_stream_chat(messages, json_mode=json_mode):
            yield item

    provider.stream_chat = capture_stream_chat  # type: ignore

    with patch(
        "app.services.voice.master_orchestrator.get_provider",
        return_value=provider,
    ), patch(
        "app.services.voice.master_orchestrator.settings"
    ) as mock_settings:
        mock_settings.ai_provider = "mock"
        mock_settings.ai_voice_provider = "aliyun"
        mock_settings.ai_voice_model_name = "test"
        async for _ in orchestrator.stream_reply("说说你自己"):
            pass

    # messages 应包含 system + narrative system + user
    assert len(captured_messages) >= 3
    assert captured_messages[0]["role"] == "system"
    assert "墨相师" in captured_messages[0]["content"]
    assert captured_messages[1]["role"] == "system"
    assert "慢热的人" in captured_messages[1]["content"]
    assert captured_messages[-1] == {"role": "user", "content": "说说你自己"}


@pytest.mark.asyncio
async def test_synthesize_current_success():
    """synthesize_current 调用 VoiceGateway 并返回 TTS URL。"""
    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    orchestrator._last_reply_text = "这是回复"
    orchestrator._last_request_id = "req-1"

    with patch(
        "app.services.voice.master_orchestrator.settings"
    ) as mock_settings:
        mock_settings.ai_voice_provider = "aliyun"
        mock_settings.ai_voice_model_name = "test"
        result = await orchestrator.synthesize_current()

    assert result.tts_audio_url == "/storage/tts/test.wav"
    assert result.tts_duration_ms == 2000
    assert orchestrator.state == MasterState.IDLE


@pytest.mark.asyncio
async def test_synthesize_current_empty_reply():
    """无回复文本时 synthesize_current 返回空结果。"""
    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    orchestrator._last_reply_text = ""

    result = await orchestrator.synthesize_current()

    assert result.tts_audio_url is None
    assert result.ai_reply == ""


@pytest.mark.asyncio
async def test_bump_generation_discards_stale_reply():
    """bump_generation 后，正在进行的流式回复应被作废。

    模拟：stream_reply 进行中 bump（用户重新说），
    后续 content 段因 generation 不匹配被丢弃。
    """

    class _BumpMidStreamProvider:
        def __init__(self, chunks, orchestrator):
            self._chunks = chunks
            self._orch = orchestrator

        async def stream_chat(self, messages, *, json_mode=False):
            for kind, text in self._chunks:
                if kind == "content" and text == "第二段":
                    self._orch.bump_generation()
                yield (kind, text)

    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    provider = _BumpMidStreamProvider(
        [("content", "第一段"), ("content", "第二段"), ("finish", "stop")],
        orchestrator,
    )

    with patch(
        "app.services.voice.master_orchestrator.get_provider",
        return_value=provider,
    ), patch(
        "app.services.voice.master_orchestrator.settings"
    ) as mock_settings:
        mock_settings.ai_provider = "mock"
        mock_settings.ai_voice_provider = "aliyun"
        mock_settings.ai_voice_model_name = "test"
        results = []
        async for kind, text in orchestrator.stream_reply("test"):
            results.append((kind, text))

    # bump 在第二段前发生，第一段应产出，第二段及之后应被丢弃
    content_parts = [t for k, t in results if k == "content"]
    assert "第一段" in content_parts
    assert "第二段" not in content_parts


def test_reset_clears_last_reply():
    """reset 清除最近回复文本。"""
    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    orchestrator._last_reply_text = "旧回复"
    orchestrator.reset()
    assert orchestrator._last_reply_text == ""
