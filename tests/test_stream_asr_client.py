"""阿里云实时 ASR WebSocket client 单元测试：mock WebSocket 连接，不依赖真实 key。

覆盖：Token 缓存与刷新、connect 发送 StartTranscription、send_chunk 发送二进制
帧、finish 发送 StopTranscription 并返回最终文本、partial_results async
generator、异常映射（连接失败/超时）、重复 connect/finish 防御。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.voice.providers import (
    _AliyunAuthError,
    _AliyunAPIError,
    _AliyunConnectionError,
)
from app.services.voice.stream_provider import AliyunStreamASRClient


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
        "header": {"task_id": "t1", "name": "TranscriptionStarted"},
        "payload": {},
    })


def _make_sentence_result(text: str, is_end: bool = False) -> str:
    return json.dumps({
        "header": {"task_id": "t1", "name": "SentenceResult"},
        "payload": {"result": text, "is_sentence_end": is_end},
    })


def _make_final_result(text: str) -> str:
    return json.dumps({
        "header": {"task_id": "t1", "name": "TranscriptionResult"},
        "payload": {"result": text, "is_sentence_end": True},
    })


def _make_completed_response() -> str:
    return json.dumps({
        "header": {"task_id": "t1", "name": "TranscriptionCompleted"},
        "payload": {},
    })


# ----------------------------------------------------------------------
# get_token
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_token_caches_until_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    client = AliyunStreamASRClient()

    mock_http = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "Token": {"Id": "token-abc", "ExpireTime": 86400}
    }
    mock_http.get = AsyncMock(return_value=mock_response)
    mock_http.aclose = AsyncMock()
    client._http_client = mock_http

    token1 = await client.get_token()
    token2 = await client.get_token()
    assert token1 == "token-abc"
    assert token2 == "token-abc"
    # 第二次应命中缓存，不重复调用 API。
    assert mock_http.get.call_count == 1


@pytest.mark.asyncio
async def test_get_token_raises_without_access_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    client = AliyunStreamASRClient()
    with pytest.raises(_AliyunAuthError):
        await client.get_token()


# ----------------------------------------------------------------------
# connect + send_chunk + finish
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_sends_start_transcription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    mock_ws = MockWSConnection([_make_started_response()])
    client = AliyunStreamASRClient(
        ws_connect=AsyncMock(return_value=mock_ws),
    )
    await client.connect(app_key="test-app-key", token="pre-generated-token")
    assert client._connected is True
    # 第一条发送的消息应为 StartTranscription 控制帧。
    start_frame = json.loads(mock_ws.sent_texts[0])
    assert start_frame["header"]["name"] == "StartTranscription"
    assert start_frame["header"]["appkey"] == "test-app-key"
    assert start_frame["payload"]["format"] == "pcm"
    assert start_frame["payload"]["sample_rate"] == 16000
    await client._close()


@pytest.mark.asyncio
async def test_send_chunk_sends_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    mock_ws = MockWSConnection([_make_started_response()])
    client = AliyunStreamASRClient(
        ws_connect=AsyncMock(return_value=mock_ws),
    )
    await client.connect(app_key="app", token="tok")
    await client.send_chunk(b"\x00\x01" * 160)
    assert mock_ws.sent_bytes_count == 1
    await client._close()


@pytest.mark.asyncio
async def test_send_chunk_before_connect_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    client = AliyunStreamASRClient(ws_connect=AsyncMock())
    with pytest.raises(_AliyunAPIError):
        await client.send_chunk(b"\x00\x01")


@pytest.mark.asyncio
async def test_finish_returns_final_text(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    responses = [
        _make_started_response(),
        _make_sentence_result("我今年28岁", is_end=False),
        _make_final_result("我今年28岁，在北京工作"),
        _make_completed_response(),
    ]
    mock_ws = MockWSConnection(responses)
    client = AliyunStreamASRClient(
        ws_connect=AsyncMock(return_value=mock_ws),
    )
    await client.connect(app_key="app", token="tok")
    final = await client.finish()
    assert final == "我今年28岁，在北京工作"
    # 最后发送的应为 StopTranscription。
    stop_frame = json.loads(mock_ws.sent_texts[-1])
    assert stop_frame["header"]["name"] == "StopTranscription"


@pytest.mark.asyncio
async def test_partial_results_yields_intermediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    responses = [
        _make_started_response(),
        _make_sentence_result("我今年", is_end=False),
        _make_sentence_result("我今年28岁", is_end=False),
        _make_final_result("我今年28岁，在北京工作"),
        _make_completed_response(),
    ]
    mock_ws = MockWSConnection(responses)
    client = AliyunStreamASRClient(
        ws_connect=AsyncMock(return_value=mock_ws),
    )
    await client.connect(app_key="app", token="tok")

    partials: list[str] = []
    async for text in client.partial_results():
        partials.append(text)
    # 最终结果不通过 partial_results yield，只保留中间结果。
    assert partials == ["我今年", "我今年28岁"]


@pytest.mark.asyncio
async def test_connect_failure_maps_to_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)

    async def _failing_connect(*args: Any, **kwargs: Any):
        raise OSError("connection refused")

    client = AliyunStreamASRClient(ws_connect=_failing_connect)
    with pytest.raises((_AliyunConnectionError, _AliyunAPIError)):
        await client.connect(app_key="app", token="tok")


@pytest.mark.asyncio
async def test_duplicate_connect_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings_with_keys()
    monkeypatch.setattr("app.services.voice.stream_provider.settings", settings)
    mock_ws = MockWSConnection([_make_started_response()])
    client = AliyunStreamASRClient(
        ws_connect=AsyncMock(return_value=mock_ws),
    )
    await client.connect(app_key="app", token="tok")
    with pytest.raises(_AliyunAPIError):
        await client.connect(app_key="app", token="tok")
    await client._close()
