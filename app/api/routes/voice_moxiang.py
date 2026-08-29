"""墨相师·AI 引路人 对话 WebSocket 路由。

路径：``/api/v1/voice/moxiang-master``。

与 ``/voice/conversation`` 的区别：
- 使用墨相师人设提示词（非 voice_reply 的 ≤30 字资料采集）
- 维护多轮对话历史（内存级，WS 连接生命周期内有效）
- 不做画像字段抽取
- 新增 ``text_message`` 文字通道（文字模式不经 ASR）
- 流式推送 ``ai_content`` 增量

消息协议（前后端共享契约）：

前端 → 后端::

    {"type": "session_start"}
    {"type": "audio_start"}
    {"type": "audio_chunk", "data": "<base64 PCM>", "seq": 1}
    {"type": "audio_end"}
    {"type": "text_message", "text": "..."}
    {"type": "listen"}
    {"type": "revise_text", "text": "..."}
    {"type": "cancel"}

后端 → 前端::

    {"type": "session_ready"}
    {"type": "partial_transcript", "text": "..."}
    {"type": "final_transcript", "text": "..."}
    {"type": "ai_thinking"}
    {"type": "ai_content", "text": "..."}        # 流式增量
    {"type": "ai_reply", "text": "..."}           # 完整回复
    {"type": "tts_audio", "audio_url": "...", "duration_ms": 3000}
    {"type": "error", "code": "...", "message": "..."}

门禁：语音模式需 ``ai_voice_conversation_enabled`` + ``ai_voice_enabled``；
文字模式仅需 AI provider 非 mock。fail closed。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import text as sql_text

from app.core.config import settings
from app.core.security import decode_access_token
from app.services.ai.flags import AiFeature, require_ai_feature
from app.services.ai.gateway import AIGateway
from app.services.ai.base import ProviderError as AIProviderError
from app.services.ai.prompts.moxiang_master import (
    OPENING_MESSAGE,
    _format_narrative_context,
)
from app.services.voice.base import (
    STREAM_CHUNK_MAX_BYTES,
    StreamTranscribeRequest,
)
from app.services.voice.gateway import VoiceGateway
from app.services.voice.master_orchestrator import MoxiangMasterOrchestrator
from app.services.voice.providers import (
    _AliyunVoiceError,
    get_voice_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter()

WS_CLOSE_NORMAL = 1000
WS_CLOSE_POLICY_VIOLATION = 1008

WS_MESSAGE_MAX_BYTES = STREAM_CHUNK_MAX_BYTES * 2

# 用户消息文本长度上限。
_MAX_TEXT_LENGTH = 2000


def _check_voice_feature() -> str | None:
    """语音门禁：返回 None 表示通过。"""
    if not settings.ai_voice_conversation_enabled:
        return "AI_FEATURE_DISABLED"
    if not settings.ai_voice_enabled:
        return "AI_FEATURE_DISABLED"
    try:
        require_ai_feature(AiFeature.VOICE_CONVERSATION, settings)
    except Exception:  # noqa: BLE001
        return "AI_FEATURE_DISABLED"
    return None


def _authenticate_ws(token: str) -> dict[str, str] | None:
    try:
        return decode_access_token(token)
    except (ValueError, KeyError):
        return None


async def _send_json(ws: WebSocket, message: dict[str, Any]) -> None:
    try:
        await ws.send_text(json.dumps(message, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        logger.debug("moxiang_ws_send_failed: connection likely closed")


async def _send_error(
    ws: WebSocket, code: str, message: str
) -> None:
    logger.info("moxiang_ws_error code=%s", code)
    await _send_json(ws, {"type": "error", "code": code, "message": message})


async def _load_narrative_context(user_id: int) -> str:
    """读取用户已发布的「我的墨相」画像叙事层，渲染成 prompt 片段。"""
    from app.db.session import session_factory

    if session_factory is None:
        return ""
    try:
        async with session_factory() as db:
            result = await db.execute(
                sql_text(
                    "SELECT summary_text, status "
                    "FROM ai_profile_summary "
                    "WHERE user_id = :uid AND subject = 'personal' "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"uid": user_id},
            )
            row = result.mappings().first()
            if row is None or not row.get("summary_text"):
                return ""
            raw = str(row["summary_text"])
            data = json.loads(raw) if raw else None
            if not isinstance(data, dict):
                return ""
            return _format_narrative_context(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "moxiang_narrative_load_failed user_id=%s err=%s",
            user_id,
            type(exc).__name__,
        )
        return ""


async def _push_streamed_reply(
    ws: WebSocket,
    orchestrator: MoxiangMasterOrchestrator,
    user_text: str,
    *,
    request_id: str,
) -> None:
    """流式推送墨相师回复：ai_thinking → ai_content* → ai_reply。"""
    await _send_json(ws, {"type": "ai_thinking"})
    full_reply = ""
    try:
        async for kind, chunk_text in orchestrator.stream_reply(
            user_text, request_id=request_id
        ):
            if kind == "content":
                full_reply += chunk_text
                await _send_json(
                    ws, {"type": "ai_content", "text": chunk_text}
                )
            # reasoning 不推送给前端（与 voice_ws 一致）
    except AIProviderError as exc:
        await _send_error(ws, exc.code, exc.message)
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "moxiang_stream_failed request_id=%s err=%s",
            request_id,
            type(exc).__name__,
        )
        await _send_error(
            ws, "AI_TEMPORARILY_UNAVAILABLE", "墨相师暂时无法回复"
        )
        return
    if full_reply:
        await _send_json(ws, {"type": "ai_reply", "text": full_reply})


async def _drain_partials(ws: WebSocket, asr_client: Any) -> None:
    try:
        async for partial_text in asr_client.partial_results():
            await _send_json(
                ws, {"type": "partial_transcript", "text": partial_text}
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "moxiang_partial_drain_error err=%s", type(exc).__name__
        )


@router.websocket("/moxiang-master")
async def moxiang_master_conversation(
    ws: WebSocket,
    token: str = Query(default=""),
) -> None:
    """墨相师·AI 引路人 对话 WebSocket 端点。

    鉴权：query 参数 ``token`` 传 JWT。
    语音门禁：``ai_voice_conversation_enabled`` 关闭时仍允许文字模式
    （连接不拒绝，但 ``audio_start`` 会返回错误）。
    """
    # JWT 鉴权
    if not token:
        await ws.close(
            code=WS_CLOSE_POLICY_VIOLATION, reason="缺少 token 参数"
        )
        return
    payload = _authenticate_ws(token)
    if payload is None:
        await ws.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="无效或已过期的访问令牌",
        )
        return

    user_id = int(payload["sub"])
    request_id = uuid.uuid4().hex

    await ws.accept()
    logger.info(
        "moxiang_ws_connected user_id=%s request_id=%s",
        user_id,
        request_id,
    )

    voice_enabled = _check_voice_feature() is None

    orchestrator = MoxiangMasterOrchestrator(
        ai_gateway=AIGateway(),
        voice_gateway=VoiceGateway(
            timeout_seconds=settings.ai_gateway_timeout_seconds
        ),
    )
    asr_client: Any = None
    partial_task: asyncio.Task[None] | None = None

    try:
        while True:
            raw = await ws.receive_text()
            if not raw:
                continue
            if len(raw.encode("utf-8")) > WS_MESSAGE_MAX_BYTES:
                await _send_error(ws, "AI_INPUT_INVALID", "消息体过大")
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(ws, "AI_INPUT_INVALID", "消息非有效 JSON")
                continue

            msg_type = message.get("type")

            if msg_type == "session_start":
                # 读取用户画像上下文
                narrative_ctx = await _load_narrative_context(user_id)
                orchestrator.set_narrative_context(narrative_ctx)
                await _send_json(ws, {"type": "session_ready"})
                # 推送开场白
                await _send_json(
                    ws, {"type": "ai_reply", "text": OPENING_MESSAGE}
                )
                logger.info(
                    "moxiang_session_start user_id=%s request_id=%s "
                    "has_narrative=%s",
                    user_id,
                    request_id,
                    bool(narrative_ctx),
                )

            elif msg_type == "text_message":
                text_content = str(message.get("text", "")).strip()
                if not text_content:
                    await _send_error(
                        ws, "AI_INPUT_INVALID", "消息内容为空"
                    )
                    continue
                if len(text_content) > _MAX_TEXT_LENGTH:
                    await _send_error(
                        ws,
                        "AI_INPUT_INVALID",
                        f"消息过长（上限 {_MAX_TEXT_LENGTH} 字）",
                    )
                    continue
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    text_content,
                    request_id=request_id,
                )

            elif msg_type == "audio_start":
                if not voice_enabled:
                    await _send_error(
                        ws, "AI_FEATURE_DISABLED", "语音对话功能当前不可用"
                    )
                    continue
                voice_provider = get_voice_provider(
                    settings.ai_voice_provider
                )
                stream_request = StreamTranscribeRequest(
                    app_key=(
                        settings.ai_aliyun_voice_app_key.get_secret_value()
                        if settings.ai_aliyun_voice_app_key
                        else ""
                    ),
                    model=settings.ai_aliyun_voice_asr_model,
                    field_key="",
                )
                asr_result = await voice_provider.stream_transcribe(
                    stream_request, on_partial=None
                )
                asr_client = asr_result
                try:
                    await asr_client.connect(
                        app_key=stream_request.app_key,
                        model=stream_request.model,
                    )
                except _AliyunVoiceError:
                    await _send_error(
                        ws,
                        "AI_TEMPORARILY_UNAVAILABLE",
                        "语音识别连接失败",
                    )
                    asr_client = None
                    continue
                partial_task = asyncio.create_task(
                    _drain_partials(ws, asr_client)
                )
                logger.info(
                    "moxiang_audio_start request_id=%s", request_id
                )

            elif msg_type == "audio_chunk":
                if asr_client is None:
                    continue
                b64data = str(message.get("data", ""))
                if not b64data:
                    continue
                try:
                    pcm_bytes = base64.b64decode(b64data)
                except Exception:  # noqa: BLE001
                    continue
                if len(pcm_bytes) > STREAM_CHUNK_MAX_BYTES:
                    continue
                try:
                    await asr_client.send_chunk(pcm_bytes)
                except _AliyunVoiceError:
                    await _send_error(
                        ws,
                        "AI_TEMPORARILY_UNAVAILABLE",
                        "语音识别写入失败",
                    )

            elif msg_type == "audio_end":
                if asr_client is None:
                    continue
                if partial_task is not None:
                    partial_task.cancel()
                    partial_task = None
                try:
                    final_transcript = await asr_client.finish()
                except _AliyunVoiceError:
                    await _send_error(
                        ws,
                        "AI_TEMPORARILY_UNAVAILABLE",
                        "语音识别结束失败",
                    )
                    asr_client = None
                    continue
                if final_transcript:
                    await _send_json(
                        ws,
                        {"type": "final_transcript", "text": final_transcript},
                    )
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    final_transcript,
                    request_id=request_id,
                )
                asr_client = None

            elif msg_type == "listen":
                if not orchestrator._last_reply_text:  # noqa: SLF001
                    continue
                spoken = await orchestrator.synthesize_current()
                if spoken.error_code:
                    await _send_error(
                        ws,
                        spoken.error_code,
                        spoken.error_message or "播报失败",
                    )
                    continue
                if spoken.tts_audio_url:
                    await _send_json(
                        ws,
                        {
                            "type": "tts_audio",
                            "audio_url": spoken.tts_audio_url,
                            "duration_ms": spoken.tts_duration_ms,
                        },
                    )

            elif msg_type == "revise_text":
                rev_text = str(message.get("text", "")).strip()
                if not rev_text:
                    await _send_error(
                        ws, "AI_INPUT_INVALID", "改写文本为空"
                    )
                    continue
                orchestrator.bump_generation()
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    rev_text,
                    request_id=request_id,
                )

            elif msg_type == "cancel":
                if asr_client is not None:
                    try:
                        await asr_client.finish()
                    except Exception:  # noqa: BLE001
                        pass
                    asr_client = None
                if partial_task is not None:
                    partial_task.cancel()
                    partial_task = None
                orchestrator.reset()
                logger.info(
                    "moxiang_cancelled request_id=%s", request_id
                )

    except WebSocketDisconnect:
        logger.info(
            "moxiang_ws_disconnected user_id=%s request_id=%s",
            user_id,
            request_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "moxiang_ws_unhandled request_id=%s err=%s",
            request_id,
            type(exc).__name__,
        )
        await _send_json(
            ws,
            {
                "type": "error",
                "code": "AI_TEMPORARILY_UNAVAILABLE",
                "message": "墨相师服务暂时不可用",
            },
        )
    finally:
        if partial_task is not None:
            partial_task.cancel()
        if asr_client is not None:
            try:
                await asr_client._close()  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass
        try:
            await ws.close(code=WS_CLOSE_NORMAL)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["router"]
