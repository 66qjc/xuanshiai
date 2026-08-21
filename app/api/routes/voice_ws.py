"""实时半双工语音对话 WebSocket 路由（P-04b）。

路径：``/api/v1/voice/conversation``。

WebSocket 不经过 HTTP 中间件，需自行 JWT 鉴权（从 query 参数取 ``token``，
复用 :func:`app.core.security.decode_access_token`）。生产环境 fail closed：
``ai_voice_conversation_enabled`` 默认 false，生产环境连接时返回 close code 1008。

消息协议（前后端共享契约）：

前端 → 后端::

    {"type": "session_start", "session_id": "...", "field_key": "age"}
    {"type": "audio_start"}
    {"type": "audio_chunk", "data": "<base64 PCM>", "seq": 1}
    {"type": "audio_end"}
    {"type": "cancel"}

后端 → 前端::

    {"type": "partial_transcript", "text": "我今年28岁"}
    {"type": "final_transcript", "text": "我今年28岁，在北京工作"}
    {"type": "ai_thinking"}
    {"type": "ai_reply", "text": "...", "field_key": "city"}
    {"type": "tts_audio", "audio_url": "...", "duration_ms": 3000}
    {"type": "error", "code": "AI_TEMPORARILY_UNAVAILABLE", "message": "..."}

审计规范：日志和审计记录不含原始音频、原始文本内容、密钥。复用
:class:`GatewayCallRecord`。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.security import decode_access_token
from app.services.ai.flags import AiFeature, require_ai_feature
from app.services.ai.gateway import AIGateway
from app.services.ai.base import ProviderError as AIProviderError
from app.services.voice.base import (
    STREAM_CHUNK_MAX_BYTES,
    StreamTranscribeRequest,
)
from app.services.voice.conversation import VoiceConversationOrchestrator
from app.services.voice.gateway import VoiceGateway
from app.services.voice.providers import (
    _AliyunVoiceError,
    get_voice_provider,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# WebSocket close codes（RFC 6455 扩展码语义）。
WS_CLOSE_NORMAL = 1000
WS_CLOSE_POLICY_VIOLATION = 1008
WS_CLOSE_INTERNAL_ERROR = 1011

# 单条 WebSocket 消息最大字节（含 base64 音频块）。
WS_MESSAGE_MAX_BYTES = STREAM_CHUNK_MAX_BYTES * 2  # base64 膨胀约 4/3


def _require_conversation_feature() -> str | None:
    """实时对话门禁：返回 None 表示通过，返回 error_code 表示拒绝。

    生产环境 fail closed：``ai_voice_conversation_enabled`` 默认 false，
    生产环境连接时返回 close code 1008。
    """
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
    """验证 JWT token，返回 payload 或 None（鉴权失败）。

    WS 不经过 HTTP 中间件，需自行鉴权。复用
    :func:`app.core.security.decode_access_token`。
    """
    try:
        return decode_access_token(token)
    except (ValueError, KeyError):
        return None


async def _send_json(ws: WebSocket, message: dict[str, Any]) -> None:
    """发送一条 JSON 消息到 WebSocket，忽略已关闭连接的写入错误。"""
    try:
        await ws.send_text(json.dumps(message, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        logger.debug("ws_send_failed: connection likely closed")


async def _send_error(
    ws: WebSocket, code: str, message: str
) -> None:
    """推送 error 消息并记录审计日志（不含原始音频/文本）。"""
    logger.info("voice_ws_error code=%s", code)
    await _send_json(
        ws,
        {
            "type": "error",
            "code": code,
            "message": message,
        },
    )


@router.websocket("/conversation")
async def voice_conversation(
    ws: WebSocket,
    token: str = Query(default=""),
) -> None:
    """实时半双工语音对话 WebSocket 端点。

    鉴权：query 参数 ``token`` 传 JWT access token。
    门禁：``ai_voice_conversation_enabled`` 关闭时拒绝连接（close 1008）。
    """
    # 1. 门禁检查：fail closed。
    gate_error = _require_conversation_feature()
    if gate_error is not None:
        await ws.close(
            code=WS_CLOSE_POLICY_VIOLATION,
            reason="语音对话功能当前不可用",
        )
        return

    # 2. JWT 鉴权。
    if not token:
        await ws.close(
            code=WS_CLOSE_POLICY_VIOLATION, reason="缺少 token 参数"
        )
        return
    payload = _authenticate_ws(token)
    if payload is None:
        await ws.close(
            code=WS_CLOSE_POLICY_VIOLATION, reason="无效或已过期的访问令牌"
        )
        return

    user_id = int(payload["sub"])
    request_id = uuid.uuid4().hex

    # 3. 接受连接。
    await ws.accept()
    logger.info(
        "voice_ws_connected user_id=%s request_id=%s",
        user_id,
        request_id,
    )

    # 4. 对话状态。
    orchestrator: VoiceConversationOrchestrator | None = None
    asr_client: Any = None
    partial_task: asyncio.Task[None] | None = None
    session_id = ""
    field_key = ""

    try:
        while True:
            raw = await ws.receive_text()
            if not raw:
                continue
            if len(raw.encode("utf-8")) > WS_MESSAGE_MAX_BYTES:
                await _send_error(
                    ws, "AI_INPUT_INVALID", "消息体过大"
                )
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(
                    ws, "AI_INPUT_INVALID", "消息非有效 JSON"
                )
                continue

            msg_type = message.get("type")

            if msg_type == "session_start":
                session_id = str(message.get("session_id", ""))
                field_key = str(message.get("field_key", ""))
                # 初始化编排器（mock provider 用于开发联调）。
                orchestrator = VoiceConversationOrchestrator(
                    ai_gateway=AIGateway(),
                    voice_gateway=VoiceGateway(
                        timeout_seconds=settings.ai_gateway_timeout_seconds
                    ),
                )
                logger.info(
                    "voice_ws_session_start user_id=%s session=%s "
                    "field_key=%s request_id=%s",
                    user_id,
                    session_id,
                    field_key,
                    request_id,
                )

            elif msg_type == "audio_start":
                if orchestrator is None:
                    await _send_error(
                        ws, "AI_INPUT_INVALID", "未先发送 session_start"
                    )
                    continue
                # 创建 ASR client（mock 或真实 aliyun）。
                voice_provider = get_voice_provider(
                    settings.ai_voice_provider
                )
                stream_request = StreamTranscribeRequest(
                    app_key=settings.ai_aliyun_voice_app_key.get_secret_value()
                    if settings.ai_aliyun_voice_app_key
                    else "",
                    model=settings.ai_aliyun_voice_asr_model,
                    field_key=field_key,
                )
                # mock provider 返回 async generator；aliyun 返回 client 实例。
                # mock stream_transcribe 是 async generator function（含 yield），
                # 调用时返回 async generator，不能 await。Aliyun provider 是普通
                # coroutine（含 return），需 await。用 inspect 判断。
                raw_result = voice_provider.stream_transcribe(
                    stream_request, on_partial=None
                )
                if inspect.isasyncgen(raw_result):
                    # mock: async generator — 直接交给 drain task 消费。
                    asr_client = None
                    partial_task = asyncio.create_task(
                        _drain_mock_partials(ws, raw_result)
                    )
                else:
                    asr_result = await raw_result
                    if hasattr(asr_result, "connect"):
                        # 真实 aliyun client：需要显式 connect。
                        asr_client = asr_result
                        try:
                            await asr_client.connect(
                                app_key=stream_request.app_key,
                                model=stream_request.model,
                            )
                        except _AliyunVoiceError as exc:
                            await _send_error(
                                ws,
                                "AI_TEMPORARILY_UNAVAILABLE",
                                "语音识别连接失败",
                            )
                            logger.warning(
                                "voice_ws_asr_connect_failed request_id=%s "
                                "err=%s",
                                request_id,
                                type(exc).__name__,
                            )
                            asr_client = None
                            continue
                        # 启动 partial_results 后台消费。
                        partial_task = asyncio.create_task(
                            _drain_partials(ws, asr_client)
                        )
                    else:
                        # 非预期返回类型：按 mock generator 处理。
                        partial_task = asyncio.create_task(
                            _drain_mock_partials(ws, asr_result)
                        )
                orchestrator.start_listening(
                    session_id=session_id,
                    field_key=field_key,
                    request_id=request_id,
                )

            elif msg_type == "audio_chunk":
                if asr_client is None:
                    # mock 模式：audio_chunk 无操作（generator 自驱）。
                    continue
                data_b64 = message.get("data", "")
                try:
                    pcm_bytes = base64.b64decode(data_b64)
                except Exception:  # noqa: BLE001
                    await _send_error(
                        ws, "AI_INPUT_INVALID", "audio_chunk data 非 base64"
                    )
                    continue
                if len(pcm_bytes) > STREAM_CHUNK_MAX_BYTES:
                    await _send_error(
                        ws, "AI_INPUT_INVALID", "音频块过大"
                    )
                    continue
                try:
                    await asr_client.send_chunk(pcm_bytes)
                except _AliyunVoiceError:
                    await _send_error(
                        ws,
                        "AI_TEMPORARILY_UNAVAILABLE",
                        "音频发送失败",
                    )

            elif msg_type == "audio_end":
                if orchestrator is None:
                    continue
                # 获取最终转写文本。
                final_transcript = ""
                if asr_client is not None:
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
                else:
                    # mock generator：partial_task 完成后最终文本由 drain 收集。
                    if partial_task is not None:
                        await partial_task
                        partial_task = None
                    final_transcript = _mock_final_store.get("text", "")
                    _mock_final_store.pop("text", None)
                if final_transcript:
                    await _send_json(
                        ws,
                        {
                            "type": "final_transcript",
                            "text": final_transcript,
                        },
                    )
                # 画像抽取 → 回复 → TTS。
                await _send_json(ws, {"type": "ai_thinking"})
                try:
                    turn_result = await orchestrator.process_transcript(
                        final_transcript,
                        user_id=user_id,
                        request_id=request_id,
                    )
                except AIProviderError as exc:
                    await _send_error(ws, exc.code, exc.message)
                    continue
                if turn_result.error_code:
                    await _send_error(
                        ws,
                        turn_result.error_code,
                        turn_result.error_message or "对话处理失败",
                    )
                    continue
                await _send_json(
                    ws,
                    {
                        "type": "ai_reply",
                        "text": turn_result.ai_reply,
                        "field_key": turn_result.field_key or field_key,
                    },
                )
                if turn_result.tts_audio_url:
                    await _send_json(
                        ws,
                        {
                            "type": "tts_audio",
                            "audio_url": turn_result.tts_audio_url,
                            "duration_ms": turn_result.tts_duration_ms,
                        },
                    )
                asr_client = None

            elif msg_type == "cancel":
                # 取消当前轮次：清理 ASR client。
                if asr_client is not None:
                    try:
                        await asr_client.finish()
                    except Exception:  # noqa: BLE001
                        pass
                    asr_client = None
                if partial_task is not None:
                    partial_task.cancel()
                    partial_task = None
                if orchestrator is not None:
                    orchestrator.reset()
                logger.info(
                    "voice_ws_cancelled request_id=%s", request_id
                )

    except WebSocketDisconnect:
        logger.info(
            "voice_ws_disconnected user_id=%s request_id=%s",
            user_id,
            request_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "voice_ws_unhandled request_id=%s err=%s",
            request_id,
            type(exc).__name__,
        )
        await _send_json(
            ws,
            {
                "type": "error",
                "code": "AI_TEMPORARILY_UNAVAILABLE",
                "message": "语音对话服务暂时不可用",
            },
        )
    finally:
        # 资源清理：关闭 ASR client、取消 partial task。
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


async def _drain_partials(ws: WebSocket, asr_client: Any) -> None:
    """后台消费真实 ASR client 的 partial_results，推送给前端。"""
    try:
        async for partial_text in asr_client.partial_results():
            await _send_json(
                ws,
                {"type": "partial_transcript", "text": partial_text},
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "voice_ws_partial_drain_error err=%s", type(exc).__name__
        )


async def _drain_mock_partials(
    ws: WebSocket, generator: Any
) -> None:
    """后台消费 mock async generator 的部分结果，推送给前端。

    mock generator 的最终结果（``is_final=True``）由 audio_end 通过
    orchestrator.process_transcript 路径处理；这里只推送 ``is_final=False``
    的部分结果。最终转写文本由 mock 内部状态推导。
    """
    try:
        async for transcript in generator:
            if hasattr(transcript, "is_final") and transcript.is_final:
                # 最终结果：暂存，由 audio_end 路径读取。这里不推送 final。
                _mock_final_store["text"] = transcript.text
                continue
            text = (
                transcript.text
                if hasattr(transcript, "text")
                else str(transcript)
            )
            await _send_json(
                ws, {"type": "partial_transcript", "text": text}
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "voice_ws_mock_drain_error err=%s", type(exc).__name__
        )


# mock 最终文本暂存（单轮对话，非并发安全；mock 联调用）。
_mock_final_store: dict[str, str] = {}


__all__ = ["router"]
