"""OpenAI-compatible text generation with a deterministic test fallback."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import HTTPException

from app.core.config import settings


async def complete(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    if not settings.ai_enabled:
        if settings.is_test_mode:
            return _mock_response(messages, json_mode=json_mode)
        raise HTTPException(503, detail="AI服务未启用")
    if not settings.ai_api_key:
        raise HTTPException(503, detail="AI服务未配置 API Key")
    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.ai_api_key.get_secret_value()}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {"model": settings.ai_model, "messages": messages, "temperature": 0.4}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        async with httpx.AsyncClient(timeout=settings.ai_timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("AI返回内容为空")
        return content.strip()
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(503, detail="AI服务暂时不可用") from exc


def parse_json(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(503, detail="AI返回格式无效") from exc
    if not isinstance(value, dict):
        raise HTTPException(503, detail="AI返回格式无效")
    return value


def _mock_response(messages: list[dict[str, str]], *, json_mode: bool) -> str:
    user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    if "PROFILE_POLISH" in user:
        return json.dumps({"polished": user.split("\n", 1)[-1][:300], "changed_points": ["保留原意并调整表达"]}, ensure_ascii=False)
    if "SEARCH_PARSE" in user:
        return json.dumps({"normalized_query": user.split("\n", 1)[-1][:500], "filters": {}, "unresolved": []}, ensure_ascii=False)
    if "MATCH_EXPLAIN" in user:
        return json.dumps({"reason": "资料中的兴趣和生活方式存在重合，建议从共同兴趣开始交流。", "suggestions": ["可以从共同兴趣开始聊天"]}, ensure_ascii=False)
    if "ADVISOR_ADVICE" in user:
        return json.dumps({
            "analysis": "The reply is brief but not rejecting. Acknowledge it and continue with one light question.",
            "suggestions": [{
                "content": "It sounds like we have something in common. Do you prefer relaxing at home or going out on weekends?",
                "style": "natural",
                "reason": "Acknowledge common ground, then use one open question without interrogating."
            }],
            "risk_level": "none",
            "risk_notice": None,
            "next_step": "If replies remain brief, pause repeated questions and give the other person space."
        }, ensure_ascii=False)
    return "我可以帮你梳理这段聊天，并给出更具体的沟通建议。"
