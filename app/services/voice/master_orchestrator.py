"""墨相师·AI 引路人的对话编排器。

与 :class:`VoiceConversationOrchestrator` 的区别：
- 维护多轮对话历史（由路由从持久化轮次恢复后，连接内继续累积）
- 使用墨相师人设提示词（非 voice_reply 的 ≤30 字资料采集助手）
- 不做画像字段抽取（不调 structured_extract / extract_all）
- 回复无硬性字数上限，适合人设化对话

语音链路仍复用 Aliyun ASR（由 WS 路由层管理 ASR client）和
VoiceGateway.synthesize 做 TTS。编排器只负责：消息组装 → stream_chat
流式生成 → 回复累积 → TTS 合成。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator

from app.core.config import settings
from app.services.ai.audit import GenerationAuditEvent, record_generation_audit
from app.services.ai.base import AITaskContext
from app.services.ai.gateway import AIGateway
from app.services.ai.prompts.moxiang_master import (
    MOXIANG_MASTER_PROMPT_VERSION,
    build_master_prompt,
)
from app.services.ai.providers import get_provider
from app.services.voice.base import SynthesizeRequest, SynthesizeResult
from app.services.voice.gateway import VoiceGateway

logger = logging.getLogger(__name__)

# 内存历史保留的轮次上限（1 轮 = 1 user + 1 assistant）。
_MAX_HISTORY_TURNS = 12


class MasterState(str, Enum):
    """墨相师对话状态机。"""

    IDLE = "idle"
    PROCESSING = "processing"
    SPEAKING = "speaking"


@dataclass
class MasterTurnResult:
    """一轮墨相师对话的结果。"""

    ai_reply: str = ""
    tts_audio_url: str | None = None
    tts_duration_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class MoxiangMasterOrchestrator:
    """墨相师对话编排器。

    ``ai_gateway`` 用于审计上下文（stream_chat 本身绕过 Gateway，
    与现有 voice reply 一致）。
    ``voice_gateway`` 用于 TTS 合成。
    ``narrative_context`` 在 session_start 时一次性读取，会话内不刷新。
    """

    ai_gateway: AIGateway
    voice_gateway: VoiceGateway
    state: MasterState = field(default=MasterState.IDLE)
    _history: list[dict[str, str]] = field(default_factory=list)
    _narrative_context: str = ""
    _build_context: str = ""
    _last_reply_text: str = ""
    _generation_id: int = 0
    _last_request_id: str = ""

    def set_narrative_context(self, context: str) -> None:
        """设置用户画像上下文（session_start 时调用）。"""
        self._narrative_context = context

    def set_build_context(self, context: str) -> None:
        """设置建构模式上下文（缺失硬字段/已确认摘要/进度），空串=纯聊模式。"""
        self._build_context = context

    def hydrate_history(self, turns: list[dict[str, str]]) -> None:
        """Restore the recent persisted dialogue for a resumed WS session.

        The orchestrator is connection-scoped, while ``ai_profile_turn`` is
        session-scoped.  Rehydrating before the next reply keeps reconnects and
        subject switches on the same conversational context without retaining
        unbounded or malformed rows.
        """
        restored: list[dict[str, str]] = []
        for turn in turns:
            role = str(turn.get("role") or "")
            content = str(turn.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            restored.append({"role": role, "content": content})
        self._history = restored[-(_MAX_HISTORY_TURNS * 2):]
        self._last_reply_text = ""

    async def stream_reply(
        self,
        user_text: str,
        *,
        request_id: str = "",
    ) -> AsyncIterator[tuple[str, str]]:
        """流式生成墨相师回复。

        组装多轮消息 → provider.stream_chat → 逐段 yield。
        回复完成后追加到内存历史。

        yield 的 kind: ``reasoning`` / ``content`` / ``finish``。
        """
        self.state = MasterState.PROCESSING
        self._last_reply_text = ""
        self._last_request_id = request_id
        gen = self._generation_id
        started = time.monotonic()
        messages = build_master_prompt(
            user_text, self._history, self._narrative_context,
            build_context=self._build_context,
        )
        error_code: str | None = None
        try:
            provider = get_provider(settings.ai_provider)
            provider_name = settings.ai_provider
            provider_model = getattr(provider, "_model", None) or getattr(
                provider, "model", None
            )
            full_reply = ""
            async for kind, text in provider.stream_chat(
                messages, json_mode=False
            ):
                if self._generation_id != gen:
                    return
                if kind == "content":
                    full_reply += text
                    yield (kind, text)
                elif kind == "reasoning":
                    yield (kind, text)
                elif kind == "finish":
                    yield (kind, text)
            # 累积历史
            self._last_reply_text = full_reply
            self._history.append({"role": "user", "content": user_text})
            self._history.append({"role": "assistant", "content": full_reply})
            if len(self._history) > _MAX_HISTORY_TURNS * 2:
                self._history = self._history[-(_MAX_HISTORY_TURNS * 2):]
        except Exception as exc:
            error_code = type(exc).__name__
            logger.warning(
                "moxiang_master_reply_failed request_id=%s err=%s",
                request_id,
                type(exc).__name__,
            )
            self._last_reply_text = ""
            raise
        finally:
            if self._generation_id == gen:
                self.state = MasterState.IDLE
            duration_ms = int((time.monotonic() - started) * 1000)
            await record_generation_audit(
                GenerationAuditEvent(
                    request_id=request_id or uuid.uuid4().hex,
                    task_id=None,
                    scene="moxiang_master_chat",
                    provider=provider_name,
                    model=provider_model,
                    prompt_version=MOXIANG_MASTER_PROMPT_VERSION,
                    schema_version="moxiang-master-v1",
                    policy_revision=settings.ai_retention_policy_version or "ai-policy-2026-08-07-v1",
                    status="succeeded" if error_code is None else "failed",
                    error_code=error_code,
                    duration_ms=duration_ms,
                    input_revision={"history": len(self._history)},
                    display_eligible=True,
                )
            )

    async def synthesize_current(self) -> MasterTurnResult:
        """对 _last_reply_text 合成 TTS。无文本则返回空。"""
        result = MasterTurnResult(ai_reply=self._last_reply_text)
        if not self._last_reply_text:
            return result
        self.state = MasterState.SPEAKING
        tts_context = AITaskContext(
            task_id=uuid.uuid4().hex,
            request_id=self._last_request_id or uuid.uuid4().hex,
            scene="moxiang_master_tts",
            provider=settings.ai_voice_provider,
            model=settings.ai_voice_model_name,
            schema_version="moxiang-tts-v1",
        )
        tts_request = SynthesizeRequest(text=self._last_reply_text)
        tts_outcome = await self.voice_gateway.synthesize(
            tts_context, tts_request
        )
        if tts_outcome.result is None:
            result.error_code = tts_outcome.error_code
            result.error_message = tts_outcome.error_message
            self.state = MasterState.IDLE
            return result
        tts_result: SynthesizeResult = tts_outcome.result
        result.tts_audio_url = tts_result.audio_url
        result.tts_duration_ms = tts_result.duration_ms
        self.state = MasterState.IDLE
        return result

    def bump_generation(self) -> None:
        """作废正在进行的流式回复（用户编辑转写重试时调用）。"""
        self._generation_id += 1
        self.state = MasterState.IDLE

    def reset(self) -> None:
        """重置编排器状态（不清空历史，保留对话记忆）。"""
        self.bump_generation()
        self._last_reply_text = ""
