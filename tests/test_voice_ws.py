"""WebSocket 实时语音对话路由单测：fake AliyunStreamASRClient，验证消息协议、鉴权、状态流转。

覆盖：
- 门禁关闭时拒绝连接（close 1008）
- 缺 token / 无效 token 拒绝连接
- 正常对话流程：session_start → audio_start → audio_end → 消息序列
- cancel 消息清理
- 生产环境 fail closed
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import create_token
from app.main import app


def _settings_dev(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "environment": "development",
        "ai_voice_enabled": True,
        "ai_voice_conversation_enabled": True,
        "ai_voice_provider": "aliyun",
        "ai_master_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


def _make_access_token(user_id: int = 1, session_id: int = 1) -> str:
    """生成一个有效的 JWT access token（供 WS 鉴权）。"""
    return create_token(
        user_id=user_id,
        session_id=session_id,
        token_type="access",
        expires_delta=timedelta(hours=1),
    )


@pytest.fixture()
def dev_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """注入开发环境 settings，启用语音对话功能。"""
    settings = _settings_dev()
    monkeypatch.setattr("app.api.routes.voice_ws.settings", settings)
    monkeypatch.setattr("app.services.voice.gateway.settings", settings)
    monkeypatch.setattr("app.services.voice.providers.settings", settings)
    monkeypatch.setattr("app.services.ai.gateway.settings", settings)
    monkeypatch.setattr("app.services.ai.flags.settings_ref", settings)
    import app.services.ai.flags as flags_mod

    monkeypatch.setattr(flags_mod, "settings", settings, raising=False)
    return settings


# ----------------------------------------------------------------------
# 门禁与鉴权
# ----------------------------------------------------------------------


def test_gate_disabled_rejects_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ai_voice_conversation_enabled=false 时拒绝连接（close 1008）。"""
    settings = Settings(
        _env_file=None, environment="development",
        ai_voice_enabled=True, ai_voice_conversation_enabled=False,
    )
    monkeypatch.setattr("app.api.routes.voice_ws.settings", settings)
    token = _make_access_token()
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/api/v1/voice/conversation?token={token}"
        ) as ws:
            ws.receive_text()


def test_missing_token_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.api.routes.voice_ws.settings", settings)
    with pytest.raises(Exception):
        with client.websocket_connect("/api/v1/voice/conversation"):
            pass


def test_invalid_token_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_dev()
    monkeypatch.setattr("app.api.routes.voice_ws.settings", settings)
    with pytest.raises(Exception):
        with client.websocket_connect(
            "/api/v1/voice/conversation?token=invalid-token"
        ):
            pass


# ----------------------------------------------------------------------
# Fake AliyunStreamASRClient（注入 stream_asr_factory）
# ----------------------------------------------------------------------


class FakeASRClient:
    """Fake AliyunStreamASRClient，模拟 connect/send_chunk/finish/partial_results。

    通过 stream_asr_factory kwarg 注入到 AliyunVoiceProvider，绕过真实阿里云连接。
    """

    def __init__(self, partials: list[str] | None = None, final: str = "") -> None:
        self._partials = list(partials or [])
        self._final = final
        self._connected = False
        self._sent_chunks = 0

    async def connect(self, app_key: str, model: str = "", token: str | None = None) -> None:
        self._connected = True

    async def send_chunk(self, pcm_bytes: bytes) -> None:
        self._sent_chunks += 1

    async def finish(self) -> str:
        return self._final

    async def partial_results(self) -> Any:
        for text in self._partials:
            yield text

    async def _close(self) -> None:
        self._connected = False


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch,
    fake_client: FakeASRClient,
) -> None:
    """Patch get_voice_provider 返回 fake provider，stream_transcribe 返回 fake client。"""
    fake_provider = MagicMock()
    fake_provider.stream_transcribe = AsyncMock(return_value=fake_client)
    monkeypatch.setattr(
        "app.api.routes.voice_ws.get_voice_provider",
        lambda name, **kw: fake_provider,
    )


# ----------------------------------------------------------------------
# 正常对话流程
# ----------------------------------------------------------------------


def test_conversation_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整对话：session_start → audio_start → audio_end → 消息序列。"""
    settings = _settings_dev()
    monkeypatch.setattr("app.api.routes.voice_ws.settings", settings)
    monkeypatch.setattr("app.services.voice.gateway.settings", settings)
    monkeypatch.setattr("app.services.ai.gateway.settings", settings)

    fake_asr = FakeASRClient(
        partials=["我今年", "我今年28岁"], final="我今年28岁，在北京工作"
    )
    _patch_provider(monkeypatch, fake_asr)

    # mock AIGateway.structured_extract 返回抽取结果。
    from app.services.ai.base import (
        ExtractedField,
        StructuredExtractResult,
    )
    from app.services.ai.gateway import InvokeOutcome
    from app.services.voice.gateway import VoiceInvokeOutcome
    from app.services.voice.base import SynthesizeResult

    field = ExtractedField.model_construct(
        field_key="age", subject="personal", value=28,
        source_quote="28岁", confidence=0.95,
        needs_confirmation=True, confirmation_status="suggested",
    )
    extract_result = StructuredExtractResult(fields=(field,))

    mock_ai_gateway = MagicMock()
    mock_ai_gateway.structured_extract = AsyncMock(
        return_value=InvokeOutcome(result=extract_result)
    )
    mock_voice_gateway = MagicMock()
    mock_voice_gateway.synthesize = AsyncMock(
        return_value=VoiceInvokeOutcome(
            result=SynthesizeResult(
                audio_url="/tts/test.mp3", audio_format="mp3",
                duration_ms=2000,
            )
        )
    )
    monkeypatch.setattr(
        "app.api.routes.voice_ws.AIGateway", lambda: mock_ai_gateway
    )
    monkeypatch.setattr(
        "app.api.routes.voice_ws.VoiceGateway",
        lambda **kw: mock_voice_gateway,
    )

    token = _make_access_token()
    with client.websocket_connect(
        f"/api/v1/voice/conversation?token={token}"
    ) as ws:
        ws.send_text(json.dumps({
            "type": "session_start",
            "session_id": "sess1",
            "field_key": "age",
        }))
        ws.send_text(json.dumps({"type": "audio_start"}))
        # 接收 partial_transcript 消息（2条）。
        msgs = []
        for _ in range(2):
            raw = ws.receive_text()
            msgs.append(json.loads(raw))
        ws.send_text(json.dumps({"type": "audio_end"}))
        # 接收 final_transcript, ai_thinking, ai_reply, tts_audio。
        for _ in range(4):
            raw = ws.receive_text()
            msgs.append(json.loads(raw))

    types = [m["type"] for m in msgs]
    assert "partial_transcript" in types
    assert "final_transcript" in types
    assert "ai_thinking" in types
    assert "ai_reply" in types
    assert "tts_audio" in types
    reply_msg = next(m for m in msgs if m["type"] == "ai_reply")
    assert reply_msg["field_key"] == "age"
    tts_msg = next(m for m in msgs if m["type"] == "tts_audio")
    assert tts_msg["audio_url"] == "/tts/test.mp3"


def test_cancel_clears_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """cancel 消息应清理当前轮次，不报错。"""
    settings = _settings_dev()
    monkeypatch.setattr("app.api.routes.voice_ws.settings", settings)

    fake_asr = FakeASRClient(partials=["partial"], final="partial")
    _patch_provider(monkeypatch, fake_asr)

    mock_ai_gateway = MagicMock()
    monkeypatch.setattr(
        "app.api.routes.voice_ws.AIGateway", lambda: mock_ai_gateway
    )
    mock_voice_gateway = MagicMock()
    monkeypatch.setattr(
        "app.api.routes.voice_ws.VoiceGateway",
        lambda **kw: mock_voice_gateway,
    )

    token = _make_access_token()
    with client.websocket_connect(
        f"/api/v1/voice/conversation?token={token}"
    ) as ws:
        ws.send_text(json.dumps({
            "type": "session_start", "session_id": "s1", "field_key": "age",
        }))
        ws.send_text(json.dumps({"type": "audio_start"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "cancel"}))
        # cancel 后再 audio_start 应可重新开始（不报错即通过）。
        ws.send_text(json.dumps({
            "type": "session_start", "session_id": "s2", "field_key": "city",
        }))
        ws.send_text(json.dumps({"type": "audio_start"}))
        ws.receive_text()


# ----------------------------------------------------------------------
# 生产门禁 fail closed
# ----------------------------------------------------------------------


def test_production_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产环境 ai_voice_conversation_enabled=false 时拒绝连接。"""
    settings = Settings(
        _env_file=None, environment="production", auto_init_db=False,
        sms_provider="disabled", wechat_provider="wechat",
        wechat_payment_mode="real",
        ai_voice_enabled=False, ai_voice_conversation_enabled=False,
    )
    monkeypatch.setattr("app.api.routes.voice_ws.settings", settings)
    token = _make_access_token()
    with pytest.raises(Exception):
        with client.websocket_connect(
            f"/api/v1/voice/conversation?token={token}"
        ):
            pass


# TestClient 需要在模块级可用。
client = TestClient(app)
