"""阿里云语音 provider 单元测试：注入 fake http client，不依赖真实 key。

覆盖：
- recognize_audio（一句话识别）：成功路径、空文本、鉴权失败、限流、5xx、超时
- synthesize_speech（TTS）：成功路径、429/401/5xx、空音频、落盘
- _ensure_token 缓存命中/未命中
- 异常映射到 ProviderError 的分类
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.voice.base import (
    TranscribeRequest,
)
from app.services.voice.providers import (
    AliyunVoiceProvider,
    _AliyunAPIError,
    _AliyunAuthError,
    _AliyunRateLimitError,
    _AliyunTimeoutError,
    _AliyunVoiceClient,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "environment": "development",
        "ai_aliyun_voice_api_key": SecretStr("test-api-key"),
        "ai_aliyun_voice_app_key": SecretStr("test-app-key"),
        "ai_aliyun_voice_region": "cn-shanghai",
    }
    base.update(overrides)
    return Settings(**base)


class FakeResponse:
    """模拟 httpx.Response。"""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        content: bytes = b"",
        content_type: str = "application/octet-stream",
    ) -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content
        self.headers = {"content-type": content_type}

    def json(self) -> Any:
        return self._json


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    http_client: Any | None = None,
    token: str | None = None,
) -> _AliyunVoiceClient:
    monkeypatch.setattr("app.services.voice.providers.settings", settings)
    kwargs: dict[str, Any] = {}
    if http_client is not None:
        kwargs["http_client"] = http_client
    if token is not None:
        kwargs["token"] = token
    return _AliyunVoiceClient(
        api_key=settings.ai_aliyun_voice_api_key,
        app_key=settings.ai_aliyun_voice_app_key,
        region=settings.ai_aliyun_voice_region,
        access_key_id=settings.ai_aliyun_voice_access_key_id,
        access_key_secret=settings.ai_aliyun_voice_access_key_secret,
        **kwargs,
    )


# ======================================================================
# recognize_audio（一句话识别 ASR）
# ======================================================================


@pytest.mark.asyncio
async def test_recognize_audio_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一句话识别成功路径：status=20000000，返回转写文本。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={
                "status": 20000000,
                "result": "我今年28岁",
                "message": "SUCCESS",
            },
        )
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    result = await client.recognize_audio(
        audio_bytes=b"\x00" * 32000,
        audio_format="mp3",
        sample_rate=16000,
        model="paraformer-realtime-v2",
    )
    assert result["text"] == "我今年28岁"
    assert result["confidence"] == 1.0
    assert result["duration_ms"] > 0
    assert result["language"] == "zh-CN"


@pytest.mark.asyncio
async def test_recognize_audio_empty_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASR 返回空文本应抛 _AliyunAPIError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"status": 20000000, "result": "", "message": "SUCCESS"},
        )
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunAPIError):
        await client.recognize_audio(
            audio_bytes=b"\x00" * 100, audio_format="mp3",
            sample_rate=16000, model="paraformer-realtime-v2",
        )


@pytest.mark.asyncio
async def test_recognize_audio_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASR status=40000001（token 无效）映射 _AliyunAuthError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"status": 40000001, "result": "", "message": "invalid token"},
        )
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunAuthError):
        await client.recognize_audio(
            audio_bytes=b"\x00" * 100, audio_format="mp3",
            sample_rate=16000, model="paraformer-realtime-v2",
        )


@pytest.mark.asyncio
async def test_recognize_audio_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASR status=40000005（限流）映射 _AliyunRateLimitError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"status": 40000005, "result": "", "message": "too many"},
        )
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunRateLimitError):
        await client.recognize_audio(
            audio_bytes=b"\x00" * 100, audio_format="mp3",
            sample_rate=16000, model="paraformer-realtime-v2",
        )


@pytest.mark.asyncio
async def test_recognize_audio_server_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 500 映射 _AliyunAPIError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(status_code=500)
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunAPIError):
        await client.recognize_audio(
            audio_bytes=b"\x00" * 100, audio_format="mp3",
            sample_rate=16000, model="paraformer-realtime-v2",
        )


@pytest.mark.asyncio
async def test_recognize_audio_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接超时映射 _AliyunTimeoutError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(side_effect=TimeoutError("timed out"))
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunTimeoutError):
        await client.recognize_audio(
            audio_bytes=b"\x00" * 100, audio_format="mp3",
            sample_rate=16000, model="paraformer-realtime-v2",
        )


# ======================================================================
# synthesize_speech（TTS）
# ======================================================================


@pytest.mark.asyncio
async def test_synthesize_speech_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTS 成功：音频落盘，返回相对路径。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200, content=b"FAKE_AUDIO_BYTES"
        )
    )
    fake_http.aclose = AsyncMock()
    written_files: list[tuple[str, bytes]] = []

    async def fake_writer(name: str, data: bytes) -> str:
        written_files.append((name, data))
        return f"/storage/uploads/{name}"

    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    client._file_writer = fake_writer
    result = await client.synthesize_speech(
        text="你好世界", voice="default", model="cosyvoice-v1",
        audio_format="mp3", sample_rate=16000, speed=1.0,
    )
    assert result["audio_url"].startswith("/storage/uploads/tts/")
    assert result["duration_ms"] > 0
    assert written_files[0][1] == b"FAKE_AUDIO_BYTES"


@pytest.mark.asyncio
async def test_synthesize_speech_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTS 429 映射 _AliyunRateLimitError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(status_code=429)
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunRateLimitError):
        await client.synthesize_speech(
            text="test", voice="default", model="cosyvoice-v1",
            audio_format="mp3", sample_rate=16000, speed=1.0,
        )


@pytest.mark.asyncio
async def test_synthesize_speech_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTS 401 映射 _AliyunAuthError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(status_code=401)
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunAuthError):
        await client.synthesize_speech(
            text="test", voice="default", model="cosyvoice-v1",
            audio_format="mp3", sample_rate=16000, speed=1.0,
        )


@pytest.mark.asyncio
async def test_synthesize_speech_empty_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTS 返回空音频映射 _AliyunAPIError。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(status_code=200, content=b"")
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunAPIError):
        await client.synthesize_speech(
            text="test", voice="xiaoyun", model="cosyvoice-v1",
            audio_format="mp3", sample_rate=16000, speed=1.0,
        )


@pytest.mark.asyncio
async def test_synthesize_speech_json_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTS 失败时返回 JSON 错误体（HTTP 200），按 status 映射异常。"""
    settings = _settings()
    fake_http = AsyncMock()
    fake_http.post = AsyncMock(
        return_value=FakeResponse(
            status_code=200,
            json_data={"status": 41020001, "result": "", "message": "Engine error"},
            content_type="application/json",
        )
    )
    fake_http.aclose = AsyncMock()
    client = _make_client(monkeypatch, settings, http_client=fake_http, token="tok")
    with pytest.raises(_AliyunAPIError):
        await client.synthesize_speech(
            text="test", voice="xiaoyun", model="cosyvoice-v1",
            audio_format="mp3", sample_rate=16000, speed=1.0,
        )


# ======================================================================
# _ensure_token 缓存
# ======================================================================


@pytest.mark.asyncio
async def test_token_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """构造时注入 token，_ensure_token 应直接返回不调 API。"""
    settings = _settings()
    client = _make_client(monkeypatch, settings, token="injected-token")
    token = await client._ensure_token()
    assert token == "injected-token"


@pytest.mark.asyncio
async def test_token_cache_miss_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无注入 token + 有 AccessKey 时应调 _fetch_nls_token。"""
    settings = _settings(
        ai_aliyun_voice_access_key_id=SecretStr("ak-id"),
        ai_aliyun_voice_access_key_secret=SecretStr("ak-secret"),
    )
    client = _make_client(monkeypatch, settings)
    # 注入 fake _fetch_nls_token 避免真实网络调用。
    async def fake_fetch(*a: Any, **kw: Any) -> tuple[str, int]:
        return "fetched-token", 3600

    monkeypatch.setattr(
        "app.services.voice.stream_provider._fetch_nls_token", fake_fetch
    )
    token = await client._ensure_token()
    assert token == "fetched-token"
    # 第二次应命中缓存。
    token2 = await client._ensure_token()
    assert token2 == "fetched-token"


# ======================================================================
# AliyunVoiceProvider 异常映射
# ======================================================================


@pytest.mark.asyncio
async def test_provider_transcribe_maps_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider.transcribe 把 _AliyunAuthError 映射为 ProviderError。"""
    from app.services.voice.base import ProviderError

    settings = _settings()
    monkeypatch.setattr("app.services.voice.providers.settings", settings)

    fake_client = AsyncMock()
    fake_client.recognize_audio = AsyncMock(
        side_effect=_AliyunAuthError("bad token", 401)
    )
    provider = AliyunVoiceProvider(client=fake_client)
    request = TranscribeRequest(audio_bytes=b"\x00" * 100)
    with pytest.raises(ProviderError) as exc_info:
        await provider.transcribe(request)
    assert exc_info.value.code == "AI_INPUT_INVALID"


@pytest.mark.asyncio
async def test_provider_transcribe_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider.transcribe 成功返回 TranscribeResult。"""
    from app.services.voice.base import TranscribeResult

    settings = _settings()
    monkeypatch.setattr("app.services.voice.providers.settings", settings)

    fake_client = AsyncMock()
    fake_client.recognize_audio = AsyncMock(
        return_value={"text": "你好", "confidence": 0.99, "duration_ms": 1000,
                      "language": "zh-CN"}
    )
    provider = AliyunVoiceProvider(client=fake_client)
    request = TranscribeRequest(audio_bytes=b"\x00" * 100)
    result = await provider.transcribe(request)
    assert isinstance(result, TranscribeResult)
    assert result.text == "你好"
    assert result.confidence == 0.99
