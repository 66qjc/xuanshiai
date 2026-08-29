"""阿里云 cosyvoice 流式 TTS WebSocket client 单元测试：mock WebSocket 连接。

覆盖：connect 发送 StartSynthesis、synthesize 发送 RunSynthesis、
audio_chunks 收集二进制音频帧、finish 发送 StopSynthesis 并等待
SynthesisCompleted、异常映射、重复 connect 防御。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.voice.providers import (
    _AliyunAPIError,
    _AliyunAuthError,
    _AliyunConnectionError,
)
from app.services.voice.stream_tts_provider import AliyunStreamTTSClient


def _settings_with_keys(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "ai_aliyun_voice_access_key_id": SecretStr("test-ak-id"),
        "ai_aliyun_voice_access_key_secret": SecretStr("test-ak-secret"),
        "ai_aliyun_voice_region": "cn-shanghai",
    }
    base.update(overrides)
    return Settings(**base)


class MockWSConnection:
    """Mock websockets ClientConnection，记录发送历史并回放服务端响应。"""

    def __init__(self, responses: list[str | bytes] | None = None) -> None:
        self._responses = list(responses or [])
        self._sent: list[str | bytes] = []
        self._closed = False

    async def send(self, data: str | bytes) -> None:
        self._sent.append(data)

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for resp in self._responses:
            yield resp

    async def close(self) -> None:
        self._closed = True

    @property
    def sent_texts(self) -> list[str]:
        return [s for s in self._sent if isinstance(s, str)]

    @property
    def sent_bytes_count(self) -> int:
        return sum(1 for s in self._sent if isinstance(s, bytes))


def _make_started_response() -> str:
    return json.dumps({
        "header": {
            "task_id": "t1",
            "name": "SynthesisStarted",
            "namespace": "FlowingSpeechSynthesizer",
        },
        "payload": {},
    })


def _make_completed_response() -> str:
    return json.dumps({
        "header": {
            "task_id": "t1",
            "name": "SynthesisCompleted",
            "namespace": "FlowingSpeechSynthesizer",
        },
        "payload": {"measureLength": 10},
    })


# ----------------------------------------------------------------------
# connect + synthesize + audio_chunks + finish
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_sends_start_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)
    mock_ws = MockWSConnection([_make_started_response()])
    client = AliyunStreamTTSClient(ws_connect=AsyncMock(return_value=mock_ws))
    await client.connect(app_key="test-app-key", token="pre-generated-token")
    assert client._connected is True
    start_frame = json.loads(mock_ws.sent_texts[0])
    assert start_frame["header"]["name"] == "StartSynthesis"
    assert start_frame["header"]["namespace"] == "FlowingSpeechSynthesizer"
    assert start_frame["header"]["appkey"] == "test-app-key"
    assert start_frame["payload"]["format"] == "mp3"
    assert start_frame["payload"]["sample_rate"] == 16000
    await client._close()


@pytest.mark.asyncio
async def test_connect_raises_without_access_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)
    client = AliyunStreamTTSClient()
    with pytest.raises(_AliyunAuthError):
        await client.get_token()


@pytest.mark.asyncio
async def test_synthesize_sends_run_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)
    mock_ws = MockWSConnection([_make_started_response()])
    client = AliyunStreamTTSClient(ws_connect=AsyncMock(return_value=mock_ws))
    await client.connect(app_key="app", token="tok")
    await client.synthesize("你好，我今年28岁。")
    run_frame = json.loads(mock_ws.sent_texts[-1])
    assert run_frame["header"]["name"] == "RunSynthesis"
    assert run_frame["header"]["namespace"] == "FlowingSpeechSynthesizer"
    assert run_frame["payload"]["text"] == "你好，我今年28岁。"
    await client._close()


@pytest.mark.asyncio
async def test_synthesize_before_connect_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)
    client = AliyunStreamTTSClient(ws_connect=AsyncMock())
    with pytest.raises(_AliyunAPIError):
        await client.synthesize("hello")


@pytest.mark.asyncio
async def test_audio_chunks_collects_binary_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audio_chunks yield 服务端二进制音频帧，drain 结束后自然终止。"""
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)
    audio_data_1 = b"\x00\x01" * 100
    audio_data_2 = b"\x02\x03" * 50
    responses = [
        _make_started_response(),
        audio_data_1,
        audio_data_2,
    ]
    mock_ws = MockWSConnection(responses)
    client = AliyunStreamTTSClient(ws_connect=AsyncMock(return_value=mock_ws))
    await client.connect(app_key="app", token="tok")
    await client.synthesize("测试文本。")
    # 先消费音频帧（drain 迭代器耗尽后入队 sentinel，audio_chunks 返回）。
    chunks: list[bytes] = []
    async for chunk in client.audio_chunks():
        chunks.append(chunk)
    assert len(chunks) == 2
    assert chunks[0] == audio_data_1
    assert chunks[1] == audio_data_2
    # finish 发送 StopSynthesis（drain 已结束，立即返回）。
    await client.finish()
    stop_frame = json.loads(mock_ws.sent_texts[-1])
    assert stop_frame["header"]["name"] == "StopSynthesis"


@pytest.mark.asyncio
async def test_finish_sends_stop_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finish 发送 StopSynthesis 并等待 drain 结束（无 Completed，迭代器耗尽）。"""
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)
    audio_data = b"\x00\x01" * 100
    responses = [
        _make_started_response(),
        audio_data,
    ]
    mock_ws = MockWSConnection(responses)
    client = AliyunStreamTTSClient(ws_connect=AsyncMock(return_value=mock_ws))
    await client.connect(app_key="app", token="tok")
    await client.synthesize("测试。")
    # 先消费音频帧。
    async for _chunk in client.audio_chunks():
        pass
    # finish 发送 StopSynthesis（drain 已结束，立即返回）。
    await client.finish()
    stop_frame = json.loads(mock_ws.sent_texts[-1])
    assert stop_frame["header"]["name"] == "StopSynthesis"
    assert stop_frame["header"]["namespace"] == "FlowingSpeechSynthesizer"
    assert stop_frame["header"]["appkey"] == "app"
    assert client._completed is True


@pytest.mark.asyncio
async def test_connect_failure_maps_to_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)

    async def _failing_connect(*args: Any, **kwargs: Any):
        raise OSError("connection refused")

    client = AliyunStreamTTSClient(ws_connect=_failing_connect)
    with pytest.raises((_AliyunConnectionError, _AliyunAPIError)):
        await client.connect(app_key="app", token="tok")


@pytest.mark.asyncio
async def test_duplicate_connect_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_tts_provider.settings", settings)
    mock_ws = MockWSConnection([_make_started_response()])
    client = AliyunStreamTTSClient(ws_connect=AsyncMock(return_value=mock_ws))
    await client.connect(app_key="app", token="tok")
    with pytest.raises(_AliyunAPIError):
        await client.connect(app_key="app", token="tok")
    await client._close()
