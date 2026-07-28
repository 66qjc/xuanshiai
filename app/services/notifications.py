"""Central notification event policy and persistence."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


_PREFERENCE_BY_EVENT = {
    "like": "notify_like",
    "superlike": "notify_like",
    "comment": "notify_comment",
    "reply": "notify_comment",
    "follow": "notify_follow",
    "match": "notify_match",
    "match_application": "notify_apply",
    "match_application_accepted": "notify_apply",
    "match_application_rejected": "notify_apply",
    "apply": "notify_apply",
    "message": "notify_message",
    "activity": "notify_activity",
    "system": "notify_system",
}
_MANDATORY_EVENT_TYPES = frozenset({
    "report_result",
    "appeal_result",
    "community_moderation_submitted",
    "community_moderation_result",
})


def _bounded(value: str | None, length: int) -> str | None:
    return value[:length] if value is not None else None


async def ensure_interaction_allowed(
    db: AsyncSession, *, actor_user_id: int, target_user_id: int
) -> None:
    if actor_user_id == target_user_id:
        return
    blocked = await db.execute(
        text(
            """SELECT 1 FROM user_block
            WHERE (user_id = :target_id AND target_user_id = :actor_id)
               OR (user_id = :actor_id AND target_user_id = :target_id)
            LIMIT 1"""
        ),
        {"target_id": target_user_id, "actor_id": actor_user_id},
    )
    if blocked.scalar():
        raise HTTPException(403, detail="拉黑关系下不能继续互动")


async def emit_notification(
    db: AsyncSession,
    *,
    recipient_user_id: int,
    actor_user_id: int | None,
    event_type: str,
    title: str,
    content: str,
    target_type: str | None = None,
    target_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Persist an event when recipient policy and relationship safety allow it."""
    if actor_user_id is not None and actor_user_id == recipient_user_id:
        return False

    if actor_user_id is not None:
        blocked = await db.execute(
            text(
                """SELECT 1 FROM user_block
                WHERE (user_id = :recipient_id AND target_user_id = :actor_id)
                   OR (user_id = :actor_id AND target_user_id = :recipient_id)
                LIMIT 1"""
            ),
            {"recipient_id": recipient_user_id, "actor_id": actor_user_id},
        )
        if blocked.scalar():
            return False

    if event_type not in _MANDATORY_EVENT_TYPES:
        preference = _PREFERENCE_BY_EVENT.get(event_type, "notify_system")
        enabled = await db.execute(
            text(
                f"SELECT COALESCE((SELECT {preference} FROM user_privacy "
                "WHERE user_id = :recipient_id), 1)"
            ),
            {"recipient_id": recipient_user_id},
        )
        if not bool(enabled.scalar()):
            return False

    event_payload = dict(payload or {})
    if actor_user_id is not None:
        event_payload.setdefault("related_user_id", actor_user_id)
    if target_type is not None:
        event_payload.setdefault("target_type", target_type)
    if target_id is not None:
        event_payload.setdefault("target_id", target_id)
    await db.execute(
        text(
            """INSERT INTO user_notification
            (user_id, notification_type, title, content, payload, related_user_id,
             related_id, target_type, target_id)
            VALUES (:user_id, :notification_type, :title, :content, :payload,
                    :related_user_id, :related_id, :target_type, :target_id)"""
        ),
        {
            "user_id": recipient_user_id,
            "notification_type": _bounded(event_type, 64),
            "title": _bounded(title, 128),
            "content": _bounded(content, 255),
            "payload": json.dumps(event_payload, ensure_ascii=False),
            "related_user_id": actor_user_id,
            "related_id": target_id,
            "target_type": _bounded(target_type, 32),
            "target_id": target_id,
        },
    )
    return True
