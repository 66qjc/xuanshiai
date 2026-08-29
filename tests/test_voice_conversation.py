"""对话编排器单测：mock AIGateway + VoiceGateway，验证状态流转与端到端编排。

覆盖：start_listening 状态机、process_transcript 正常路径（画像抽取 → 回复 →
TTS）、画像抽取失败时填充 error_code、TTS 失败时填充 error_code、状态非
listening 时调用抛错、reset 恢复 IDLE。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import Settings
from app.services.ai.base import (
    ExtractedField,
    ProviderError,
    ProviderErrorKind,
    StructuredExtractResult,
)
from app.services.ai.gateway import InvokeOutcome
from app.services.voice.base import SynthesizeResult
from app.services.voice.conversation import (
    ConversationState,
    VoiceConversationOrchestrator,
)
from app.services.voice.gateway import VoiceInvokeOutcome


def _settings_dev(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "environment": "development",
        "ai_provider": "mock",
        "ai_voice_provider": "aliyun",
    }
    base.update(overrides)
    return Settings(**base)


def _make_mock_ai_gateway(
    extract_result: StructuredExtractResult | None = None,
    extract_error: ProviderError | None = None,
    reply_text: str | None = None,
    reply_error: Exception | None = None,
) -> MagicMock:
    """构造 mock AIGateway，structured_extract / generate_reply 返回可控结果。

    ``reply_text`` 为 None 且无 ``reply_error`` 时，generate_reply 返回空结果
    （模拟 LLM 未配置或返回空），编排器降级到模板回复。
    """
    from app.services.ai.base import ReplyResult

    gateway = MagicMock()
    if extract_error is not None:
        outcome = InvokeOutcome(
            error_code=extract_error.code,
            error_message="mock extract error",
            retryable=extract_error.retryable,
        )
    else:
        outcome = InvokeOutcome(result=extract_result)
    gateway.structured_extract = AsyncMock(return_value=outcome)

    if reply_error is not None:
        gateway.generate_reply = AsyncMock(side_effect=reply_error)
    elif reply_text is not None:
        reply_outcome = InvokeOutcome(result=ReplyResult(reply_text=reply_text))
        gateway.generate_reply = AsyncMock(return_value=reply_outcome)
    else:
        # 未配置 reply：返回空 outcome，降级模板。
        gateway.generate_reply = AsyncMock(return_value=InvokeOutcome())
    return gateway


def _make_mock_voice_gateway(
    synth_result: SynthesizeResult | None = None,
    synth_error: ProviderError | None = None,
) -> MagicMock:
    """构造 mock VoiceGateway，synthesize 返回可控结果。"""
    gateway = MagicMock()
    if synth_error is not None:
        outcome = VoiceInvokeOutcome(
            error_code=synth_error.code,
            error_message="mock synth error",
            retryable=synth_error.retryable,
        )
    else:
        outcome = VoiceInvokeOutcome(result=synth_result)
    gateway.synthesize = AsyncMock(return_value=outcome)
    return gateway


def _make_extract_result(field_key: str = "age") -> StructuredExtractResult:
    field = ExtractedField.model_construct(
        field_key=field_key,
        subject="personal",
        value=28,
        source_quote="28岁",
        confidence=0.95,
        needs_confirmation=True,
        confirmation_status="suggested",
    )
    return StructuredExtractResult(fields=(field,))


# ----------------------------------------------------------------------
# start_listening 状态机
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_listening_sets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    assert orch.state == ConversationState.IDLE
    orch.start_listening(session_id="sess1", field_key="age")
    assert orch.state == ConversationState.LISTENING


@pytest.mark.asyncio
async def test_start_listening_rejects_when_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    orch.start_listening(session_id="s1")
    orch.state = ConversationState.PROCESSING
    with pytest.raises(RuntimeError):
        orch.start_listening(session_id="s2")


# ----------------------------------------------------------------------
# process_transcript 正常路径
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_transcript_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract = _make_extract_result(field_key="age")
    synth = SynthesizeResult(
        audio_url="/storage/voice/tts/test.mp3",
        audio_format="mp3",
        duration_ms=3000,
    )
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(extract_result=extract),
        voice_gateway=_make_mock_voice_gateway(synth_result=synth),
    )
    orch.start_listening(session_id="s1", field_key="age")
    result = await orch.process_transcript(
        "我今年28岁", user_id=1, request_id="req1", synthesize=True
    )
    assert result.final_transcript == "我今年28岁"
    assert result.extracted_field_key == "age"
    assert result.field_key == "age"
    assert result.extracted_value == 28
    assert result.ai_reply  # 非空
    assert result.tts_audio_url == "/storage/voice/tts/test.mp3"
    assert result.tts_duration_ms == 3000
    assert result.error_code is None
    assert orch.state == ConversationState.IDLE


@pytest.mark.asyncio
async def test_process_transcript_does_not_synthesize_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract = _make_extract_result(field_key="age")
    voice = _make_mock_voice_gateway(
        synth_result=SynthesizeResult(
            audio_url="/storage/voice/tts/test.mp3",
            audio_format="mp3",
            duration_ms=3000,
        )
    )
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(extract_result=extract, reply_text="记下了，你在哪座城市？"),
        voice_gateway=voice,
    )
    orch.start_listening(session_id="s1", field_key="age")
    result = await orch.process_transcript("我今年28岁", user_id=1, request_id="req1")
    assert result.ai_reply
    assert result.tts_audio_url is None
    voice.synthesize.assert_not_called()
    spoken = await orch.synthesize_current()
    assert spoken.tts_audio_url == "/storage/voice/tts/test.mp3"
    voice.synthesize.assert_called_once()


@pytest.mark.asyncio
async def test_process_transcript_extract_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抽取失败不打断口语回复。"""
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract_error = ProviderError(
        code="AI_TEMPORARILY_UNAVAILABLE",
        message="extract failed",
        kind=ProviderErrorKind.RETRYABLE,
    )
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(extract_error=extract_error),
        voice_gateway=_make_mock_voice_gateway(),
    )
    orch.start_listening(session_id="s1")
    result = await orch.process_transcript("text", user_id=1)
    assert result.error_code is None
    assert result.ai_reply  # 非空：抽取失败仍继续口语回复
    assert result.tts_audio_url is None
    assert orch.state == ConversationState.IDLE


@pytest.mark.asyncio
async def test_process_transcript_tts_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract = _make_extract_result()
    synth_error = ProviderError(
        code="AI_QUOTA_EXCEEDED",
        message="tts quota",
        kind=ProviderErrorKind.RETRYABLE,
    )
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(extract_result=extract),
        voice_gateway=_make_mock_voice_gateway(synth_error=synth_error),
    )
    orch.start_listening(session_id="s1")
    result = await orch.process_transcript("text", user_id=1)
    # TTS 失败：先拼回复，再单独合成。
    assert result.ai_reply  # 回复已生成
    assert result.tts_audio_url is None
    spoken = await orch.synthesize_current()
    assert spoken.error_code == "AI_QUOTA_EXCEEDED"
    assert spoken.tts_audio_url is None
    assert orch.state == ConversationState.IDLE


@pytest.mark.asyncio
async def test_process_transcript_wrong_state_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    # 未 start_listening，状态为 IDLE。
    with pytest.raises(RuntimeError):
        await orch.process_transcript("text", user_id=1)


@pytest.mark.asyncio
async def test_reset_restores_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(),
        voice_gateway=_make_mock_voice_gateway(),
    )
    orch.start_listening(session_id="s1")
    orch.state = ConversationState.PROCESSING
    orch._last_reply_text = "上一轮回复"
    orch.reset()
    assert orch.state == ConversationState.IDLE
    assert orch._last_reply_text == ""


@pytest.mark.asyncio
async def test_custom_reply_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract = _make_extract_result()
    synth = SynthesizeResult(
        audio_url="/tts.mp3", audio_format="mp3", duration_ms=1000
    )
    custom_reply = "你说了28岁，接下来告诉我身高吧"

    def builder(transcript: str, ext: Any, req_id: str) -> str:
        return custom_reply

    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(extract_result=extract),
        voice_gateway=_make_mock_voice_gateway(synth_result=synth),
        reply_builder=builder,
    )
    orch.start_listening(session_id="s1")
    result = await orch.process_transcript("28岁", user_id=1)
    assert result.ai_reply == custom_reply


# ----------------------------------------------------------------------
# LLM 回复生成（generate_reply 真接）
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_reply_used_when_gateway_returns_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AIGateway.generate_reply 返回非空文本时，编排器使用 LLM 回复。"""
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract = _make_extract_result(field_key="age")
    synth = SynthesizeResult(
        audio_url="/tts.mp3", audio_format="mp3", duration_ms=1000
    )
    llm_reply = "收到，28岁啦。你平时喜欢做什么呀？"
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(
            extract_result=extract, reply_text=llm_reply
        ),
        voice_gateway=_make_mock_voice_gateway(synth_result=synth),
    )
    orch.start_listening(session_id="s1", field_key="age")
    result = await orch.process_transcript("我今年28岁", user_id=1)
    assert result.ai_reply == llm_reply
    # generate_reply 被调用，参数含本轮转写文本。
    call_kwargs = orch.ai_gateway.generate_reply.await_args.args[1]
    assert call_kwargs.transcript == "我今年28岁"
    assert call_kwargs.field_key == "age"


@pytest.mark.asyncio
async def test_llm_reply_empty_falls_back_to_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_reply 返回空文本时，降级到模板回复。"""
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract = _make_extract_result(field_key="age")
    synth = SynthesizeResult(
        audio_url="/tts.mp3", audio_format="mp3", duration_ms=1000
    )
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(
            extract_result=extract, reply_text=None
        ),
        voice_gateway=_make_mock_voice_gateway(synth_result=synth),
    )
    orch.start_listening(session_id="s1", field_key="age")
    result = await orch.process_transcript("28岁", user_id=1)
    # 降级模板包含 field_key。
    assert result.ai_reply == "好的，已记录你的信息。请继续告诉我你的age。"


@pytest.mark.asyncio
async def test_llm_reply_exception_falls_back_to_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_reply 抛异常时，降级到模板回复，对话不中断。"""
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)
    extract = _make_extract_result(field_key="age")
    synth = SynthesizeResult(
        audio_url="/tts.mp3", audio_format="mp3", duration_ms=1000
    )
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(
            extract_result=extract,
            reply_error=RuntimeError("LLM down"),
        ),
        voice_gateway=_make_mock_voice_gateway(synth_result=synth),
    )
    orch.start_listening(session_id="s1", field_key="age")
    result = await orch.process_transcript("28岁", user_id=1, synthesize=True)
    assert result.ai_reply  # 非空：降级模板
    assert result.error_code is None  # 回复失败被吞，不阻断流程
    assert result.tts_audio_url == "/tts.mp3"


@pytest.mark.asyncio
async def test_stream_reply_events_emits_reasoning_then_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.services.voice.conversation.settings", settings)

    async def fake_stream(messages, json_mode=False):
        yield ("reasoning", "先接住年龄")
        yield ("content", "记下了，你在哪座城市？")
        yield ("finish", "stop")

    provider = MagicMock()
    provider.stream_chat = fake_stream
    extract = _make_extract_result(field_key="age")
    orch = VoiceConversationOrchestrator(
        ai_gateway=_make_mock_ai_gateway(extract_result=extract),
        voice_gateway=_make_mock_voice_gateway(),
        reply_streamer=provider.stream_chat,
    )
    orch.start_listening(session_id="s1", field_key="age")
    events = [item async for item in orch.stream_reply_events("我今年28岁", user_id=1)]
    kinds = [k for k, _ in events]
    assert "reasoning" in kinds
    assert "content" in kinds
    assert events[-1][0] == "finish"
    assert orch._last_reply_text == "记下了，你在哪座城市？"
    assert orch.state == ConversationState.IDLE
