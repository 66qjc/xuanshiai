"""墨相师·AI 引路人 对话 WebSocket 路由。

路径：``/api/v1/voice/moxiang-master``。

与 ``/voice/conversation`` 的区别：
- 使用墨相师人设提示词（非 voice_reply 的 ≤30 字资料采集）
- 维护多轮对话历史（持久化轮次在 WS 建立时恢复，连接内继续累积）
- 最终文本进入异步候选抽取，候选完成后实时推进六维理解进度
- 新增 ``text_message`` 文字通道（文字模式不经 ASR）
- 流式推送 ``ai_content`` 增量

消息协议（前后端共享契约）：

前端 → 后端::

    {"type": "session_start", "mode": "moxiang_journey",
     "subject": "personal"?,
     "consentVersion": "profile-text-v1"?}
    {"type": "subject_switch", "subject": "ideal_partner"}
    {"type": "audio_start"}
    {"type": "audio_chunk", "data": "<base64 PCM>", "seq": 1}
    {"type": "audio_end"}
    {"type": "text_message", "text": "...", "clientTurnId": "..."?}
    {"type": "listen"}
    {"type": "revise_text", "text": "..."}
    {"type": "cancel"}

后端 → 前端::

    {"type": "journey_ready", "session_id": "...", "subject": "personal",
     "journey_stage": "chatting", "resumed": true}
    {"type": "extraction_status", "subject": "personal", "task_id": "...",
     "status": "queued|processing|completed|failed"}
    {"type": "journey_progress", "subject": "personal", "overall_percent": 40.0,
     "dimensions": {"lifestyle": {"percent": 50.0, "evidence_count": 1}}}
    {"type": "confirm_card", "subject": "personal", "card_id": "c-...",
     "draft_id": "d-...",
     "expected_revision": 3, "items": [{"field_key": "...", "kind": "entry",
     "category": "价值观", "content": "...", ...}]}   # 仅建构模式
    {"type": "publish_ready", "subject": "personal",
     "summary": "你的个人画像已经可以成稿了"}  # 门槛达标
    {"type": "partial_transcript", "text": "..."}
    {"type": "final_transcript", "text": "..."}
    {"type": "ai_thinking"}
    {"type": "ai_content", "text": "..."}        # 流式增量
    {"type": "ai_reply", "text": "...", "opening": false}  # 完整回复；开场时 opening=true
    {"type": "tts_audio", "audio_url": "...", "duration_ms": 3000}
    {"type": "error", "code": "...", "message": "..."}

旅程模式：``session_start`` 必须带 ``mode=moxiang_journey``，绑定（或复用）
master 会话。每一轮最终文本先持久化再入队独立候选任务；聊天回复不等待任务，
任务完成后只推送安全的状态和六维进度，不向消息流泄露候选内容。

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
    AIInputError,
    ProfileSubject,
    create_master_session,
    persist_master_assistant_reply,
)
from app.services.ai.journey import (
    compose_journey_build_context,
    list_session_candidates,
    maybe_create_build_invite,
    resolve_journey_invite,
    submit_journey_turn,
)
from app.services.ai.journey_progress import calculate_journey_progress
from app.services.ai.prompts.moxiang_master import (
    AI_ROLE_NAME,
    _format_narrative_context,
    opening_message_for_subject,
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

_MOXIANG_JOURNEY_MODE = "moxiang_journey"


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


def _check_journey_feature() -> str | None:
    """The real-time journey is opt-in and inherits the profile safety gate."""
    if not bool(getattr(settings, "ai_moxiang_journey_enabled", False)):
        return "AI_FEATURE_DISABLED"
    try:
        require_ai_feature(AiFeature.PROFILE, settings)
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


async def _journey_build_context(session_id: str, subject: str) -> str | None:
    """Project the session's candidates into 知遇's build-mode context.

    Returns None when there is no journey session or the read fails, so the
    caller leaves the orchestrator in pure-chat mode rather than crashing the
    reply path on a context-refresh hiccup.
    """
    if not session_id or _db_session_factory is None:
        return None
    try:
        async with _db_session_factory() as db:
            return await compose_journey_build_context(
                db, session_id=session_id, subject=subject
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "moxiang_build_context_failed session_id=%s err=%s",
            session_id,
            type(exc).__name__,
        )
        return None


async def _push_streamed_reply(
    ws: WebSocket,
    orchestrator: MoxiangMasterOrchestrator,
    user_text: str,
    *,
    request_id: str,
    build_context: str | None = None,
) -> None:
    """流式推送墨相师回复：ai_thinking → ai_content* → ai_reply。

    ``build_context`` 非 None 时刷新建构模式上下文（缺失硬字段/进度/已确认摘要），
    让知遇围绕尚未了解的部分提问；None 表示保持当前模式不变（纯聊不注入）。
    """
    if build_context is not None:
        orchestrator.set_build_context(build_context)
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
            ws, "AI_TEMPORARILY_UNAVAILABLE", f"{AI_ROLE_NAME}暂时无法回复"
        )
        return
    if full_reply:
        await _send_json(
            ws, {"type": "ai_reply", "text": full_reply, "opening": False}
        )


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


# 候选任务终态。每个任务均独立观察，后续消息绝不取消前一轮的结果推送。
_CANDIDATE_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "superseded"}
)


async def _load_master_history(
    db: Any, session_id: str, *, limit: int = 24
) -> list[dict[str, str]]:
    """Load the recent persisted master dialogue in chronological order."""
    result = await db.execute(
        sql_text(
            "SELECT role, answer_text FROM ai_profile_turn "
            "WHERE session_id = :session_id AND status = 'saved' "
            "ORDER BY turn_no DESC LIMIT :limit"
        ),
        {"session_id": session_id, "limit": limit},
    )
    rows = list(result.mappings().all())
    rows.reverse()
    return [
        {
            "role": str(row.get("role") or ""),
            "content": str(row.get("answer_text") or ""),
        }
        for row in rows
        if str(row.get("role") or "") in {"user", "assistant"}
        and str(row.get("answer_text") or "").strip()
    ]


async def _push_journey_progress(
    ws: WebSocket, session_id: str, subject: str
) -> None:
    """Project persisted candidates into the safe six-dimension wire payload."""
    if _db_session_factory is None:
        return
    try:
        async with _db_session_factory() as db:
            candidates = await list_session_candidates(
                db, session_id=session_id, active_only=False
            )
        snapshot = calculate_journey_progress(candidates)
        await _send_json(
            ws,
            {
                "type": "journey_progress",
                "subject": subject,
                "overall_percent": snapshot.overall_percent,
                "dimensions": {
                    key: {
                        "percent": item.percent,
                        "evidence_count": item.evidence_count,
                    }
                    for key, item in snapshot.dimensions.items()
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "moxiang_journey_progress_push_failed session_id=%s err=%s",
            session_id,
            type(exc).__name__,
        )


async def _maybe_push_build_invite(
    ws: WebSocket, *, user_id: int, session_id: str, subject: str
) -> None:
    """Create/replay the one pending invite only after durable task success."""
    if _db_session_factory is None:
        return
    try:
        async with _db_session_factory() as db:
            invite = await maybe_create_build_invite(
                db, session_id=session_id, user_id=user_id, subject=subject
            )
            await db.commit()
        if invite is None:
            return
        await _send_json(
            ws,
            {
                "type": "build_invite",
                "subject": invite.subject,
                "invite_id": invite.invite_id,
                "summary_items": list(invite.summary_items),
                "effective_turn_count": invite.effective_turn_count,
                "dimension_count": invite.dimension_count,
                "candidate_count": invite.candidate_count,
                "journey_stage": "building",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "moxiang_invite_create_failed session_id=%s err=%s",
            session_id,
            type(exc).__name__,
        )


async def _push_confirm_card_for_draft(
    ws: WebSocket, *, subject: str, draft_id: str
) -> None:
    """Reuse the existing confirmation-card wire shape after invite acceptance."""
    if _db_session_factory is None:
        return
    try:
        async with _db_session_factory() as db:
            draft_row = (
                await db.execute(
                    sql_text(
                        "SELECT expected_revision FROM ai_profile_draft "
                        "WHERE draft_id = :draft_id"
                    ),
                    {"draft_id": draft_id},
                )
            ).mappings().first()
            rows = (
                await db.execute(
                    sql_text(
                        "SELECT field_key, field_kind, category, content, display_value "
                        "FROM ai_profile_draft_field WHERE draft_id = :draft_id "
                        "AND confirmation_status = 'suggested' ORDER BY id ASC"
                    ),
                    {"draft_id": draft_id},
                )
            ).mappings().all()
        if draft_row is None or not rows:
            return
        await _send_json(
            ws,
            {
                "type": "confirm_card",
                "subject": subject,
                "card_id": f"c-{uuid.uuid4().hex}",
                "draft_id": draft_id,
                "expected_revision": int(draft_row.get("expected_revision") or 0),
                "items": [
                    {
                        "field_key": str(row["field_key"]),
                        "kind": str(row.get("field_kind") or "structured"),
                        "category": row.get("category"),
                        "content": row.get("content"),
                        "display_value": row.get("display_value"),
                    }
                    for row in rows
                ],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "moxiang_confirm_card_push_failed draft_id=%s err=%s",
            draft_id,
            type(exc).__name__,
        )


async def _replay_pending_confirm_card(
    ws: WebSocket, *, session_id: str, subject: str
) -> None:
    """WS 重连/切换主体时重放未确认完的确认卡。

    确认卡此前只在接受邀请那一刻推送一次；用户中途退出再进，卡片即丢失，
    草稿里的 suggested 字段无人可确认，旅程卡死在 building。这里复用
    ``_push_confirm_card_for_draft``：无未完成草稿或草稿已全部确认时自动跳过。
    """
    if _db_session_factory is None or not session_id:
        return
    try:
        async with _db_session_factory() as db:
            row = (
                await db.execute(
                    sql_text(
                        "SELECT draft_id FROM ai_profile_draft "
                        "WHERE session_id = :session_id AND status = 'draft' "
                        "ORDER BY updated_at DESC LIMIT 1"
                    ),
                    {"session_id": session_id},
                )
            ).mappings().first()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "moxiang_confirm_card_replay_lookup_failed session_id=%s err=%s",
            session_id,
            type(exc).__name__,
        )
        return
    if row is None:
        return
    await _push_confirm_card_for_draft(
        ws, subject=subject, draft_id=str(row["draft_id"])
    )


async def _wait_candidate_and_push(
    ws: WebSocket, user_id: int, session_id: str, subject: str, task_id: str
) -> None:
    """Watch one durable candidate task without cancelling other turns."""
    if _db_session_factory is None:
        return
    await _send_json(
        ws,
        {
            "type": "extraction_status",
            "subject": subject,
            "task_id": task_id,
            "status": "processing",
        },
    )
    # 快慢两段轮询：快段 0.5s×30s 保实时性；慢段 2s×6min 兜住供应商重试
    # （实测 dots 单次抽取可到 6 分钟）。窗口内任务未到终态则静默放弃，
    # 前端保留"正在理解"占位，进度以重连后的 journey_ready 快照兜底。
    for poll_delay in (0.5,) * 60 + (2.0,) * 180:
        await asyncio.sleep(poll_delay)
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
                "moxiang_candidate_poll_failed task_id=%s err=%s",
                task_id,
                type(exc).__name__,
            )
            return
        if row is None:
            return
        if str(row["status"]) in _CANDIDATE_TERMINAL_STATUSES:
            break
    else:
        return
    terminal_status = str(row["status"])
    if terminal_status != "succeeded":
        await _send_json(
            ws,
            {
                "type": "extraction_status",
                "subject": subject,
                "task_id": task_id,
                "status": "failed",
            },
        )
        return
    await _send_json(
        ws,
        {
            "type": "extraction_status",
            "subject": subject,
            "task_id": task_id,
            "status": "completed",
        },
    )
    await _push_journey_progress(ws, session_id, subject)
    await _maybe_push_build_invite(
        ws, user_id=user_id, session_id=session_id, subject=subject
    )


async def _submit_journey_candidate_turn(
    user_id: int, session_id: str, client_turn_id: str, text_content: str
) -> str:
    """Persist one final transcript and enqueue its dedicated candidate task."""
    if _db_session_factory is None:
        return ""
    async with _db_session_factory() as db:
        submission = await submit_journey_turn(
            db,
            session_id=session_id,
            owner_user_id=user_id,
            client_turn_id=client_turn_id,
            answer_text=text_content,
        )
        await db.commit()
    return str(submission.task_id or "")


async def _finish_journey_turn(
    ws: WebSocket,
    orchestrator: MoxiangMasterOrchestrator,
    user_id: int,
    session_id: str,
    subject: str,
    task_id: str,
    poll_tasks: set[asyncio.Task[None]],
) -> None:
    """Persist assistant reply, then independently watch the candidate task."""
    if not session_id:
        return
    reply_text = orchestrator._last_reply_text  # noqa: SLF001 — listen 分支同款私有读取
    if reply_text and _db_session_factory is not None:
        async with _db_session_factory() as db:
            await persist_master_assistant_reply(
                db, session_id, user_id, reply_text
            )
            await db.commit()
    if not task_id:
        return
    watch_task = asyncio.create_task(
        _wait_candidate_and_push(ws, user_id, session_id, subject, task_id)
    )
    poll_tasks.add(watch_task)
    watch_task.add_done_callback(poll_tasks.discard)


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
    # 每个主体绑定独立的自然对话会话；回复上下文仍属于当前连接。
    sessions_by_subject: dict[str, str] = {}
    active_subject = ProfileSubject.PERSONAL.value
    poll_tasks: set[asyncio.Task[None]] = set()
    journey_active = False
    journey_consent_version = "profile-text-v1"

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
                requested_mode = str(message.get("mode", ""))
                if requested_mode != _MOXIANG_JOURNEY_MODE:
                    await _send_error(ws, "AI_INPUT_INVALID", "请使用最新墨相师旅程")
                    continue
                journey_error = _check_journey_feature()
                if journey_error is not None:
                    await _send_error(ws, journey_error, "墨相师连续旅程当前未启用")
                    continue
                requested_subject = str(message.get("subject") or "personal")
                if requested_subject not in {
                    ProfileSubject.PERSONAL.value,
                    ProfileSubject.IDEAL_PARTNER.value,
                }:
                    await _send_error(ws, "AI_INPUT_INVALID", "画像主体非法")
                    continue
                if _db_session_factory is None:
                    await _send_error(ws, "AI_TEMPORARILY_UNAVAILABLE", "旅程暂不可用")
                    continue
                narrative_ctx = await _load_narrative_context(user_id)
                orchestrator.set_narrative_context(narrative_ctx)
                consent_version = str(message.get("consentVersion", "profile-text-v1"))
                try:
                    async with _db_session_factory() as db:
                        session = await create_master_session(
                            db, user_id, ProfileSubject(requested_subject), consent_version
                        )
                        stage_row = (
                            await db.execute(
                                sql_text(
                                    "SELECT journey_stage FROM ai_profile_session "
                                    "WHERE session_id = :session_id"
                                ),
                                {"session_id": session.session_id},
                            )
                        ).mappings().first()
                        await db.commit()
                        history = await _load_master_history(db, session.session_id)
                except Exception as exc:  # noqa: BLE001
                    code = "AI_CONSENT_REQUIRED" if isinstance(exc, AIConsentRequired) else "AI_TEMPORARILY_UNAVAILABLE"
                    await _send_error(ws, code, "墨相师旅程暂不可用，可稍后重试")
                    continue
                sessions_by_subject[requested_subject] = session.session_id
                active_subject = requested_subject
                journey_active = True
                journey_consent_version = consent_version
                orchestrator.hydrate_history(history)
                await _send_json(
                    ws,
                    {
                        "type": "journey_ready",
                        "session_id": session.session_id,
                        "subject": requested_subject,
                        "journey_stage": str(
                            (stage_row or {}).get("journey_stage") or "chatting"
                        ),
                        "resumed": not session.created,
                    },
                )
                await _push_journey_progress(ws, session.session_id, requested_subject)
                if session.created:
                    await _send_json(
                        ws,
                        {
                            "type": "ai_reply",
                            "opening": True,
                            "text": opening_message_for_subject(requested_subject),
                        },
                    )
                else:
                    # 重连恢复：把上次未确认完的卡片重新推回消息流。
                    await _replay_pending_confirm_card(
                        ws, session_id=session.session_id, subject=requested_subject
                    )

            elif msg_type == "subject_switch":
                requested_subject = str(message.get("subject") or "")
                if requested_subject not in {
                    ProfileSubject.PERSONAL.value,
                    ProfileSubject.IDEAL_PARTNER.value,
                }:
                    await _send_error(ws, "AI_INPUT_INVALID", "画像主体非法")
                    continue
                if not journey_active or _db_session_factory is None:
                    await _send_error(
                        ws, "AI_INPUT_INVALID", "当前不是墨相师旅程"
                    )
                    continue
                try:
                    async with _db_session_factory() as db:
                        session = await create_master_session(
                            db,
                            user_id,
                            ProfileSubject(requested_subject),
                            journey_consent_version,
                        )
                        stage_row = (
                            await db.execute(
                                sql_text(
                                    "SELECT journey_stage FROM ai_profile_session "
                                    "WHERE session_id = :session_id"
                                ),
                                {"session_id": session.session_id},
                            )
                        ).mappings().first()
                        await db.commit()
                        session_new = session.created
                        history = await _load_master_history(
                            db, session.session_id
                        )
                    # 只有目标会话和上下文均加载成功后才切换，失败时保持原主体。
                    sessions_by_subject[requested_subject] = session.session_id
                    active_subject = requested_subject
                    orchestrator.hydrate_history(history)
                except Exception as exc:  # noqa: BLE001
                    code = (
                        "AI_CONSENT_REQUIRED"
                        if isinstance(exc, AIConsentRequired)
                        else "AI_TEMPORARILY_UNAVAILABLE"
                    )
                    await _send_error(
                        ws, code, "画像阶段切换失败，当前阶段保持不变"
                    )
                    continue
                await _send_json(
                    ws,
                    {
                        "type": "journey_ready",
                        "subject": requested_subject,
                        "session_id": session.session_id,
                        "resumed": not session_new,
                        "journey_stage": str(
                            (stage_row or {}).get("journey_stage") or "chatting"
                        ),
                    },
                )
                await _push_journey_progress(ws, session.session_id, requested_subject)
                if session_new:
                    await _send_json(
                        ws,
                        {
                            "type": "ai_reply",
                            "opening": True,
                            "text": opening_message_for_subject(requested_subject),
                        },
                    )
                else:
                    await _replay_pending_confirm_card(
                        ws, session_id=session.session_id, subject=requested_subject
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
                turn_subject = active_subject
                turn_session_id = (
                    sessions_by_subject.get(turn_subject, "")
                    if journey_active
                    else ""
                )
                task_id = ""
                if turn_session_id and _db_session_factory is not None:
                    task_id = await _submit_journey_candidate_turn(
                        user_id,
                        turn_session_id,
                        str(message.get("clientTurnId") or uuid.uuid4().hex),
                        text_content,
                    )
                    await _send_json(
                        ws,
                        {
                            "type": "extraction_status",
                            "subject": turn_subject,
                            "task_id": task_id,
                            "status": "queued",
                        },
                    )
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    text_content,
                    request_id=request_id,
                    build_context=await _journey_build_context(
                        turn_session_id, turn_subject
                    ),
                )
                await _finish_journey_turn(
                    ws,
                    orchestrator,
                    user_id,
                    turn_session_id,
                    turn_subject,
                    task_id,
                    poll_tasks,
                )

            elif msg_type in {"build_invite_accept", "build_invite_snooze"}:
                if not journey_active or _db_session_factory is None:
                    await _send_error(ws, "AI_INPUT_INVALID", "当前没有可处理的整理邀请")
                    continue
                requested_subject = str(message.get("subject") or "")
                invite_id = str(message.get("invite_id") or "")
                if requested_subject not in {
                    ProfileSubject.PERSONAL.value,
                    ProfileSubject.IDEAL_PARTNER.value,
                } or not invite_id:
                    await _send_error(ws, "AI_INPUT_INVALID", "邀请参数非法")
                    continue
                resolution = "accepted" if msg_type == "build_invite_accept" else "snoozed"
                try:
                    async with _db_session_factory() as db:
                        invite, draft_id = await resolve_journey_invite(
                            db,
                            invite_id=invite_id,
                            user_id=user_id,
                            resolution=resolution,
                        )
                        if invite.subject != requested_subject:
                            raise ValueError("邀请主体不匹配")
                        await db.commit()
                except (AIInputError, ValueError) as exc:
                    await _send_error(ws, "AI_INPUT_INVALID", str(exc))
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "moxiang_invite_resolve_failed invite_id=%s err=%s",
                        invite_id,
                        type(exc).__name__,
                    )
                    await _send_error(ws, "AI_TEMPORARILY_UNAVAILABLE", "整理邀请暂时无法处理")
                    continue
                await _send_json(
                    ws,
                    {
                        "type": "build_invite_resolved",
                        "subject": invite.subject,
                        "invite_id": invite.invite_id,
                        "resolution": resolution,
                        "journey_stage": "building" if resolution == "accepted" else "chatting",
                    },
                )
                if draft_id is not None:
                    await _push_confirm_card_for_draft(
                        ws, subject=invite.subject, draft_id=draft_id
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
                # 只有最终转写进入候选任务；partial_transcript 从不落库。
                turn_subject = active_subject
                turn_session_id = (
                    sessions_by_subject.get(turn_subject, "")
                    if journey_active
                    else ""
                )
                task_id = ""
                if (
                    turn_session_id
                    and _db_session_factory is not None
                    and final_transcript.strip()
                ):
                    task_id = await _submit_journey_candidate_turn(
                        user_id,
                        turn_session_id,
                        uuid.uuid4().hex,
                        final_transcript,
                    )
                    await _send_json(
                        ws,
                        {
                            "type": "extraction_status",
                            "subject": turn_subject,
                            "task_id": task_id,
                            "status": "queued",
                        },
                    )
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    final_transcript,
                    request_id=request_id,
                    build_context=await _journey_build_context(
                        turn_session_id, turn_subject
                    ),
                )
                await _finish_journey_turn(
                    ws,
                    orchestrator,
                    user_id,
                    turn_session_id,
                    turn_subject,
                    task_id,
                    poll_tasks,
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
                turn_subject = active_subject
                turn_session_id = (
                    sessions_by_subject.get(turn_subject, "")
                    if journey_active
                    else ""
                )
                task_id = ""
                if turn_session_id and _db_session_factory is not None:
                    task_id = await _submit_journey_candidate_turn(
                        user_id,
                        turn_session_id,
                        str(message.get("clientTurnId") or uuid.uuid4().hex),
                        rev_text,
                    )
                    await _send_json(
                        ws,
                        {
                            "type": "extraction_status",
                            "subject": turn_subject,
                            "task_id": task_id,
                            "status": "queued",
                        },
                    )
                await _push_streamed_reply(
                    ws,
                    orchestrator,
                    rev_text,
                    request_id=request_id,
                    build_context=await _journey_build_context(
                        turn_session_id, turn_subject
                    ),
                )
                await _finish_journey_turn(
                    ws,
                    orchestrator,
                    user_id,
                    turn_session_id,
                    turn_subject,
                    task_id,
                    poll_tasks,
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
                "message": f"{AI_ROLE_NAME}服务暂时不可用",
            },
        )
    finally:
        if partial_task is not None:
            partial_task.cancel()
        for poll_task in tuple(poll_tasks):
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
