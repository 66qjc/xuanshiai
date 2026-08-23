"""Relationship advisor MVP service."""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import consume_daily, daily_quota_key, refund_daily
from app.schemas.ai_advisor import (
    AdvisorAdviceRequest,
    AdvisorAdviceResponse,
    AdvisorFeedbackRequest,
    AdvisorFeedbackResponse,
    AdvisorSessionPage,
    AdvisorSessionResponse,
    AdvisorSessionCreate,
    AdvisorSuggestion,
)
from app.services.ai_provider import complete, parse_json
from app.services.content_filter import assert_text_allowed

_DISCLAIMER = "以上建议仅供参考，请根据真实感受沟通，并尊重对方边界。"
_HIGH_RISK_TERMS = (
    "\u81ea\u6740", "\u81ea\u4f24", "\u4ed6\u6740", "\u4f24\u5bb3\u5bf9\u65b9", "\u8bc8\u9a97", "\u8f6c\u8d26", "\u94f6\u884c\u5361", "\u9a8c\u8bc1\u7801",
    "\u88f8\u7167", "\u8272\u60c5", "\u672a\u6210\u5e74", "\u5f3a\u5978", "\u8ddf\u8e2a", "\u62a5\u590d", "\u5a01\u80c1", "\u6bd2\u54c1",
)
_ABSOLUTE_TERMS = ("\u4e00\u5b9a\u559c\u6b22\u4f60", "\u4fdd\u8bc1\u590d\u5408", "\u767e\u5206\u4e4b\u767e", "\u80af\u5b9a\u4f1a\u7b54\u5e94", "\u7edd\u5bf9")
_FALLBACK_KNOWLEDGE: dict[str, tuple[str, ...]] = {
    "opening": ("Hello, nice to meet you. I hope we can chat casually.", "Your profile seems interesting, so I wanted to say hello."),
    "reply": ("That sounds interesting. Would you like to tell me more?", "I see. Is that something you do often?"),
    "topic_extension": ("Besides that, what else do you enjoy?", "I would like to try that sometime too."),
    "rescue": ("Let us switch to something light. Do you prefer staying in or going out?", "Has anything small made you happy recently?"),
    "care": ("Remember to eat and leave yourself some time to rest.", "You sound busy lately. Rest early when you finish."),
    "compliment": ("You have your own ideas, and talking with you feels comfortable.", "You take things seriously, which is valuable."),
    "values": ("What kind of compatibility matters most to you in a relationship?", "When two people disagree, do you prefer calming down first or talking immediately?"),
    "intimacy": ("Talking with you feels relaxed, and time passes quickly.", "It is rare to meet someone easy to talk with, and I value that."),
    "closing": ("I enjoyed talking today. Rest early and let us chat again.", "Take care of what you need to do, and we can continue later."),
    "analyze": ("Acknowledge what they said, then observe whether they want to expand.", "If replies stay short, reduce the frequency and respect their space."),
}


async def _require_vip(db: AsyncSession, user_id: int) -> None:
    row = await db.execute(text("""SELECT 1 FROM user_membership
        WHERE user_id=:user_id AND status=1
          AND (start_at IS NULL OR start_at<=UTC_TIMESTAMP())
          AND (end_at IS NULL OR end_at>UTC_TIMESTAMP()) LIMIT 1"""), {"user_id": user_id})
    if not row.scalar():
        raise HTTPException(403, detail="AI功能仅限有效会员使用")


async def _consume_quota(user_id: int) -> str:
    key = daily_quota_key("ai:advisor", user_id)
    if not await consume_daily(key, settings.ai_daily_advisor_limit):
        raise HTTPException(429, detail="今日AI军师使用次数已用完")
    return key


async def _assert_chat_session_access(db: AsyncSession, user_id: int, chat_session_id: int) -> None:
    row = await db.execute(text("""SELECT id FROM chat_session
        WHERE id=:session_id AND (user1_id=:user_id OR user2_id=:user_id)"""), {
        "session_id": chat_session_id, "user_id": user_id,
    })
    if not row.scalar():
        raise HTTPException(403, detail="无权读取该聊天会话")


async def _load_context(db: AsyncSession, user_id: int, chat_session_id: int | None) -> str:
    if chat_session_id is None:
        return ""
    await _assert_chat_session_access(db, user_id, chat_session_id)
    rows = (await db.execute(text("""SELECT from_user_id, content FROM chat_message
        WHERE session_id=:session_id AND type=1 AND revoked_at IS NULL
        ORDER BY created_at DESC, id DESC LIMIT :limit"""), {
        "session_id": chat_session_id,
        "limit": settings.ai_advisor_max_context_messages,
    })).mappings().all()
    return "\n".join(
        f"{'我' if int(row['from_user_id']) == user_id else '对方'}：{str(row['content'])[:1000]}"
        for row in reversed(rows)
    )


async def _load_knowledge(db: AsyncSession, scenario: str, tone: str) -> list[dict[str, str]]:
    try:
        rows = (await db.execute(text("""SELECT content, COALESCE(reason, '') AS reason
            FROM ai_advisor_knowledge
            WHERE advisor_type='relationship' AND scenario=:scenario AND tone=:tone AND enabled=1
            ORDER BY id DESC LIMIT 10"""), {"scenario": scenario, "tone": tone})).mappings().all()
    except Exception:
        rows = []
    knowledge = [{"content": str(row["content"]), "reason": str(row["reason"])} for row in rows]
    if knowledge:
        return knowledge
    return [{"content": item, "reason": "Seed relationship-advice guidance"} for item in _FALLBACK_KNOWLEDGE.get(scenario, _FALLBACK_KNOWLEDGE["reply"])]


def _risk_level(content: str) -> str:
    value = content.casefold()
    if any(term.casefold() in value for term in _HIGH_RISK_TERMS):
        return "high"
    if any(term.casefold() in value for term in _ABSOLUTE_TERMS):
        return "medium"
    return "none"


def _safe_risk_response(level: str) -> tuple[str, str | None]:
    if level == "high":
        return "high", "This is a high-risk situation. Stop pressure or sensitive-data requests and seek professional help."
    if level == "medium":
        return "medium", "Relationship outcomes are uncertain. Treat this as reference and follow the other person actual response."
    return "none", None


def _build_prompt(request: AdvisorAdviceRequest, context: str, knowledge: list[dict[str, str]], risk: str) -> str:
    snippets = "\n".join(f"- {item['content']}（{item['reason']}）" for item in knowledge)
    return f"""ADVISOR_ADVICE
You are a relationship communication advisor clearly identified as AI. Give respectful suggestions only and never send messages.
Do not claim certain attraction or reconciliation. Do not provide medical, legal, financial, or psychological diagnoses.
Scenario: {request.scenario}
Goal: {request.goal or 'natural communication'}
Tone: {request.tone}
Input risk: {risk}
Latest message: {request.incoming_message}
Conversation context: {context or 'history access not authorized'}
Reference guidance:
{snippets}
Return JSON only with analysis, suggestions, risk_level, risk_notice, and next_step. suggestions must contain at most {request.max_suggestions} items with content, style, and reason. Keep replies short and avoid repeated questioning."""


def _normalize_result(data: dict[str, Any], request: AdvisorAdviceRequest) -> dict[str, Any]:
    analysis = str(data.get("analysis") or "Acknowledge the current message, then decide whether to continue based on the response.").strip()[:500]
    raw_suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
    suggestions: list[dict[str, str]] = []
    for item in raw_suggestions[: request.max_suggestions]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()[:500]
        if not content:
            continue
        style = str(item.get("style") or request.tone)
        if style not in {"natural", "warm", "humorous", "mature"}:
            style = request.tone
        reason = str(item.get("reason") or "Naturally continue from the current topic").strip()[:300]
        suggestions.append({"content": content, "style": style, "reason": reason})
    if not suggestions:
        for item in _FALLBACK_KNOWLEDGE.get(request.scenario, _FALLBACK_KNOWLEDGE["reply"])[: request.max_suggestions]:
            suggestions.append({"content": item, "style": request.tone, "reason": "Conservative fallback for the selected scenario"})
    model_level = str(data.get("risk_level") or "none")
    if model_level not in {"none", "low", "medium", "high"}:
        model_level = "none"
    risk_level = _risk_level(" ".join([analysis, *(item["content"] for item in suggestions)]))
    if risk_level == "none":
        risk_level = model_level
    _, risk_notice = _safe_risk_response(risk_level)
    return {
        "analysis": analysis,
        "suggestions": suggestions,
        "risk_level": risk_level,
        "risk_notice": str(data.get("risk_notice") or risk_notice)[:500] if (data.get("risk_notice") or risk_notice) else None,
        "next_step": str(data.get("next_step") or "Adjust the pace based on the reply and avoid repeated questions")[:500],
    }


async def create_session(db: AsyncSession, user_id: int, request: AdvisorSessionCreate) -> AdvisorSessionResponse:
    await _require_vip(db, user_id)
    if request.chat_session_id is not None:
        await _assert_chat_session_access(db, user_id, request.chat_session_id)
    result = await db.execute(text("""INSERT INTO ai_advisor_session
        (user_id, advisor_type, chat_session_id, title)
        VALUES (:user_id, :advisor_type, :chat_session_id, :title)"""), {
        "user_id": user_id,
        "advisor_type": request.advisor_type,
        "chat_session_id": request.chat_session_id,
        "title": request.title or "Relationship advisor",
    })
    await db.commit()
    row = (await db.execute(text("""SELECT id, advisor_type, chat_session_id, title, created_at, updated_at
        FROM ai_advisor_session WHERE id=:id"""), {"id": result.lastrowid})).mappings().one()
    return AdvisorSessionResponse(**dict(row), message_count=0)


async def list_sessions(db: AsyncSession, user_id: int, page: int, page_size: int) -> AdvisorSessionPage:
    await _require_vip(db, user_id)
    total = int((await db.execute(text("""SELECT COUNT(*) FROM ai_advisor_session
        WHERE user_id=:user_id AND status=1"""), {"user_id": user_id})).scalar() or 0)
    rows = (await db.execute(text("""SELECT s.id, s.advisor_type, s.chat_session_id, s.title,
            s.created_at, s.updated_at, COUNT(m.id) AS message_count
        FROM ai_advisor_session s LEFT JOIN ai_advisor_message m ON m.session_id=s.id
        WHERE s.user_id=:user_id AND s.status=1
        GROUP BY s.id ORDER BY s.updated_at DESC, s.id DESC
        LIMIT :limit OFFSET :offset"""), {
        "user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size,
    })).mappings().all()
    return AdvisorSessionPage(
        items=[AdvisorSessionResponse(**dict(row)) for row in rows],
        page=page, page_size=page_size, total=total, has_more=page * page_size < total,
    )


async def delete_session(db: AsyncSession, user_id: int, session_id: int) -> None:
    result = await db.execute(text("""UPDATE ai_advisor_session SET status=0, deleted_at=UTC_TIMESTAMP()
        WHERE id=:session_id AND user_id=:user_id AND status=1"""), {"session_id": session_id, "user_id": user_id})
    if not result.rowcount:
        raise HTTPException(404, detail="AI军师消息不存在")
    await db.execute(text("""UPDATE ai_advisor_message SET status='deleted'
        WHERE session_id=:session_id AND user_id=:user_id"""), {"session_id": session_id, "user_id": user_id})
    await db.commit()


async def get_advice(db: AsyncSession, user_id: int, session_id: int, request: AdvisorAdviceRequest) -> AdvisorAdviceResponse:
    await _require_vip(db, user_id)
    session = (await db.execute(text("""SELECT id, chat_session_id FROM ai_advisor_session
        WHERE id=:session_id AND user_id=:user_id AND status=1"""), {"session_id": session_id, "user_id": user_id})).mappings().first()
    if not session:
        raise HTTPException(404, detail="AI advisor message not found")
    chat_session_id = request.chat_session_id or session.get("chat_session_id")
    if request.include_history and chat_session_id is None:
        raise HTTPException(422, detail="chat_session_id is required when include_history is true")
    await assert_text_allowed(db, request.incoming_message, field="Incoming message")
    input_risk = _risk_level(request.incoming_message)
    if input_risk == "high":
        raise HTTPException(422, detail="该内容涉及高风险情境，暂不生成情感话术建议")
    context = await _load_context(db, user_id, chat_session_id) if request.include_history else ""
    knowledge = await _load_knowledge(db, request.scenario, request.tone)
    quota_key = await _consume_quota(user_id)
    request_id = uuid4().hex
    started = time.monotonic()
    prompt = _build_prompt(request, context, knowledge, input_risk)
    try:
        raw = await complete([
            {"role": "system", "content": "You are a cautious, privacy-respecting relationship advisor clearly identified as AI."},
            {"role": "user", "content": prompt},
        ], json_mode=True)
        data = _normalize_result(parse_json(raw), request)
        if data["risk_level"] == "high":
            raise HTTPException(422, detail="AI建议命中高风险规则，暂不返回")
        result = await db.execute(text("""INSERT INTO ai_advisor_message
            (session_id, user_id, role, scenario, input_text, output_json, risk_level, status,
             model_name, prompt_version, knowledge_version, request_id, latency_ms, quota_consumed)
            VALUES (:session_id, :user_id, 'assistant', :scenario, :input_text, :output_json, :risk_level, 'success',
             :model_name, :prompt_version, :knowledge_version, :request_id, :latency_ms, 1)"""), {
            "session_id": session_id,
            "user_id": user_id,
            "scenario": request.scenario,
            "input_text": request.incoming_message,
            "output_json": json.dumps(data, ensure_ascii=False),
            "risk_level": data["risk_level"],
            "model_name": settings.ai_model,
            "prompt_version": settings.ai_advisor_prompt_version,
            "knowledge_version": settings.ai_advisor_knowledge_version,
            "request_id": request_id,
            "latency_ms": int((time.monotonic() - started) * 1000),
        })
        await db.execute(text("UPDATE ai_advisor_session SET updated_at=UTC_TIMESTAMP() WHERE id=:id"), {"id": session_id})
        await db.commit()
    except HTTPException:
        await refund_daily(quota_key)
        raise
    except Exception as exc:
        await db.rollback()
        await refund_daily(quota_key)
        raise HTTPException(503, detail="AI军师服务暂时不可用") from exc
    row = (await db.execute(text("""SELECT id, created_at FROM ai_advisor_message WHERE id=:id"""), {"id": result.lastrowid})).mappings().one()
    return AdvisorAdviceResponse(
        id=int(row["id"]), session_id=session_id, scenario=request.scenario,
        analysis=data["analysis"], suggestions=[AdvisorSuggestion(**item) for item in data["suggestions"]],
        risk_level=data["risk_level"], risk_notice=data["risk_notice"], next_step=data["next_step"],
        disclaimer=_DISCLAIMER, created_at=row["created_at"],
    )


async def record_feedback(db: AsyncSession, user_id: int, message_id: int, request: AdvisorFeedbackRequest) -> AdvisorFeedbackResponse:
    exists = (await db.execute(text("""SELECT id FROM ai_advisor_message
        WHERE id=:message_id AND user_id=:user_id AND status='success'"""), {"message_id": message_id, "user_id": user_id})).scalar()
    if not exists:
        raise HTTPException(404, detail="AI advisor message not found")
    try:
        await db.execute(text("""INSERT INTO ai_advisor_feedback (message_id, user_id, feedback_type)
            VALUES (:message_id, :user_id, :feedback_type)"""), {
            "message_id": message_id, "user_id": user_id, "feedback_type": request.feedback_type,
        })
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if "Duplicate" in str(exc) or "duplicate" in str(exc):
            return AdvisorFeedbackResponse(message_id=message_id, feedback_type=request.feedback_type, recorded=False)
        raise HTTPException(503, detail="AI军师反馈暂时无法保存") from exc
    return AdvisorFeedbackResponse(message_id=message_id, feedback_type=request.feedback_type, recorded=True)
