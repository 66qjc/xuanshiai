"""实时半双工语音对话编排器（P-04b）。

:class:`VoiceConversationOrchestrator` 协调一轮对话的完整流程：
``final_transcript`` → 画像抽取（AIGateway.structured_extract）→ 生成回复文本 →
TTS 合成（VoiceGateway.synthesize）→ 返回 ``ai_reply`` + ``tts_audio``。

编排器管理对话轮次状态（listening → processing → speaking → idle），协调
ASR client、AIGateway 与 VoiceGateway 的交互。不直接持有 WebSocket 引用，
由 WS 路由层负责消息收发与推送；编排器只做流程编排与状态流转。

审计复用 :class:`GatewayCallRecord`：日志和审计记录不含原始音频、原始文本
内容、密钥。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.core.config import settings
from app.services.ai.base import AITaskContext, StructuredExtractRequest
from app.services.ai.gateway import AIGateway
from app.services.ai.base import StructuredExtractResult
from app.services.voice.base import (
    SynthesizeRequest,
    SynthesizeResult,
)
from app.services.voice.gateway import VoiceGateway

logger = logging.getLogger(__name__)


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
    ``reply_builder`` 可注入自定义回复生成函数（默认用内置 question bank 逻辑）。
    """

    ai_gateway: AIGateway
    voice_gateway: VoiceGateway
    reply_builder: Any = None
    state: ConversationState = field(default=ConversationState.IDLE)
    # 当前轮次的审计上下文与画像会话元数据。
    _session_id: str = ""
    _field_key: str = ""

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

    async def process_transcript(
        self,
        final_transcript: str,
        *,
        user_id: int,
        consent_version: str = "consent-v1",
        policy_revision: str = "ai-policy-v1",
        request_id: str = "",
    ) -> ConversationTurnResult:
        """处理最终转写文本：画像抽取 → 生成回复 → TTS 合成。

        状态流转：LISTENING → PROCESSING → SPEAKING → IDLE。
        任一步骤失败时填充 ``error_code``/``error_message`` 并提前返回。
        """
        if self.state != ConversationState.LISTENING:
            raise RuntimeError(
                f"对话状态非 listening，无法处理转写: {self.state}"
            )
        self.state = ConversationState.PROCESSING
        result = ConversationTurnResult(final_transcript=final_transcript)

        # 1. 画像抽取（AIGateway.structured_extract）。
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
            result.error_code = extract_outcome.error_code
            result.error_message = extract_outcome.error_message
            self.state = ConversationState.IDLE
            return result

        extracted: StructuredExtractResult = extract_outcome.result
        # 取第一个抽取字段（实时对话一轮聚焦一个字段）。
        if extracted.fields:
            field = extracted.fields[0]
            result.extracted_field_key = field.field_key
            result.extracted_value = field.value
            result.field_key = field.field_key

        # 2. 生成回复文本。
        reply_text = await self._build_reply(
            final_transcript, extracted, request_id
        )
        result.ai_reply = reply_text

        # 3. TTS 合成。
        self.state = ConversationState.SPEAKING
        if not reply_text:
            logger.warning(
                "conversation_empty_reply session=%s request_id=%s",
                self._session_id,
                request_id,
            )
            self.state = ConversationState.IDLE
            return result

        tts_context = AITaskContext(
            task_id=uuid.uuid4().hex,
            request_id=request_id or uuid.uuid4().hex,
            scene="voice_conversation_tts",
            provider=settings.ai_voice_provider,
            model=settings.ai_voice_model_name,
            schema_version="voice-tts-v1",
        )
        tts_request = SynthesizeRequest(text=reply_text)
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

    async def _build_reply(
        self,
        transcript: str,
        extracted: StructuredExtractResult,
        request_id: str,
    ) -> str:
        """生成回复文本。

        默认实现：若注入了 ``reply_builder`` 则委托它；否则返回固定确认 + 下一个
        问题的模板回复（开发档足够联调，生产替换为 LLM 对话生成）。
        """
        if self.reply_builder is not None:
            value = self.reply_builder(transcript, extracted, request_id)
            if hasattr(value, "__await__"):
                return await value  # type: ignore[no-any-return]
            return value  # type: ignore[no-any-return]
        # 内置模板：确认 + 追问下一个字段（开发档简单联调用）。
        if extracted.fields:
            field = extracted.fields[0]
            return f"好的，已记录你的信息。请继续告诉我你的{field.field_key}。"
        return "好的，我了解了，请继续。"

    def reset(self) -> None:
        """重置编排器到 IDLE 状态（异常恢复用）。"""
        self.state = ConversationState.IDLE
        self._session_id = ""
        self._field_key = ""


__all__ = [
    "ConversationState",
    "ConversationTurnResult",
    "VoiceConversationOrchestrator",
]
