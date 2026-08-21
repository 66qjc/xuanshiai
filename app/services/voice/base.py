"""Voice-CORE typed request/result contracts, Provider protocol.

Parallel to ``app.services.ai.base`` for text AI.  STT (speech-to-text) and
TTS (text-to-speech) are a separate capability domain: different vendor
(Alibaba Cloud vs DeepSeek), audio I/O, and lifecycle.  Reuses
``ProviderError``/``ProviderErrorKind``/``AITaskContext``/``GatewayCallRecord``
from ``ai.base`` so the error taxonomy and audit shape stay unified.

Business modules depend only on the ``VoiceProvider`` Protocol and these
dataclasses; they never import a vendor SDK.  All provider output is 100%
typed and validated by the VoiceGateway before it can reach a response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

# 复用 ai.base 的通用类型：错误分类、审计上下文、审计记录。
# 这些类型跨域共享，不携带任何文本/音频语义，复用避免重复造轮子。
from app.services.ai.base import (
    AITaskContext,
    GatewayCallRecord,
    ProviderError,
    ProviderErrorKind,
)

# 音频格式与采样率约束（与前端 VoiceRecorder 录音参数对齐：
# format=mp3, sampleRate=16000, numberOfChannels=1, duration<=60000ms）。
ALLOWED_AUDIO_FORMATS = frozenset({"mp3", "wav", "m4a", "aac"})
DEFAULT_SAMPLE_RATE = 16000
MAX_AUDIO_DURATION_SECONDS = 60
MAX_TTS_TEXT_LENGTH = 500

# 实时半双工对话音频格式：前端通过 WebSocket 流式发送 PCM 音频块。
# 约束：PCM 16kHz 16bit 单声道，base64 编码后放入 audio_chunk.data。
STREAM_AUDIO_FORMAT = "pcm"
STREAM_SAMPLE_RATE = 16000
STREAM_AUDIO_CHANNELS = 1
STREAM_AUDIO_SAMPLE_BITS = 16
# 单个 PCM 块最大字节数（base64 解码后），约 320ms 音频 @16k/16bit/单声道。
STREAM_CHUNK_MAX_BYTES = 10 * 1024


@dataclass(frozen=True)
class TranscribeRequest:
    """Minimal input for speech-to-text.

    ``audio_ref`` is a storage reference (relative path or object key) to the
    uploaded audio file; never raw bytes or a public URL.  The Gateway and
    provider resolve it to the actual storage location.

    ``question_field_key`` carries the profile field key (e.g. ``"age"``)
    for mock fixture matching; real providers ignore it.
    """

    audio_ref: str
    audio_format: str = "mp3"
    sample_rate: int = DEFAULT_SAMPLE_RATE
    max_duration_seconds: int = MAX_AUDIO_DURATION_SECONDS
    locale: str | None = None
    question_field_key: str = ""


class TranscribeResult(BaseModel):
    """Typed provider result for speech-to-text."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=2000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    duration_ms: int = Field(default=0, ge=0)
    # provider 返回的原始语言代码（如 zh-CN），用于下游记录，非业务必填。
    detected_language: str | None = None


@dataclass(frozen=True)
class SynthesizeRequest:
    """Minimal input for text-to-speech.

    ``text`` is the question text to synthesize; never raw user input or
    profile data.  Length is capped at ``MAX_TTS_TEXT_LENGTH`` by the route
    layer before reaching the provider.
    """

    text: str
    voice: str = "default"
    locale: str | None = None
    speed: float = 1.0
    audio_format: str = "mp3"
    sample_rate: int = DEFAULT_SAMPLE_RATE


# ----------------------------------------------------------------------
# 实时半双工语音对话（streaming ASR）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class StreamTranscribeRequest:
    """流式语音识别输入。

    与 :class:`TranscribeRequest` 不同：实时模式不引用已上传文件，而是由
    WebSocket 路由层将 base64 PCM 块解码后逐块喂给 provider。``field_key``
    用于 mock fixture 匹配（与 TranscribeRequest.question_field_key 同义）。
    """

    app_key: str
    model: str = "paraformer-realtime-v2"
    sample_rate: int = STREAM_SAMPLE_RATE
    field_key: str = ""


@dataclass(frozen=True)
class PartialTranscript:
    """一次流式识别的中间或最终结果片段。

    ``is_final=False`` 时为部分识别结果（SentenceResult，边说边出字）；
    ``is_final=True`` 时为本轮对话最终文本（TranscriptionResult）。
    """

    text: str
    is_final: bool = False
    confidence: float = 1.0


class SynthesizeResult(BaseModel):
    """Typed provider result for text-to-speech.

    ``audio_url`` is a time-limited, signed URL (or relative storage path) the
    frontend plays via ``uni.createInnerAudioContext``.  ``expires_at`` lets the
    frontend decide whether to re-request before expiry.
    """

    model_config = ConfigDict(extra="forbid")

    audio_url: str = Field(..., min_length=1, max_length=1024)
    audio_format: str = Field(default="mp3", pattern="^(mp3|wav|m4a|aac)$")
    duration_ms: int = Field(default=0, ge=0)
    expires_at: str | None = None


class VoiceProvider(Protocol):
    """Voice provider adapter interface.

    Phase 4 / P-04: one mock implementation and one Alibaba Cloud
    implementation.  Development/testing only; production enablement requires
    the full approval gates (see config.py).

    ``stream_transcribe`` is optional (real-time half-duplex conversation,
    P-04b): providers that do not support streaming may omit it.
    """

    async def transcribe(
        self, request: TranscribeRequest
    ) -> TranscribeResult: ...

    async def synthesize(
        self, request: SynthesizeRequest
    ) -> SynthesizeResult: ...

    async def stream_transcribe(
        self,
        request: StreamTranscribeRequest,
        on_partial: Any,
    ) -> AsyncIterator[PartialTranscript]: ...


__all__ = [
    "ALLOWED_AUDIO_FORMATS",
    "DEFAULT_SAMPLE_RATE",
    "MAX_AUDIO_DURATION_SECONDS",
    "MAX_TTS_TEXT_LENGTH",
    "STREAM_AUDIO_FORMAT",
    "STREAM_SAMPLE_RATE",
    "STREAM_AUDIO_CHANNELS",
    "STREAM_AUDIO_SAMPLE_BITS",
    "STREAM_CHUNK_MAX_BYTES",
    "TranscribeRequest",
    "TranscribeResult",
    "SynthesizeRequest",
    "SynthesizeResult",
    "StreamTranscribeRequest",
    "PartialTranscript",
    "VoiceProvider",
    "ProviderError",
    "ProviderErrorKind",
    "AITaskContext",
    "GatewayCallRecord",
]
