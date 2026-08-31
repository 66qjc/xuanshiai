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

    {"type": "session_start", "mode": "profile_build"?, "subject": "personal"?,
     "consentVersion": "profile-text-v1"?}
    {"type": "audio_start"}
    {"type": "audio_chunk", "data": "<base64 PCM>", "seq": 1}
    {"type": "audio_end"}
    {"type": "text_message", "text": "...", "clientTurnId": "..."?}
    {"type": "listen"}
    {"type": "revise_text", "text": "..."}
    {"type": "cancel"}

后端 → 前端::

    {"type": "session_ready"}
    {"type": "progress", "percent": 40.0, "hard_done": 1, "hard_total": 3,
     "entry_score": 1.5, "gate_met": false}      # 仅建构模式（mode=profile_build）
    {"type": "confirm_card", "card_id": "c-...", "draft_id": "d-...",
     "expected_revision": 3, "items": [{"field_key": "...", "kind": "entry",
     "category": "价值观", "content": "...", ...}]}   # 仅建构模式
    {"type": "publish_ready", "summary": "基础信息已齐，可以去成稿了"}  # 门槛达标
    {"type": "partial_transcript", "text": "..."}
    {"type": "final_transcript", "text": "..."}
    {"type": "ai_thinking"}
    {"type": "ai_content", "text": "..."}        # 流式增量
    {"type": "ai_reply", "text": "..."}           # 完整回复
    {"type": "tts_audio", "audio_url": "...", "duration_ms": 3000}
    {"type": "error", "code": "...", "message": "..."}

建构模式：``session_start`` 带 ``mode=profile_build`` 时绑定（或复用）master
会话，用户轮次经 ``submit_profile_turn`` 落库并入队 ``profile_extract`` 任务，
任务终态后推送 progress / confirm_card。不带 ``mode`` 为纯聊，行为与既有
协议一致（不建会话、不推进度）。

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
from app.db.session import session_factory as _db_session_factory
from app.services.ai.flags import AiFeature, require_ai_feature
from app.services.ai.gateway import AIGateway
from app.services.ai.base import ProviderError as AIProviderError
from app.services.ai.profile import (
    AIConsentRequired,
    ProfileSubject,
    _load_active_draft_id_for_session,
    _load_draft_field_rows,
    _load_draft_row,
    create_master_session,
    load_master_progress_snapshot,
    persist_master_assistant_reply,
    submit_profile_turn,
)
from app.services.ai.prompts.moxiang_master import (
    OPENING_MESSAGE,
    _format_narrative_context,
    _missing_label,
    build_build_context,
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


# profile_extract 任务的终态集合（AiTaskStatus 无 ``dead``；取消/被取代同样
# 停止轮询——任务不会再推进，继续轮询只会空转 30s）。
_EXTRACT_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "superseded"}
)


async def _build_context_snapshot(db: Any, user_id: int, session_id: str) -> str:
    """建构模式上下文快照（Task 4）：缺失硬字段 + 已确认摘要 + 进度。

    缺失字段在路由侧先经 ``_missing_label`` 渲染成中文标签再交给
    ``build_build_context``（其内部同源渲染兜底未知 key），确保提示词里
    不出现英文 field_key。
    """
    snap = await load_master_progress_snapshot(db, session_id, user_id)
    missing = [_missing_label(key) for key in snap.missing_hard]
    return build_build_context(missing, snap.confirmed_summary, snap.progress.percent)


async def _push_progress_snapshot(
    ws: WebSocket, user_id: int, session_id: str
) -> None:
    """读会话已确认字段/条目并推 progress；读不到就静默跳过（不阻塞对话）。"""
    if _db_session_factory is None:
        return
    try:
        async with _db_session_factory() as db:
            snap = await load_master_progress_snapshot(db, session_id, user_id)
        await _send_json(
            ws,
            {
                "type": "progress",
                "percent": snap.progress.percent,
                "hard_done": snap.progress.hard_done,
                "hard_total": snap.progress.hard_total,
                "entry_score": snap.progress.entry_score,
                "gate_met": snap.progress.gate_met,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "moxiang_progress_push_failed user_id=%s err=%s",
            user_id,
            type(exc).__name__,
        )


async def _push_confirm_card(
    ws: WebSocket, user_id: int, session_id: str
) -> None:
    """把活动草稿的 suggested 行推成 confirm_card（items 含 field_key/kind/
    category/content + draft_id + expected_revision，前端可直接拿去调 REST
    确认）；确认门槛达标时再推 publish_ready。读不到就静默跳过。"""
    if _db_session_factory is None:
        return
    try:
        async with _db_session_factory() as db:
            draft_id = await _load_active_draft_id_for_session(db, session_id)
            if draft_id is None:
                return
            draft_row = await _load_draft_row(db, draft_id)
            if draft_row is None:
                return
            rows = await _load_draft_field_rows(db, draft_id)
            snap = await load_master_progress_snapshot(db, session_id, user_id)
        items: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("confirmation_status") or "suggested") != "suggested":
                continue
            raw_value = row.get("value_json")
            try:
                value = (
                    json.loads(raw_value)
                    if isinstance(raw_value, str) and raw_value
                    else None
                )
            except json.JSONDecodeError:
                value = None
            items.append(
                {
                    "field_key": str(row["field_key"]),
                    "kind": str(row.get("field_kind") or "structured"),
                    "category": row.get("category"),
                    "content": row.get("content"),
                    "display_value": row.get("display_value"),
                    "value": value,
                }
            )
        if items:
            await _send_json(
                ws,
                {
                    "type": "confirm_card",
                    "card_id": f"c-{uuid.uuid4().hex}",
                    "draft_id": str(draft_id),
                    "expected_revision": int(
                        draft_row.get("expected_revision") or 0
                    ),
                    "items": items,
                },
            )
        if snap.progress.gate_met:
            await _send_json(
                ws,
                {
                    "type": "publish_ready",
                    "summary": "基础信息已齐，可以去成稿了",
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "moxiang_confirm_card_push_failed user_id=%s err=%s",
            user_id,
            type(exc).__name__,
        )


async def _wait_extract_and_push(
    ws: WebSocket, user_id: int, session_id: str, task_id: str
) -> None:
    """轮询 profile_extract 任务终态（≤30s），终态后推 progress/confirm_card。

    断线/新轮次由调用方 cancel，不产生幽灵任务；30s 未终态则本轮不推，
    下一轮对话或重连时补推。
    """
    if _db_session_factory is None:
        return
    for _ in range(60):
        await asyncio.sleep(0.5)
        try:
            async with _db_session_factory() as db:
                row = (
                    await db.execute(
                        sql_text(
                            "SELECT status FROM ai_task "
                            "WHERE task_id = :task_id"
                        ),
                        {"task_id": task_id},
                    )
                ).mappings().first()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "moxiang_extract_poll_failed task_id=%s err=%s",
                task_id,
                type(exc).__name__,
            )
            return
        if row is None:
            return
        if str(row["status"]) in _EXTRACT_TERMINAL_STATUSES:
            break
    else:
        return
    await _push_progress_snapshot(ws, user_id, session_id)
    await _push_confirm_card(ws, user_id, session_id)


async def _submit_build_turn(
    user_id: int, session_id: str, client_turn_id: str, text_content: str
) -> str:
    """建构轮次：原文落库 + 入队 profile_extract；返回 task_id（回放为空串）。

    异常不吞：会话 stale/授权缺失等沿既有 turn 错误路径上抛（外层统一
    error 处理），避免静默吞掉导致状态漂移。
    """
    if _db_session_factory is None:
        return ""
    async with _db_session_factory() as db:
        submission = await submit_profile_turn(
            db,
            session_id,
            user_id,
            client_turn_id,
            text_content,
            idempotency_key=uuid.uuid4().hex,
        )
        await db.commit()
    return str(submission.task_id or "")


async def _finish_build_turn(
    ws: WebSocket,
    orchestrator: MoxiangMasterOrchestrator,
    user_id: int,
    session_id: str,
    task_id: str,
    poll_task: asyncio.Task[None] | None,
) -> asyncio.Task[None] | None:
    """墨相师回复落库（assistant turn）+ 抽取轮询接管；text_message 与
    audio_end 共用。返回新的轮询任务（或传入的原任务）。

    ``persist_master_assistant_reply`` 经 ``load_owned_active_session`` 护栏：
    stale/paused 会话抛异常沿既有错误路径上抛，不静默吞掉导致状态漂移。
    """
    if not session_id:
        return poll_task
    reply_text = orchestrator._last_reply_text  # noqa: SLF001 — listen 分支同款私有读取
    if reply_text and _db_session_factory is not None:
        async with _db_session_factory() as db:
            await persist_master_assistant_reply(
                db, session_id, user_id, reply_text
            )
            await db.commit()
    if not task_id:
        return poll_task
    if poll_task is not None:
        poll_task.cancel()
    return asyncio.create_task(
        _wait_extract_and_push(ws, user_id, session_id, task_id)
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
    # 建构模式连接级状态（设计 Task 7）：build_session_id 绑定 master 会话，
    # 空串=纯聊；poll_task 轮询当前轮抽取任务，新轮次/断线时取消。
    build_session_id = ""
    build_subject = "personal"
    poll_task: asyncio.Task[None] | None = None

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
                build_mode = str(message.get("mode", "")) == "profile_build"
                build_subject = str(message.get("subject") or "personal")
                build_session_id = ""
                if not build_mode:
                    # 纯聊：不建会话、不推进度（行为与既有协议一致）。
                    orchestrator.set_build_context("")
                elif _db_session_factory is not None:
                    # 建构模式：绑定（或复用）master 会话；fail-closed——
                    # 授权缺失 AI_CONSENT_REQUIRED，DB/其他异常
                    # AI_TEMPORARILY_UNAVAILABLE，且不得留下可用会话绑定。
                    try:
                        async with _db_session_factory() as db:
                            session = await create_master_session(
                                db,
                                user_id,
                                ProfileSubject(build_subject),
                                str(
                                    message.get(
                                        "consentVersion", "profile-text-v1"
                                    )
                                ),
                            )
                            await db.commit()
                            build_session_id = session.session_id
                            build_ctx = await _build_context_snapshot(
                                db, user_id, session.session_id
                            )
                        orchestrator.set_build_context(build_ctx)
                    except Exception as exc:  # noqa: BLE001
                        build_session_id = ""
                        orchestrator.set_build_context("")
                        code = (
                            "AI_CONSENT_REQUIRED"
                            if isinstance(exc, AIConsentRequired)
                            else "AI_TEMPORARILY_UNAVAILABLE"
                        )
                        logger.warning(
                            "moxiang_build_session_failed user_id=%s "
                            "request_id=%s err=%s",
                            user_id,
                            request_id,
                            type(exc).__name__,
                        )
                        await _send_error(
                            ws, code, "画像建构通道暂不可用，可稍后重试"
                        )
                await _send_json(ws, {"type": "session_ready"})
                if build_session_id:
                    await _push_progress_snapshot(ws, user_id, build_session_id)
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
                if build_session_id:
                    logger.info(
                        "moxiang_build_session_bound user_id=%s "
                        "request_id=%s session_id=%s",
                        user_id,
                        request_id,
                        build_session_id,
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
                # 建构模式：先落库原文并入队抽取（回复失败也不丢用户输入），
                # 再流式回复；异常沿既有 turn 错误路径上抛，不吞。
                task_id = ""
                if build_session_id and _db_session_factory is not None:
                    task_id = await _submit_build_turn(
                        user_id,
                        build_session_id,
                        str(message.get("clientTurnId") or uuid.uuid4().hex),
                        text_content,
                    )
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    text_content,
                    request_id=request_id,
                )
                poll_task = await _finish_build_turn(
                    ws,
                    orchestrator,
                    user_id,
                    build_session_id,
                    task_id,
                    poll_task,
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
                # 建构模式：语音轮次与文字轮次同一链路（clientTurnId 由服务端
                # 生成；source 语义由 submit 内部落库承担）。
                task_id = ""
                if (
                    build_session_id
                    and _db_session_factory is not None
                    and final_transcript.strip()
                ):
                    task_id = await _submit_build_turn(
                        user_id,
                        build_session_id,
                        uuid.uuid4().hex,
                        final_transcript,
                    )
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    final_transcript,
                    request_id=request_id,
                )
                poll_task = await _finish_build_turn(
                    ws,
                    orchestrator,
                    user_id,
                    build_session_id,
                    task_id,
                    poll_task,
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
        if poll_task is not None:
            poll_task.cancel()
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
