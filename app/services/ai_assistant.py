"""Phase-one AI features built on the existing profile, chat and discovery data."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import consume_daily, daily_quota_key
from app.schemas.ai import (
    AIAssistantMessageResponse,
    AIAssistantSessionPage,
    AIAssistantSessionResponse,
    AIMatchItem,
    AIMatchPage,
    AIProfilePolishRequest,
    AIProfilePolishResponse,
    AISearchRequest,
    AISearchResponse,
)
from app.schemas.discovery import DiscoveryFilters
from app.services.ai_provider import complete, parse_json
from app.services.discovery import _candidate_score, _card, _fetch_rows, _viewer_context

MatchType = Literal["who_likes_me", "i_like", "material", "soul"]


async def _require_vip(db: AsyncSession, user_id: int) -> None:
    row = await db.execute(text("""SELECT 1 FROM user_membership
        WHERE user_id=:user_id AND status=1
          AND (start_at IS NULL OR start_at<=UTC_TIMESTAMP())
          AND (end_at IS NULL OR end_at>UTC_TIMESTAMP()) LIMIT 1"""), {"user_id": user_id})
    if not row.scalar():
        raise HTTPException(403, detail="AI功能仅限会员使用")


async def _consume_ai_quota(db: AsyncSession, user_id: int, code: str, limit: int) -> None:
    if not await consume_daily(daily_quota_key(f"ai:{code}", user_id), limit):
        raise HTTPException(429, detail="今日 AI 使用次数已用完")


async def create_assistant_session(db: AsyncSession, user_id: int, title: str | None) -> AIAssistantSessionResponse:
    await _require_vip(db, user_id)
    result = await db.execute(text("""INSERT INTO ai_assistant_session (user_id,title)
        VALUES (:user_id,:title)"""), {"user_id": user_id, "title": title or "AI助手会话"})
    await db.commit()
    row = (await db.execute(text("SELECT id,title,created_at,updated_at FROM ai_assistant_session WHERE id=:id"), {"id": result.lastrowid})).mappings().one()
    return AIAssistantSessionResponse(id=int(row["id"]), title=row["title"], message_count=0, created_at=row["created_at"], updated_at=row["updated_at"])


async def list_assistant_sessions(db: AsyncSession, user_id: int, page: int, page_size: int) -> AIAssistantSessionPage:
    await _require_vip(db, user_id)
    total = int((await db.execute(text("SELECT COUNT(*) FROM ai_assistant_session WHERE user_id=:user_id AND status=1"), {"user_id": user_id})).scalar() or 0)
    rows = (await db.execute(text("""SELECT s.id,s.title,s.created_at,s.updated_at,COUNT(m.id) message_count
        FROM ai_assistant_session s LEFT JOIN ai_assistant_message m ON m.session_id=s.id
        WHERE s.user_id=:user_id AND s.status=1 GROUP BY s.id ORDER BY s.updated_at DESC,s.id DESC
        LIMIT :limit OFFSET :offset"""), {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size})).mappings().all()
    return AIAssistantSessionPage(items=[AIAssistantSessionResponse(id=int(r["id"]), title=r["title"], message_count=int(r["message_count"]), created_at=r["created_at"], updated_at=r["updated_at"]) for r in rows], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def assistant_message(db: AsyncSession, user_id: int, session_id: int, content: str) -> AIAssistantMessageResponse:
    await _require_vip(db, user_id)
    session = (await db.execute(text("SELECT id FROM ai_assistant_session WHERE id=:id AND user_id=:user_id AND status=1"), {"id": session_id, "user_id": user_id})).scalar()
    if not session:
        raise HTTPException(404, detail="AI助手会话不存在")
    await _consume_ai_quota(db, user_id, "assistant", settings.ai_daily_assistant_limit)
    # The user explicitly chose to allow the assistant to inspect all of their
    # own chat records. Only the user's two-party messages are included.
    rows = (await db.execute(text("""SELECT from_user_id,content,created_at FROM chat_message
        WHERE (from_user_id=:user_id OR to_user_id=:user_id) AND type=1 AND revoked_at IS NULL
        ORDER BY created_at DESC LIMIT :limit"""), {"user_id": user_id, "limit": settings.ai_max_context_messages})).mappings().all()
    context = "\n".join(f"{'我' if int(r['from_user_id']) == user_id else '对方'}：{r['content']}" for r in reversed(rows))
    await db.execute(text("INSERT INTO ai_assistant_message (session_id,role,content) VALUES (:sid,'user',:content)"), {"sid": session_id, "content": content})
    prompt = f"你是婚恋沟通助手，只提供沟通建议，不做医疗、法律或高风险决定。\n聊天记录：\n{context}\n用户问题：{content}"
    answer = await complete([{"role": "system", "content": "你是谨慎、尊重隐私的婚恋沟通助手。"}, {"role": "user", "content": prompt}])
    result = await db.execute(text("INSERT INTO ai_assistant_message (session_id,role,content) VALUES (:sid,'assistant',:content)"), {"sid": session_id, "content": answer})
    await db.execute(text("UPDATE ai_assistant_session SET updated_at=UTC_TIMESTAMP() WHERE id=:id"), {"id": session_id})
    await db.commit()
    row = (await db.execute(text("SELECT id,session_id,role,content,created_at FROM ai_assistant_message WHERE id=:id"), {"id": result.lastrowid})).mappings().one()
    return AIAssistantMessageResponse(id=int(row["id"]), session_id=int(row["session_id"]), role="assistant", content=row["content"], created_at=row["created_at"])


async def polish_profile(db: AsyncSession, user_id: int, request: AIProfilePolishRequest) -> AIProfilePolishResponse:
    await _require_vip(db, user_id)
    await _consume_ai_quota(db, user_id, "polish", settings.ai_daily_polish_limit)
    content = await complete([{"role": "system", "content": "你只润色用户提供的原文，不添加未提供的事实。输出JSON：polished(string), changed_points(array[string])。"}, {"role": "user", "content": f"PROFILE_POLISH style={request.style} max_length={request.max_length}\n{request.content}"}], json_mode=True)
    data = parse_json(content)
    polished = str(data.get("polished") or request.content).strip()[:request.max_length]
    points = data.get("changed_points") if isinstance(data.get("changed_points"), list) else []
    return AIProfilePolishResponse(original=request.content, polished=polished, style=request.style, changed_points=[str(x) for x in points[:5]])


async def parse_search(db: AsyncSession, user_id: int, request: AISearchRequest) -> AISearchResponse:
    await _require_vip(db, user_id)
    await _consume_ai_quota(db, user_id, "search", settings.ai_daily_search_limit)
    raw = await complete([{"role": "system", "content": "把自然语言婚恋搜索转换为JSON。只允许输出 filters、normalized_query、unresolved。filters只能包含 gender,age_min,age_max,city_code,marriage_status,education_min,height_min,height_max,income_min,income_max,tag。不要编造城市编码。"}, {"role": "user", "content": f"SEARCH_PARSE\n{request.query}"}], json_mode=True)
    data = parse_json(raw)
    filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
    allowed = set(DiscoveryFilters.model_fields) | {"tag"}
    filters = {k: v for k, v in filters.items() if k in allowed and v is not None}
    try:
        parsed = DiscoveryFilters(page=request.page, page_size=request.page_size, **{k: v for k, v in filters.items() if k != "tag"})
    except Exception:
        parsed = DiscoveryFilters(page=request.page, page_size=request.page_size)
        filters = {}
    from app.services.discovery import _fetch_rows
    rows = await _fetch_rows(db, user_id, parsed, plaza=True, tag=str(filters["tag"]) if filters.get("tag") else None, respect_preferences=False)
    viewer = await _viewer_context(db, user_id)
    scored = sorted([(_candidate_score(viewer, row), row) for row in rows], key=lambda x: x[0][0], reverse=True)
    start = (request.page - 1) * request.page_size
    selected = scored[start:start + request.page_size]
    viewer_vip = True
    from app.schemas.discovery import DiscoveryPage
    result_page = DiscoveryPage(items=[_card(row, score, reason, detail_locked=bool(row.get("only_vip_can_see_detail")) and not viewer_vip) for (score, reason), row in selected], page=request.page, page_size=request.page_size, total=len(scored), has_more=start + request.page_size < len(scored))
    return AISearchResponse(query=request.query, normalized_query=str(data.get("normalized_query") or request.query), filters=filters, unresolved=[str(x) for x in data.get("unresolved", []) if isinstance(x, (str, int))], results=result_page.model_dump())


def _breakdown(viewer: dict[str, Any], row: dict[str, Any], match_type: MatchType) -> dict[str, float]:
    score, _ = _candidate_score(viewer, row)
    tags = set(json.loads(viewer.get("interest_tags") or "[]") if isinstance(viewer.get("interest_tags"), str) else viewer.get("interest_tags") or [])
    candidate_tags = set(json.loads(row.get("interest_tags") or "[]") if isinstance(row.get("interest_tags"), str) else row.get("interest_tags") or [])
    overlap = min(100.0, len(tags & candidate_tags) * 20.0)
    if match_type == "material":
        return {"age": min(100.0, score), "location": 100.0 if viewer.get("residence_city_code") == row.get("residence_city_code") else 0.0, "preference": min(100.0, score * 0.7)}
    if match_type == "soul":
        return {"interest": overlap, "mbti": 70.0 if viewer.get("mbti") and row.get("mbti") else 0.0, "activity": 80.0 if row.get("last_active_at") else 0.0}
    return {"preference": score, "interest": overlap, "activity": 80.0 if row.get("last_active_at") else 0.0}


async def match_page(db: AsyncSession, user_id: int, match_type: MatchType, page: int, page_size: int) -> AIMatchPage:
    await _require_vip(db, user_id)
    await _consume_ai_quota(db, user_id, "match", settings.ai_daily_match_limit)
    viewer = await _viewer_context(db, user_id)
    rows = await _fetch_rows(db, user_id, DiscoveryFilters(page=1, page_size=20), plaza=True, respect_preferences=False)
    scored: list[tuple[float, dict[str, Any], dict[str, float]]] = []
    for row in rows:
        breakdown = _breakdown(viewer, row, match_type)
        score = round(sum(breakdown.values()) / max(1, len(breakdown)), 2)
        scored.append((score, row, breakdown))
    scored.sort(key=lambda x: x[0], reverse=True)
    start = (page - 1) * page_size
    selected = scored[start:start + page_size]
    items: list[AIMatchItem] = []
    for score, row, breakdown in selected:
        explanation = await complete([{"role": "system", "content": "根据给定分项生成简短、客观的JSON，不夸大成功概率。输出 reason(string), suggestions(array[string])。"}, {"role": "user", "content": f"MATCH_EXPLAIN type={match_type} score={score} breakdown={json.dumps(breakdown, ensure_ascii=False)}"}], json_mode=True)
        data = parse_json(explanation)
        items.append(AIMatchItem(user_id=int(row["user_id"]), nickname=row.get("nickname"), avatar=row.get("avatar"), match_type=match_type, match_score=score, score_breakdown=breakdown, match_reason=str(data.get("reason") or "资料存在一定匹配点"), suggestions=[str(x) for x in data.get("suggestions", []) if isinstance(x, str)][:3]))
    return AIMatchPage(match_type=match_type, items=items, page=page, page_size=page_size, total=len(scored), has_more=start + page_size < len(scored))

