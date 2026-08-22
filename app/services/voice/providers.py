"""Voice providers and the provider registry.

``AliyunVoiceProvider`` is the real provider using Alibaba Cloud Intelligent
Speech (智能语音交互), covering both ASR (one-sentence recognition REST API +
realtime WS streaming) and TTS (cosyvoice).  Development/testing uses injected
fake http clients / WS connections; production enablement requires the full
approval gate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.services.voice.base import (
    ProviderError,
    ProviderErrorKind,
    StreamTranscribeRequest,
    SynthesizeRequest,
    SynthesizeResult,
    TranscribeRequest,
    TranscribeResult,
    VoiceProvider,
)

logger = logging.getLogger(__name__)


# ==================== Aliyun voice provider ====================
#
# 阿里云智能语音交互（NLS）提供 ASR 与 TTS 能力。开发档通过 HTTP REST API
# 调用（一句话识别 / 录音文件识别 + 语音合成），不依赖长连接 SDK，与现有
# asyncio 架构兼容。生产环境真正启用前需完成 DPA / 数据出境审查。
#
# 异常映射策略与 DeepSeekAIProvider 一致：
#   - 限流 (429)        → AI_QUOTA_EXCEEDED (RETRYABLE)
#   - 超时 / 连接失败    → AI_TEMPORARILY_UNAVAILABLE (RETRYABLE)
#   - 鉴权失败 (401/403) → AI_INPUT_INVALID (NON_RETRYABLE, 配置错误)
#   - 其他 5xx          → AI_TEMPORARILY_UNAVAILABLE (RETRYABLE)


class AliyunVoiceProvider:
    """Alibaba Cloud Intelligent Speech provider (ASR + TTS).

    Uses the NLS REST API for both speech-to-text (paraformer) and
    text-to-speech (cosyvoice).  Development/testing only; production
    enablement requires the full approval gate (see config.py).

    The provider accepts a ``client`` kwarg for test injection, mirroring
    DeepSeekAIProvider's pattern.
    """

    def __init__(self, **kwargs: Any) -> None:
        api_key = settings.ai_aliyun_voice_api_key
        app_key = settings.ai_aliyun_voice_app_key
        if (api_key is None or app_key is None) and "client" not in kwargs:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message=(
                    "阿里云语音 provider 缺少配置，请在 .env 配置 "
                    "AI_ALIYUN_VOICE_API_KEY 与 AI_ALIYUN_VOICE_APP_KEY"
                    "（仅开发/测试环境）"
                ),
                kind=ProviderErrorKind.NON_RETRYABLE,
            )
        # 测试可通过 kwargs 注入 mock client 与 mock 流式 client 工厂。
        self._client = kwargs.pop("client", None) or _AliyunVoiceClient(
            api_key=api_key,
            app_key=app_key,
            region=settings.ai_aliyun_voice_region,
            access_key_id=settings.ai_aliyun_voice_access_key_id,
            access_key_secret=settings.ai_aliyun_voice_access_key_secret,
        )
        self._asr_model = settings.ai_aliyun_voice_asr_model
        self._tts_model = settings.ai_aliyun_voice_tts_model
        # 实时流式 ASR client 工厂（测试可注入 mock）。
        self._stream_asr_factory = kwargs.pop(
            "stream_asr_factory", None
        ) or _default_stream_asr_factory

    # ------------------------------------------------------------------
    # VoiceProvider Protocol
    # ------------------------------------------------------------------
    async def transcribe(
        self, request: TranscribeRequest
    ) -> TranscribeResult:
        """Call Alibaba Cloud one-sentence ASR (一句话识别) and map to typed result."""
        try:
            raw = await self._client.recognize_audio(
                audio_bytes=request.audio_bytes,
                audio_format=request.audio_format,
                sample_rate=request.sample_rate,
                model=self._asr_model,
            )
        except _AliyunRateLimitError as exc:
            raise ProviderError(
                code="AI_QUOTA_EXCEEDED",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
                retry_after_ms=2000,
            ) from exc
        except (_AliyunTimeoutError, _AliyunConnectionError) as exc:
            raise ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
            ) from exc
        except _AliyunAuthError as exc:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message=str(exc),
                kind=ProviderErrorKind.NON_RETRYABLE,
            ) from exc
        except _AliyunAPIError as exc:
            raise ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
            ) from exc

        text = str(raw.get("text", "")).strip()
        if not text:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message="ASR 返回空转写文本",
                kind=ProviderErrorKind.NON_RETRYABLE,
            )
        return TranscribeResult(
            text=text,
            confidence=float(raw.get("confidence", 1.0)),
            duration_ms=int(raw.get("duration_ms", 0)),
            detected_language=raw.get("language") or "zh-CN",
        )

    async def synthesize(
        self, request: SynthesizeRequest
    ) -> SynthesizeResult:
        """Call Alibaba Cloud TTS (cosyvoice) and map to typed result."""
        try:
            raw = await self._client.synthesize_speech(
                text=request.text,
                voice=request.voice,
                model=self._tts_model,
                audio_format=request.audio_format,
                sample_rate=request.sample_rate,
                speed=request.speed,
            )
        except _AliyunRateLimitError as exc:
            raise ProviderError(
                code="AI_QUOTA_EXCEEDED",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
                retry_after_ms=2000,
            ) from exc
        except (_AliyunTimeoutError, _AliyunConnectionError) as exc:
            raise ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
            ) from exc
        except _AliyunAuthError as exc:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message=str(exc),
                kind=ProviderErrorKind.NON_RETRYABLE,
            ) from exc
        except _AliyunAPIError as exc:
            raise ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message=str(exc),
                kind=ProviderErrorKind.RETRYABLE,
            ) from exc

        audio_url = str(raw.get("audio_url", "")).strip()
        if not audio_url:
            raise ProviderError(
                code="AI_INPUT_INVALID",
                message="TTS 返回空音频地址",
                kind=ProviderErrorKind.NON_RETRYABLE,
            )
        return SynthesizeResult(
            audio_url=audio_url,
            audio_format=request.audio_format,
            duration_ms=int(raw.get("duration_ms", 0)),
            expires_at=raw.get("expires_at"),
        )

    async def stream_transcribe(
        self,
        request: StreamTranscribeRequest,
        on_partial: Any | None = None,
    ):
        """实时流式语音识别：创建 AliyunStreamASRClient 并返回它。

        返回一个已创建但未连接的 client 对象，由 WS 路由层驱动
        ``send_chunk`` / ``finish`` / ``partial_results`` 生命周期，完整
        支持半双工对话的"边说边出字"语义。

        ``on_partial`` 在真实 provider 下不直接使用（部分结果由路由层通过
        ``client.partial_results`` async generator 消费）；保留参数是为了与
        Protocol 签名对齐。
        """
        client = self._stream_asr_factory(
            app_key=request.app_key,
            model=request.model,
        )
        # 连接由路由层在 audio_start 时显式调用，这里只创建实例。
        return client
# 隔离 HTTP 细节与 provider 逻辑：provider 只负责异常映射与类型化，
# client 负责 HTTP 调用与响应解析。测试注入 mock client 即可覆盖全部分支。


class _AliyunVoiceError(Exception):
    """Base for Aliyun client errors."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code


class _AliyunRateLimitError(_AliyunVoiceError):
    """HTTP 429."""


class _AliyunTimeoutError(_AliyunVoiceError):
    """Request timeout."""


class _AliyunConnectionError(_AliyunVoiceError):
    """Network/connection failure."""


class _AliyunAuthError(_AliyunVoiceError):
    """HTTP 401/403 — configuration error, not transient."""


class _AliyunAPIError(_AliyunVoiceError):
    """Other API errors (5xx etc.)."""


def _default_stream_asr_factory(
    app_key: str | None = None,
    model: str = "paraformer-realtime-v2",
) -> Any:
    """默认实时 ASR client 工厂：实例化 AliyunStreamASRClient。

    延迟导入避免循环依赖（stream_provider 导入本模块的错误类族）。
    测试可通过 ``stream_asr_factory`` kwarg 注入 mock 工厂。
    """
    from app.services.voice.stream_provider import AliyunStreamASRClient

    return AliyunStreamASRClient()


class _AliyunVoiceClient:
    """Thin HTTP wrapper for Alibaba Cloud NLS REST API.

    Methods return plain dicts; the provider maps them to typed results and
    translates errors to ``ProviderError``.  Audio files are read from local
    storage (``audio_ref``) — the route layer stores uploaded audio before
    enqueuing the task, so the provider never receives raw bytes.

    接受 ``http_client`` / ``file_writer`` / ``token`` kwargs 用于测试注入：
    前两者注入 mock httpx 客户端与文件写入函数，后者注入预生成的 NLS Token
    避免测试时真实换取 Token。
    """

    def __init__(
        self,
        api_key: Any,
        app_key: Any,
        region: str = "cn-shanghai",
        *,
        http_client: Any | None = None,
        file_writer: Any | None = None,
        token: str | None = None,
        access_key_id: Any | None = None,
        access_key_secret: Any | None = None,
    ) -> None:
        self._api_key = (
            api_key.get_secret_value()
            if hasattr(api_key, "get_secret_value")
            else api_key
        )
        self._app_key = (
            app_key.get_secret_value()
            if hasattr(app_key, "get_secret_value")
            else app_key
        )
        self._region = region
        self._base_url = f"https://nls-meta.{region}.aliyuncs.com"
        self._http_client = http_client
        self._file_writer = file_writer
        self._token = token
        # TTS 也需要 NLS Token 鉴权（与 ASR 同源 AccessKey→Token）。
        self._access_key_id = _ak_secret_value(access_key_id) if access_key_id else None
        self._access_key_secret = _ak_secret_value(access_key_secret) if access_key_secret else None
        self._token_cache: tuple[str, float] | None = (token, float("inf")) if token else None

    async def _ensure_token(self) -> str:
        """获取 NLS Token，优先用构造时注入的 token，否则用 AccessKey 换取。

        TTS 和 ASR 共用同一套阿里云 NLS 鉴权：AccessKey → Token。
        """
        if self._token_cache is not None:
            token, expires_at = self._token_cache
            if expires_at == float("inf") or time.time() < expires_at - 300:
                return token
        if not self._access_key_id or not self._access_key_secret:
            # 没有 AccessKey 配置：返回空字符串（mock 联调或测试注入场景）。
            return self._token or ""
        # 复用 stream_provider 的 Token 获取逻辑。
        from app.services.voice.stream_provider import _fetch_nls_token

        token, expires_in = await _fetch_nls_token(
            access_key_id=self._access_key_id,
            access_key_secret=self._access_key_secret,
            region=self._region,
            http_client=self._http_client,
        )
        self._token_cache = (token, time.time() + expires_in)
        return token

    async def recognize_audio(
        self,
        audio_bytes: bytes,
        audio_format: str,
        sample_rate: int,
        model: str,
    ) -> dict[str, Any]:
        """Call Aliyun one-sentence ASR (一句话识别): POST raw audio → text.

        Aliyun NLS REST API (``/stream/v1/asr``).  Audio bytes are POSTed
        directly (``Content-Type: application/octet-stream``); NLS Token is
        reused from :meth:`_ensure_token` (same token as TTS).  Response is
        JSON with a ``status`` field: ``20000000`` = success, anything else =
        failure (see error mapping below).  Tests inject a fake ``http_client``.
        """
        import httpx

        client = self._http_client or httpx.AsyncClient(timeout=30.0)
        try:
            params = {
                "appkey": self._app_key,
                "format": audio_format,
                "sample_rate": str(sample_rate),
                "enable_punctuation_prediction": "true",
                "enable_inverse_text_normalization": "true",
            }
            asr_url = (
                f"https://nls-gateway.{self._region}.aliyuncs.com"
                "/stream/v1/asr"
            )
            headers = {
                "X-NLS-Token": await self._ensure_token(),
                "Content-Type": "application/octet-stream",
            }
            response = await client.post(
                asr_url, params=params, headers=headers, content=audio_bytes
            )
            status_code = response.status_code
            if status_code == 429:
                raise _AliyunRateLimitError("NLS ASR 限流", status_code)
            if status_code in (401, 403):
                raise _AliyunAuthError(
                    f"NLS ASR 鉴权失败: {status_code}", status_code
                )
            if status_code >= 500:
                raise _AliyunAPIError(
                    f"NLS ASR 服务端错误: {status_code}", status_code
                )
            if status_code != 200:
                raise _AliyunAPIError(
                    f"NLS ASR 异常响应: {status_code}", status_code
                )
            payload = response.json()
            asr_status = int(payload.get("status", 0))
            if asr_status != 20000000:
                message = str(payload.get("message", "ASR 识别失败"))
                if asr_status == 40000001:
                    raise _AliyunAuthError(message, status_code)
                if asr_status == 40000005:
                    raise _AliyunRateLimitError(message, status_code)
                if asr_status >= 50000000:
                    raise _AliyunAPIError(message, status_code)
                raise _AliyunAPIError(message, status_code)
            text = str(payload.get("result", "")).strip()
            if not text:
                raise _AliyunAPIError("ASR 返回空转写文本")
            # 一句话识别不返回 duration，按音频字节数估算（16kHz/16bit/单声道）。
            duration_ms = int(
                len(audio_bytes) / (sample_rate * 2) * 1000
            ) if sample_rate > 0 else 0
            return {
                "text": text,
                "confidence": 1.0,
                "duration_ms": duration_ms,
                "language": "zh-CN",
            }
        except _AliyunVoiceError:
            raise
        except (TimeoutError, OSError) as exc:
            raise _AliyunTimeoutError(
                f"NLS ASR 超时: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise _AliyunConnectionError(
                f"NLS ASR 连接失败: {type(exc).__name__}"
            ) from exc
        finally:
            if self._http_client is None and hasattr(client, "aclose"):
                await client.aclose()

    async def synthesize_speech(
        self,
        text: str,
        voice: str,
        model: str,
        audio_format: str,
        sample_rate: int,
        speed: float,
    ) -> dict[str, Any]:
        """Call TTS: synthesize text → return {audio_url, duration_ms, ...}.

        阿里云 NLS 语音合成 HTTP API（cosyvoice）。合成音频落盘到
        ``settings.upload_dir`` 下的 voice/tts 子目录，返回可访问的相对路径
        供前端播放。测试通过 ``http_client`` / ``file_writer`` kwarg 注入 mock。
        """
        import os
        import time

        import aiofiles
        import httpx

        client = self._http_client or httpx.AsyncClient(timeout=30.0)
        file_writer = self._file_writer
        try:
            # 阿里云 NLS 语音合成 HTTP 提交（/stream/v1/tts，与 ASR 对称）：
            # 参数走 query string，成功返回音频二进制，失败返回 JSON。
            tts_url = (
                f"https://nls-gateway.{self._region}.aliyuncs.com"
                "/stream/v1/tts"
            )
            params = {
                "appkey": self._app_key,
                "text": text,
                "format": audio_format,
                "sample_rate": str(sample_rate),
                "voice": voice,
                "volume": "50",
                "speech_rate": str(int((speed - 1.0) * 100)),
            }
            headers = {
                "X-NLS-Token": await self._ensure_token(),
            }
            response = await client.post(tts_url, params=params, headers=headers)
            status_code = response.status_code
            if status_code == 429:
                raise _AliyunRateLimitError(
                    "NLS TTS 限流", status_code
                )
            if status_code in (401, 403):
                raise _AliyunAuthError(
                    f"NLS TTS 鉴权失败: {status_code}", status_code
                )
            if status_code >= 500:
                raise _AliyunAPIError(
                    f"NLS TTS 服务端错误: {status_code}", status_code
                )
            if status_code != 200:
                raise _AliyunAPIError(
                    f"NLS TTS 异常响应: {status_code}", status_code
                )
            # 音频二进制内容落盘，返回相对路径作为 audio_url。
            # 失败时阿里云返回 JSON（HTTP 仍 200），按 Content-Type 区分。
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                error_payload = response.json()
                asr_status = int(error_payload.get("status", 0))
                message = str(error_payload.get("message", "TTS 合成失败"))
                if asr_status == 40000001:
                    raise _AliyunAuthError(message, status_code)
                if asr_status == 40000005:
                    raise _AliyunRateLimitError(message, status_code)
                raise _AliyunAPIError(
                    f"NLS TTS 失败(status={asr_status}): {message}",
                    status_code,
                )
            audio_bytes = response.content
            if not audio_bytes:
                raise _AliyunAPIError("NLS TTS 返回空音频")
            filename = f"tts/{int(time.time() * 1000)}-{os.urandom(4).hex()}.{audio_format}"
            if file_writer is not None:
                result = await file_writer(filename, audio_bytes)
                audio_url = result if isinstance(result, str) else filename
            else:
                full_path = os.path.join(settings.upload_dir, filename)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                async with aiofiles.open(full_path, "wb") as f:
                    await f.write(audio_bytes)
                audio_url = f"/storage/uploads/{filename}"
            # 估算时长（无精确元数据时按 250ms/字估算，与 mock 对齐）。
            duration_ms = min(
                30000, max(1000, len(text) * 250)
            )
            return {
                "audio_url": audio_url,
                "duration_ms": duration_ms,
                "expires_at": None,
            }
        except _AliyunVoiceError:
            raise
        except (TimeoutError, OSError) as exc:
            raise _AliyunTimeoutError(
                f"NLS TTS 超时: {type(exc).__name__}"
            ) from exc
        except Exception as exc:
            raise _AliyunConnectionError(
                f"NLS TTS 连接失败: {type(exc).__name__}"
            ) from exc
        finally:
            if self._http_client is None and hasattr(client, "aclose"):
                await client.aclose()


# ==================== Registry ====================


class VoiceProviderRegistry:
    """Provider registry keyed by configuration name."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., VoiceProvider]] = {
            "aliyun": AliyunVoiceProvider,
        }

    def register(self, name: str, factory: Callable[..., VoiceProvider]) -> None:
        self._factories[name] = factory

    def create(self, name: str = "aliyun", **kwargs: Any) -> VoiceProvider:
        if name not in self._factories:
            raise KeyError(f"未知 voice provider: {name}")
        return self._factories[name](**kwargs)


_voice_registry = VoiceProviderRegistry()


def get_voice_provider(name: str = "aliyun", **kwargs: Any) -> VoiceProvider:
    """Return a voice provider instance from the shared registry."""
    return _voice_registry.create(name, **kwargs)


__all__ = [
    "AliyunVoiceProvider",
    "VoiceProviderRegistry",
    "get_voice_provider",
]


def _ak_secret_value(value: Any) -> str | None:
    """从 SecretStr 或裸值提取字符串，None 透传。"""
    if value is None:
        return None
    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    return str(value)
