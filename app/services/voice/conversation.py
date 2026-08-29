"""实时半双工语音对话编排器（P-04b）。

:class:`VoiceConversationOrchestrator` 协调一轮对话的完整流程：
``final_transcript`` → 画像抽取（AIGateway.structured_extract）→ 流式拼回复。
默认不合成 TTS；调用方再 ``synthesize_current()`` 或 ``process_transcript(...,
synthesize=True)`` 时才进入 SPEAKING。

编排器管理对话轮次状态（listening → processing → speaking → idle），协调
ASR client、AIGateway 与 VoiceGateway 的交互。不直接持有 WebSocket 引用，
由 WS 路由层负责消息收发与推送；编排器只做流程编排与状态流转。

审计复用 :class:`GatewayCallRecord`：日志和审计记录不含原始音频、原始文本
内容、密钥。
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.config import settings
from app.services.ai.base import (
    AITaskContext,
    ProviderError,
    ProviderErrorKind,
    ReplyRequest,
    StructuredExtractRequest,
    StructuredExtractResult,
)
from app.services.ai.gateway import AIGateway
from app.services.ai.prompts.voice_reply import build_voice_reply_prompt
from app.services.voice.base import (
    SynthesizeRequest,
    SynthesizeResult,
)
from app.services.voice.gateway import VoiceGateway

logger = logging.getLogger(__name__)

# 按标点分句：中文句末标点（。！？；）+ 换行；标点缺失时每 ~15 字兜底切一刀。
_SENTENCE_END_RE = re.compile(r"[。！？；!?\n]+")
_FALLBACK_CHUNK_CHARS = 15


def _split_sentences(text: str) -> list[str]:
    """把回复文本按标点切分成句子列表。

    优先按中文句末标点（。！？；）和换行切分，标点缺失时每 ~15 字兜底
    切一刀（避免超长无标点文本一次性喂给 TTS）。空文本返回空列表。
    """
    text = text.strip()
    if not text:
        return []
    # 按标点切分，保留标点在句尾。
    parts = _SENTENCE_END_RE.split(text)
    sentences: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) > _FALLBACK_CHUNK_CHARS * 2:
            # 无标点超长段：按兜底字数切分。
            for i in range(0, len(part), _FALLBACK_CHUNK_CHARS):
                sentences.append(part[i : i + _FALLBACK_CHUNK_CHARS])
        else:
            sentences.append(part)
    return sentences or [text]


class ConversationState(str, Enum):
    """半双工对话轮次状态机。"""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


@dataclass
class ConversationTurnResult:
    """一轮对话的编排结果（不含原始音频/文本，仅传递给 WS 路由层推送）。"""

    final_transcript: str = ""
    extracted_field_key: str | None = None
    extracted_value: Any = None
    ai_reply: str = ""
    field_key: str | None = None
    tts_audio_url: str | None = None
    tts_duration_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class VoiceConversationOrchestrator:
    """半双工语音对话编排器。

    管理对话轮次状态，协调 ASR → 画像抽取 → TTS 完整流程。

    ``ai_gateway`` 与 ``voice_gateway`` 可注入 mock，测试时跳过真实 provider。
    ``reply_builder`` 可注入自定义整段回复函数（非流式回退）。
    ``reply_streamer`` 可注入 ``stream_chat``；提供时本轮不再走 ``_build_reply``。
    """

    ai_gateway: AIGateway
    voice_gateway: VoiceGateway
    reply_builder: Any = None
    reply_streamer: Any = None
    state: ConversationState = field(default=ConversationState.IDLE)
    # 当前轮次的审计上下文与画像会话元数据。
    _session_id: str = ""
    _field_key: str = ""
    _last_reply_text: str = ""
    _generation_id: int = 0
    _last_request_id: str = ""
    _last_extract_result: StructuredExtractResult | None = None
    # 对话全程转写累积：每轮 append，结束时一次性批量抽取
    _all_transcripts: list[str] = field(default_factory=list)

    def start_listening(
        self,
        session_id: str,
        field_key: str = "",
        request_id: str = "",
    ) -> None:
        """开始一轮监听：设置会话上下文，状态切到 LISTENING。"""
        if self.state not in (ConversationState.IDLE, ConversationState.SPEAKING):
            raise RuntimeError(
                f"对话状态非 idle/speaking，无法开始监听: {self.state}"
            )
        self._session_id = session_id
        self._field_key = field_key
        self.state = ConversationState.LISTENING

    def bump_generation(self) -> int:
        """自增 generation，调用方用它丢弃过期的流式回复。"""
        self._generation_id += 1
        return self._generation_id

    async def process_transcript(
        self,
        final_transcript: str,
        *,
        user_id: int,
        consent_version: str = "consent-v1",
        policy_revision: str = "ai-policy-v1",
        request_id: str = "",
        synthesize: bool = False,
    ) -> ConversationTurnResult:
        """处理最终转写文本：画像抽取 → 流式拼回复；默认不合成 TTS。

        状态流转：LISTENING → PROCESSING → IDLE。
        仅当 ``synthesize=True`` 时再进入 SPEAKING。
        抽取失败不打断口语回复。
        """
        result = ConversationTurnResult(final_transcript=final_transcript)
        async for _kind, _text in self.stream_reply_events(
            final_transcript,
            user_id=user_id,
            request_id=request_id,
            consent_version=consent_version,
            policy_revision=policy_revision,
        ):
            pass

        extracted = self._last_extract_result
        if extracted is not None and extracted.fields:
            field = extracted.fields[0]
            result.extracted_field_key = field.field_key
            result.extracted_value = field.value
            result.field_key = field.field_key
        result.ai_reply = self._last_reply_text
        if not synthesize:
            return result
        spoken = await self.synthesize_current()
        result.tts_audio_url = spoken.tts_audio_url
        result.tts_duration_ms = spoken.tts_duration_ms
        result.error_code = spoken.error_code
        result.error_message = spoken.error_message
        return result

    async def stream_reply_events(
        self,
        final_transcript: str,
        *,
        user_id: int,
        request_id: str = "",
        consent_version: str = "consent-v1",
        policy_revision: str = "ai-policy-v1",
    ) -> AsyncIterator[tuple[str, str]]:
        """直接生成回复，不做每轮画像抽取。抽取延迟到对话结束后批量做。

        状态流转：LISTENING → PROCESSING → IDLE。
        仅生成口语回复（确认 + 追问），不调 structured_extract（省 ~15s）。
        """
        if self.state != ConversationState.LISTENING:
            raise RuntimeError(
                f"对话状态非 listening，无法处理转写: {self.state}"
            )
        self.state = ConversationState.PROCESSING
        self._last_reply_text = ""
        self._last_request_id = request_id
        self._last_extract_result = None
        # 累积本轮转写，供对话结束后批量抽取
        self._all_transcripts.append(final_transcript)
        gen = self._generation_id
        try:
            if callable(self.reply_streamer):
                messages = [
                    {
                        "role": "user",
                        "content": build_voice_reply_prompt(
                            final_transcript, self._field_key, ()
                        ),
                    }
                ]
                async for kind, text in self.reply_streamer(
                    messages, json_mode=False
                ):
                    if self._generation_id != gen:
                        return
                    if kind == "content":
                        self._last_reply_text += text
                    yield (kind, text)
            else:
                reply_text = await self._build_reply_fast(
                    final_transcript, request_id
                )
                if self._generation_id != gen:
                    return
                self._last_reply_text = reply_text
                yield ("content", reply_text)
                yield ("finish", "stop")
        finally:
            if self._generation_id == gen:
                self.state = ConversationState.IDLE

    async def synthesize_current(self) -> ConversationTurnResult:
        """对 _last_reply_text 合成 TTS。无文本则返回空 URL。"""
        result = ConversationTurnResult(ai_reply=self._last_reply_text)
        extracted = self._last_extract_result
        if extracted is not None and extracted.fields:
            field = extracted.fields[0]
            result.extracted_field_key = field.field_key
            result.extracted_value = field.value
            result.field_key = field.field_key
        if not self._last_reply_text:
            logger.warning(
                "conversation_empty_reply session=%s request_id=%s",
                self._session_id,
                self._last_request_id,
            )
            return result

        self.state = ConversationState.SPEAKING
        tts_context = AITaskContext(
            task_id=uuid.uuid4().hex,
            request_id=self._last_request_id or uuid.uuid4().hex,
            scene="voice_conversation_tts",
            provider=settings.ai_voice_provider,
            model=settings.ai_voice_model_name,
            schema_version="voice-tts-v1",
        )
        tts_request = SynthesizeRequest(text=self._last_reply_text)
        tts_outcome = await self.voice_gateway.synthesize(
            tts_context, tts_request
        )
        if tts_outcome.result is None:
            result.error_code = tts_outcome.error_code
            result.error_message = tts_outcome.error_message
            self.state = ConversationState.IDLE
            return result

        tts_result: SynthesizeResult = tts_outcome.result
        result.tts_audio_url = tts_result.audio_url
        result.tts_duration_ms = tts_result.duration_ms
        self.state = ConversationState.IDLE
        return result

    async def synthesize_streaming(self) -> AsyncIterator[tuple[str, int]]:
        """流式合成 TTS：按句切分 ``_last_reply_text``，逐句合成并落盘。

        yield ``(audio_url, duration_ms)``，每个对应一句话的独立音频文件。
        前端按顺序链式播放以降低首音延迟。任一句合成失败时 raise
        :class:`ProviderError`，由 WS 路由层捕获后回退到整段模式。

        状态流转：SPEAKING → IDLE。
        """
        if not self._last_reply_text:
            logger.warning(
                "conversation_streaming_empty session=%s request_id=%s",
                self._session_id,
                self._last_request_id,
            )
            return
        self.state = ConversationState.SPEAKING
        sentences = _split_sentences(self._last_reply_text)
        # 延迟导入避免循环依赖（providers 导入 stream_provider）。
        from app.services.voice.stream_tts_provider import (
            AliyunStreamTTSClient,
        )

        app_key = (
            settings.ai_aliyun_voice_app_key.get_secret_value()
            if settings.ai_aliyun_voice_app_key
            else ""
        )
        voice = settings.ai_aliyun_voice_tts_model or "longxiaochun"
        try:
            for sentence in sentences:
                audio_url, duration_ms = await self._synthesize_one_sentence(
                    sentence,
                    app_key=app_key,
                    voice=voice,
                    client_factory=AliyunStreamTTSClient,
                )
                yield (audio_url, duration_ms)
        except Exception:
            # 任何句子失败都让 WS 路由层回退整段模式；先恢复状态。
            self.state = ConversationState.IDLE
            raise
        self.state = ConversationState.IDLE

    async def _synthesize_one_sentence(
        self,
        sentence: str,
        *,
        app_key: str,
        voice: str,
        client_factory: Any,
    ) -> tuple[str, int]:
        """合成单句：建连 → RunSynthesis → 收音频帧 → 落盘 → 返回 (url, dur)。

        ``client_factory`` 注入 :class:`AliyunStreamTTSClient`（测试可注入
        mock）。
        """
        client = client_factory()
        await client.connect(
            app_key,
            voice=voice,
            audio_format="mp3",
            sample_rate=16000,
        )
        audio_bytes = bytearray()
        try:
            await client.synthesize(sentence)
            await client.finish()
            async for chunk in client.audio_chunks():
                audio_bytes.extend(chunk)
        finally:
            await client._close()  # noqa: SLF001
        if not audio_bytes:
            raise ProviderError(
                code="AI_TEMPORARILY_UNAVAILABLE",
                message="流式 TTS 返回空音频",
                kind=ProviderErrorKind.RETRYABLE,
            )
        audio_url = await self._write_tts_file(bytes(audio_bytes), "mp3")
        # 估算时长（与 providers._AliyunVoiceClient 对齐：250ms/字）。
        duration_ms = min(30000, max(500, len(sentence) * 250))
        return (audio_url, duration_ms)

    async def _write_tts_file(
        self, audio_bytes: bytes, audio_format: str
    ) -> str:
        """把音频二进制落盘到 upload_dir/voice/tts，返回相对 URL。

        与 :meth:`providers._AliyunVoiceClient.synthesize_speech` 的落盘逻辑
        对齐，路径 ``/storage/uploads/voice/tts/<ts>-<rand>.mp3``。
        """
        import aiofiles

        filename = (
            f"voice/tts/{int(time.time() * 1000)}-"
            f"{os.urandom(4).hex()}.{audio_format}"
        )
        full_path = os.path.join(settings.upload_dir, filename)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        async with aiofiles.open(full_path, "wb") as f:
            await f.write(audio_bytes)
        return f"/storage/uploads/{filename}"

    async def _extract_in_memory(
        self,
        final_transcript: str,
        *,
        consent_version: str,
        policy_revision: str,
        request_id: str,
    ) -> StructuredExtractResult:
        """画像抽取仅留在内存；失败时用空 fields 继续口语回复。"""
        extract_context = AITaskContext(
            task_id=uuid.uuid4().hex,
            request_id=request_id or uuid.uuid4().hex,
            scene="voice_conversation_extract",
            provider=settings.ai_provider,
            model=settings.ai_model_name,
            schema_version="profile-extract-v1",
        )
        extract_request = StructuredExtractRequest(
            subject="personal",
            turn_texts=(final_transcript,),
            consent_version=consent_version,
            policy_revision=policy_revision,
        )
        extract_outcome = await self.ai_gateway.structured_extract(
            extract_context, extract_request
        )
        if extract_outcome.result is None:
            logger.warning(
                "conversation_extract_failed session=%s request_id=%s code=%s",
                self._session_id,
                request_id,
                extract_outcome.error_code,
            )
            return StructuredExtractResult(fields=())
        return extract_outcome.result

    async def _build_reply_fast(
        self,
        transcript: str,
        request_id: str,
    ) -> str:
        """快速回复：不做画像抽取，直接用 transcript 生成口语回复。

        LLM 失败时降级到模板回复，保证对话不中断。
        """
        reply_context = AITaskContext(
            task_id=uuid.uuid4().hex,
            request_id=request_id or uuid.uuid4().hex,
            scene="voice_conversation_reply",
            provider=settings.ai_provider,
            model=settings.ai_model_name,
            schema_version="voice-reply-v1",
        )
        reply_request = ReplyRequest(
            transcript=transcript,
            field_key=self._field_key,
            known_fields=(),
        )
        try:
            outcome = await self.ai_gateway.generate_reply(
                reply_context, reply_request
            )
            if outcome.result is not None and outcome.result.reply_text:
                return outcome.result.reply_text
        except Exception:  # noqa: BLE001
            logger.warning(
                "conversation_reply_failed session=%s request_id=%s, "
                "降级模板回复",
                self._session_id,
                request_id,
            )
        # 降级模板
        return "好的，我了解了，请继续。"

    async def extract_all(
        self,
        *,
        user_id: int,
        request_id: str = "",
        consent_version: str = "consent-v1",
        policy_revision: str = "ai-policy-v1",
    ) -> StructuredExtractResult:
        """对话结束后批量抽取：把累积的全部转写一次性交给 structured_extract。

        不在每轮调用，仅在对话结束时调用一次，省去逐轮抽取的延迟。
        """
        if not self._all_transcripts:
            return StructuredExtractResult(fields=())
        all_text = "\n".join(self._all_transcripts)
        result = await self._extract_in_memory(
            all_text,
            consent_version=consent_version,
            policy_revision=policy_revision,
            request_id=request_id,
        )
        return result

    def transcript_text(self) -> str:
        """全部累积转写的合并文本（WP-P5 finish 落库用；clear 后为空）。"""
        return "\n".join(self._all_transcripts)

    def clear_transcripts(self) -> None:
        """清空累积转写（新对话开始时调用）。"""
        self._all_transcripts.clear()

    def reset(self) -> None:
        """重置编排器到 IDLE 状态（异常恢复用）。"""
        self.state = ConversationState.IDLE
        self._session_id = ""
        self._field_key = ""
        self._last_reply_text = ""
        self._last_request_id = ""
        self._last_extract_result = None
        self._all_transcripts.clear()


__all__ = [
    "ConversationState",
    "ConversationTurnResult",
    "VoiceConversationOrchestrator",
]
