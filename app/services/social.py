"""Relationship, chat, notification and safety services."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.social import (
    BlockRequest,
    ChatMessageCreate,
    ChatMessageResponse,
    ChatMessagePage,
    ChatSessionRequestCreate,
    ChatSessionRequestResponse,
    NotificationUnreadSummary,
    ChatSessionPage,
    ChatSessionResponse,
    NotificationItem,
    NotificationPage,
    PrivacyResponse,
    PrivacyUpdateRequest,
    RelationPage,
    RelationResponse,
    ReportRequest,
    ReportResponse,
    ReportAppealCreate,
    ReportAppealPage,
    ReportAppealResponse,
    ReportDetailResponse,
    ReportPage,
    SocialUser,
)
from app.schemas.admin import (
    AdminReportAppealItem,
    AdminReportAppealPage,
    AdminReportItem,
    AdminReportPage,
    ReportAppealReviewRequest,
    ReportAppealReviewResponse,
    ReportReviewRequest,
    ReportReviewResponse,
)
from app.services.discovery import _ensure_target, _target_rows
from app.services.notifications import emit_notification
from app.services.profile import _calculate_age
from app.services.restrictions import ensure_user_allowed
from app.services.restrictions import create_restriction
from app.schemas.restrictions import RestrictionCreate
from app.services.revisions import RevisionKind, increment_revision_and_enqueue


def _social_user(row: dict[str, Any]) -> SocialUser:
    return SocialUser(
        user_id=int(row["user_id"]),
        nickname=row.get("nickname"),
        avatar=row.get("avatar"),
        age=_calculate_age(row["birthday"]) if row.get("birthday") else None,
    )


async def _is_blocked(db: AsyncSession, left_id: int, right_id: int) -> bool:
    result = await db.execute(
        text("""SELECT 1 FROM user_block
               WHERE (user_id = :left_id AND target_user_id = :right_id)
                  OR (user_id = :right_id AND target_user_id = :left_id)"""),
        {"left_id": left_id, "right_id": right_id},
    )
    return bool(result.scalar())


async def ensure_users_can_interact(db: AsyncSession, left_id: int, right_id: int) -> None:
    """Reject interactions when either user has blocked the other."""
    if left_id == right_id or await _is_blocked(db, left_id, right_id):
        raise HTTPException(403, detail="双方当前不能进行该互动")


async def _match_exists(db: AsyncSession, user_id: int, target_id: int) -> bool:
    result = await db.execute(
        text("SELECT 1 FROM user_match WHERE user_id = :user_id AND target_user_id = :target_id AND status IN (1, 2)"),
        {"user_id": user_id, "target_id": target_id},
    )
    return bool(result.scalar())


async def set_like(db: AsyncSession, user_id: int, target_id: int, enabled: bool) -> RelationResponse:
    """用户级喜欢：私有列表，不因互喜欢建 match/会话。聊天仅经申请同意。"""
    await _ensure_target(db, user_id, target_id)
    if enabled:
        await db.execute(
            text(
                "INSERT IGNORE INTO user_favorite (user_id, target_user_id, type) "
                "VALUES (:user_id, :target_id, 1)"
            ),
            {"user_id": user_id, "target_id": target_id},
        )
        # 定版：喜欢不通知对方、不自动匹配、不建 chat_session
    else:
        await db.execute(
            text(
                "DELETE FROM user_favorite WHERE user_id = :user_id "
                "AND target_user_id = :target_id AND type = 1"
            ),
            {"user_id": user_id, "target_id": target_id},
        )
    await db.commit()
    # matched 仅反映是否仍有有效 match（申请同意等），不再由喜欢产生
    return RelationResponse(
        target_user_id=target_id,
        relation_type="like",
        enabled=enabled,
        matched=await _match_exists(db, user_id, target_id),
    )


async def set_follow(db: AsyncSession, user_id: int, target_id: int, enabled: bool) -> RelationResponse:
    await _ensure_target(db, user_id, target_id)
    if enabled:
        result = await db.execute(text("INSERT IGNORE INTO user_favorite (user_id, target_user_id, type) VALUES (:user_id, :target_id, 3)"), {"user_id": user_id, "target_id": target_id})
        if getattr(result, "rowcount", 1) > 0:
            await emit_notification(
                db,
                recipient_user_id=target_id,
                actor_user_id=user_id,
                event_type="follow",
                title="有人关注了你",
                content="你收到了一条新的关注",
                target_type="user",
                target_id=user_id,
            )
    else:
        await db.execute(text("DELETE FROM user_favorite WHERE user_id = :user_id AND target_user_id = :target_id AND type = 3"), {"user_id": user_id, "target_id": target_id})
    await db.commit()
    return RelationResponse(target_user_id=target_id, relation_type="follow", enabled=enabled)


async def _relation_page(db: AsyncSession, user_id: int, relation_type: str, incoming: bool, page: int, page_size: int) -> RelationPage:
    if relation_type == "match":
        ids_sql = "SELECT target_user_id FROM user_match WHERE user_id = :user_id AND status IN (1, 2) ORDER BY matched_at DESC"
    else:
        type_value = 1 if relation_type == "like" else 3
        field = "target_user_id" if incoming else "user_id"
        selected = "user_id" if incoming else "target_user_id"
        ids_sql = f"SELECT {selected} AS target_user_id FROM user_favorite WHERE {field} = :user_id AND type = {type_value} ORDER BY created_at DESC"
    rows = (await db.execute(text(ids_sql), {"user_id": user_id})).mappings().all()
    targets = await _target_rows(db, user_id, [int(row["target_user_id"]) for row in rows])
    visible = [_social_user(targets[int(row["target_user_id"])]) for row in rows if int(row["target_user_id"]) in targets]
    start = (page - 1) * page_size
    return RelationPage(items=visible[start:start + page_size], page=page, page_size=page_size, total=len(visible))


async def list_relation(db: AsyncSession, user_id: int, relation_type: str, incoming: bool, page: int, page_size: int) -> RelationPage:
    return await _relation_page(db, user_id, relation_type, incoming, page, page_size)


async def unmatch(db: AsyncSession, user_id: int, target_id: int) -> None:
    await _ensure_target(db, user_id, target_id)
    if not await _match_exists(db, user_id, target_id):
        raise HTTPException(404, detail="匹配关系不存在")
    await db.execute(text("UPDATE user_match SET status = 3, updated_at = UTC_TIMESTAMP() WHERE (user_id = :user_id AND target_user_id = :target_id) OR (user_id = :target_id AND target_user_id = :user_id)"), {"user_id": user_id, "target_id": target_id})
    await db.commit()


async def _session(db: AsyncSession, user_id: int, session_id: int) -> tuple[dict[str, Any], int]:
    result = await db.execute(text("""SELECT * FROM chat_session
        WHERE id = :session_id AND (user1_id = :user_id OR user2_id = :user_id)"""), {"session_id": session_id, "user_id": user_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="聊天会话不存在")
    target_id = int(row["user2_id"] if row["user1_id"] == user_id else row["user1_id"])
    if await _is_blocked(db, user_id, target_id) or not await _match_exists(db, user_id, target_id):
        raise HTTPException(403, detail="当前没有聊天权限")
    return dict(row), target_id


async def list_chat_sessions(db: AsyncSession, user_id: int, page: int, page_size: int) -> ChatSessionPage:
    count_result = await db.execute(text("""SELECT COUNT(*) FROM chat_session s
        WHERE ((s.user1_id = :user_id AND s.is_user1_hidden = 0) OR (s.user2_id = :user_id AND s.is_user2_hidden = 0))
          AND EXISTS (SELECT 1 FROM user_match um WHERE um.user_id = :user_id
                      AND um.target_user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END
                      AND um.status IN (1, 2))
          AND NOT EXISTS (SELECT 1 FROM user_restriction ban
                          WHERE ban.user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END
                            AND ban.restriction_type = 'TOTAL_BAN' AND ban.status = 1
                            AND ban.starts_at <= UTC_TIMESTAMP()
                            AND (ban.ends_at IS NULL OR ban.ends_at > UTC_TIMESTAMP()))
          AND NOT EXISTS (SELECT 1 FROM user_block ub WHERE (ub.user_id = :user_id
                      AND ub.target_user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END)
                   OR (ub.target_user_id = :user_id
                      AND ub.user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END))"""), {"user_id": user_id})
    total = int(count_result.scalar() or 0)
    result = await db.execute(text("""SELECT s.*, u.id AS target_id, u.nickname, u.avatar, u.birthday,
        CASE WHEN s.user1_id = :user_id THEN s.unread_count_user1 ELSE s.unread_count_user2 END AS unread_count,
        CASE WHEN s.user1_id = :user_id THEN s.user1_pinned_at ELSE s.user2_pinned_at END AS pinned_at
        FROM chat_session s JOIN users u ON u.id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END
        WHERE ((s.user1_id = :user_id AND s.is_user1_hidden = 0) OR (s.user2_id = :user_id AND s.is_user2_hidden = 0))
          AND EXISTS (SELECT 1 FROM user_match um WHERE um.user_id = :user_id
                      AND um.target_user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END
                      AND um.status IN (1, 2))
          AND NOT EXISTS (SELECT 1 FROM user_restriction ban
                          WHERE ban.user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END
                            AND ban.restriction_type = 'TOTAL_BAN' AND ban.status = 1
                            AND ban.starts_at <= UTC_TIMESTAMP()
                            AND (ban.ends_at IS NULL OR ban.ends_at > UTC_TIMESTAMP()))
          AND NOT EXISTS (SELECT 1 FROM user_block ub WHERE (ub.user_id = :user_id
                      AND ub.target_user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END)
                   OR (ub.target_user_id = :user_id
                      AND ub.user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END))
        ORDER BY (CASE WHEN s.user1_id = :user_id THEN s.user1_pinned_at ELSE s.user2_pinned_at END) IS NOT NULL DESC,
        COALESCE(CASE WHEN s.user1_id = :user_id THEN s.user1_pinned_at ELSE s.user2_pinned_at END, s.last_message_time, s.created_at) DESC
        LIMIT :limit OFFSET :offset"""), {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size})
    items = [ChatSessionResponse(id=int(row["id"]), target=SocialUser(user_id=int(row["target_id"]), nickname=row["nickname"], avatar=row["avatar"], age=_calculate_age(row["birthday"]) if row["birthday"] else None), last_message=row["last_message"], last_message_time=row["last_message_time"], unread_count=int(row["unread_count"] or 0), pinned=row["pinned_at"] is not None) for row in result.mappings().all()]
    return ChatSessionPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


def _message(row: dict[str, Any]) -> ChatMessageResponse:
    revoked = row.get("revoked_at") is not None
    return ChatMessageResponse(
        id=int(row["id"]),
        session_id=int(row["session_id"]),
        from_user_id=int(row["from_user_id"]),
        to_user_id=int(row["to_user_id"]),
        type=int(row["type"]),
        content="消息已撤回" if revoked else row.get("content"),
        media_url=None if revoked else row.get("media_url"),
        is_read=bool(row["is_read"]),
        revoked=revoked,
        is_recalled=revoked,
        recalled_at=row.get("revoked_at"),
        created_at=row["created_at"],
    )


async def list_messages(db: AsyncSession, user_id: int, session_id: int, page: int, page_size: int) -> list[ChatMessageResponse]:
    await _session(db, user_id, session_id)
    result = await db.execute(text("SELECT id, session_id, from_user_id, to_user_id, type, content, media_url, is_read, revoked_at, created_at FROM chat_message WHERE session_id = :session_id ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"), {"session_id": session_id, "limit": page_size, "offset": (page - 1) * page_size})
    return [_message(dict(row)) for row in reversed(result.mappings().all())]


async def send_message(db: AsyncSession, user_id: int, session_id: int, request: ChatMessageCreate) -> ChatMessageResponse:
    await ensure_user_allowed(db, user_id, "MESSAGE_RESTRICTED")
    session, target_id = await _session(db, user_id, session_id)
    result = await db.execute(text("""INSERT INTO chat_message (session_id, from_user_id, to_user_id, type, content, media_url)
        VALUES (:session_id, :from_id, :to_id, :type, :content, :media_url)"""), {"session_id": session_id, "from_id": user_id, "to_id": target_id, **request.model_dump()})
    preview = request.content if request.type == 1 else "[媒体消息]"
    unread_field = "unread_count_user1" if session["user1_id"] == target_id else "unread_count_user2"
    await db.execute(text(f"""UPDATE chat_session SET last_message = :last_message,
        last_message_time = UTC_TIMESTAMP(), {unread_field} = {unread_field} + 1,
        is_user1_hidden = CASE WHEN user1_id = :target_id THEN 0 ELSE is_user1_hidden END,
        is_user2_hidden = CASE WHEN user2_id = :target_id THEN 0 ELSE is_user2_hidden END,
        updated_at = UTC_TIMESTAMP() WHERE id = :session_id"""), {
            "last_message": preview, "session_id": session_id, "target_id": target_id,
        })
    await emit_notification(
        db, recipient_user_id=target_id, actor_user_id=user_id, event_type="message",
        title="?????", content=preview, target_type="chat_session",
        target_id=session_id, payload={"message_type": request.type},
    )
    await db.commit()
    created = await db.execute(text("SELECT id, session_id, from_user_id, to_user_id, type, content, media_url, is_read, revoked_at, created_at FROM chat_message WHERE id = :id"), {"id": result.lastrowid})
    return _message(dict(created.mappings().one()))


async def mark_messages_read(db: AsyncSession, user_id: int, session_id: int) -> None:
    session, _ = await _session(db, user_id, session_id)
    unread_field = "unread_count_user1" if session["user1_id"] == user_id else "unread_count_user2"
    await db.execute(text("UPDATE chat_message SET is_read = 1, read_at = UTC_TIMESTAMP() WHERE session_id = :session_id AND to_user_id = :user_id AND is_read = 0"), {"session_id": session_id, "user_id": user_id})
    await db.execute(text(f"UPDATE chat_session SET {unread_field} = 0 WHERE id = :session_id"), {"session_id": session_id})
    await db.commit()


async def recall_message(db: AsyncSession, user_id: int, message_id: int) -> ChatMessageResponse:
    result = await db.execute(text("""SELECT id, session_id, from_user_id, to_user_id, type,
        content, media_url, is_read, revoked_at, created_at
        FROM chat_message WHERE id = :id AND from_user_id = :user_id"""), {"id": message_id, "user_id": user_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="消息不存在或无法撤回")
    await _session(db, user_id, int(row["session_id"]))
    if row.get("revoked_at") is None:
        await db.execute(text("UPDATE chat_message SET revoked_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": message_id})
        refreshed = await db.execute(text("SELECT revoked_at FROM chat_message WHERE id = :id"), {"id": message_id})
        row = dict(row)
        row["revoked_at"] = refreshed.scalar()
    await emit_notification(
        db, recipient_user_id=int(row["to_user_id"]), actor_user_id=user_id,
        event_type="message_recalled", title="?????", content="?????????",
        target_type="chat_session", target_id=int(row["session_id"]),
        payload={"message_id": message_id},
    )
    await db.commit()
    return _message(dict(row))


async def revoke_message(db: AsyncSession, user_id: int, message_id: int) -> None:
    """Backward-compatible 204 endpoint; the recall endpoint returns the message projection."""
    await recall_message(db, user_id, message_id)


async def list_notifications(
    db: AsyncSession,
    user_id: int,
    page: int,
    page_size: int,
    notification_type: str | None = None,
) -> NotificationPage:
    condition = " AND notification_type = :notification_type" if notification_type else ""
    params: dict[str, Any] = {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size}
    if notification_type:
        params["notification_type"] = notification_type
    result = await db.execute(text(f"""SELECT id, notification_type, title, content, payload,
        related_user_id, related_id, target_type, target_id, is_read, created_at
        FROM user_notification WHERE user_id = :user_id{condition}
        ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"""), params)
    items = []
    for row in result.mappings().all():
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        items.append(NotificationItem(
            id=int(row["id"]), notification_type=row["notification_type"], title=row["title"],
            content=row["content"] or "", payload=payload, related_user_id=row["related_user_id"],
            related_id=row["related_id"], target_type=row.get("target_type"), target_id=row.get("target_id"),
            actor_user_id=row["related_user_id"], action=row["notification_type"],
            is_read=bool(row["is_read"]), created_at=row["created_at"],
        ))
    count_params = {key: value for key, value in params.items() if key not in ("limit", "offset")}
    total = int((await db.execute(text(f"SELECT COUNT(*) FROM user_notification WHERE user_id = :user_id{condition}"), count_params)).scalar() or 0)
    unread = int((await db.execute(text(f"SELECT COUNT(*) FROM user_notification WHERE user_id = :user_id AND is_read = 0{condition}"), count_params)).scalar() or 0)
    return NotificationPage(items=items, page=page, page_size=page_size, total=total, unread_count=unread)


async def mark_notification_read(db: AsyncSession, user_id: int, notification_id: int | None) -> None:
    condition = "id = :notification_id" if notification_id is not None else "1 = 1"
    params = {"user_id": user_id, "notification_id": notification_id}
    await db.execute(text(f"UPDATE user_notification SET is_read = 1, read_at = UTC_TIMESTAMP() WHERE user_id = :user_id AND {condition}"), params)
    await db.commit()


async def get_privacy(db: AsyncSession, user_id: int) -> PrivacyResponse:
    result = await db.execute(text("SELECT * FROM user_privacy WHERE user_id = :user_id"), {"user_id": user_id})
    row = result.mappings().first()
    values = dict(row) if row else {"user_id": user_id}
    defaults = {"hide_phone": 0, "hide_school": 0, "hide_company": 0, "hide_distance": 0, "hide_online_status": 0, "only_auth_can_contact": 0, "only_vip_can_see_detail": 0, "who_can_see_me": 1, "match_status": 1, "anonymous_browse_enabled": 0, "show_profile": 1, "show_likes": 1, "show_posts": 1, "notify_like": 1, "notify_comment": 1, "notify_follow": 1, "notify_message": 1, "notify_match": 1, "notify_apply": 1, "notify_system": 1, "notify_activity": 1}
    values = {**defaults, **values, "user_id": user_id}
    return PrivacyResponse(**{key: bool(value) if key not in ("user_id", "who_can_see_me", "match_status") else value for key, value in values.items() if key in PrivacyResponse.model_fields})


async def update_privacy(db: AsyncSession, user_id: int, request: PrivacyUpdateRequest) -> PrivacyResponse:
    values = request.model_dump(exclude_unset=True)
    values = {key: int(value) if isinstance(value, bool) else value for key, value in values.items()}
    if values:
        columns = ["user_id", *values]
        placeholders = ", ".join(f":{column}" for column in columns)
        updates = ", ".join(f"{column} = VALUES({column})" for column in values)
        await db.execute(text(f"INSERT INTO user_privacy ({', '.join(columns)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}, updated_at = UTC_TIMESTAMP()"), {"user_id": user_id, **values})
        await increment_revision_and_enqueue(
            db,
            user_id,
            RevisionKind.PRIVACY,
            tuple(values.keys()),
            "privacy_updated",
            10,
        )
        await db.commit()
    return await get_privacy(db, user_id)


async def list_blocks(db: AsyncSession, user_id: int) -> list[SocialUser]:
    result = await db.execute(text("SELECT target_user_id FROM user_block WHERE user_id = :user_id ORDER BY created_at DESC"), {"user_id": user_id})
    targets = await _target_rows(db, user_id, [int(row[0]) for row in result])
    return [_social_user(targets[target_id]) for target_id in targets]


async def set_block(db: AsyncSession, user_id: int, target_id: int, request: BlockRequest, enabled: bool) -> None:
    if enabled:
        await _ensure_target(db, user_id, target_id)
        inserted = await db.execute(text("INSERT IGNORE INTO user_block (user_id, target_user_id, reason) VALUES (:user_id, :target_id, :reason)"), {"user_id": user_id, "target_id": target_id, "reason": request.reason if request else None})
        await db.execute(text("UPDATE user_match SET status = 3, updated_at = UTC_TIMESTAMP() WHERE (user_id = :user_id AND target_user_id = :target_id) OR (user_id = :target_id AND target_user_id = :user_id)"), {"user_id": user_id, "target_id": target_id})
        await db.execute(text("UPDATE match_apply SET status = 3, updated_at = UTC_TIMESTAMP() WHERE status = 0 AND ((from_user_id = :user_id AND to_user_id = :target_id) OR (from_user_id = :target_id AND to_user_id = :user_id))"), {"user_id": user_id, "target_id": target_id})
        # Only enqueue an invalidation when the block actually took effect;
        # INSERT IGNORE with rowcount 0 means the pair was already blocked.
        if getattr(inserted, "rowcount", 1) > 0:
            await increment_revision_and_enqueue(db, user_id, RevisionKind.RELATIONSHIP, ("block",), "relationship_blocked", 10)
            await increment_revision_and_enqueue(db, target_id, RevisionKind.RELATIONSHIP, ("block",), "relationship_blocked", 10)
    else:
        result = await db.execute(text("SELECT 1 FROM users WHERE id = :target_id AND status = 1"), {"target_id": target_id})
        if not result.scalar():
            raise HTTPException(404, detail="目标用户不存在")
        deleted = await db.execute(text("DELETE FROM user_block WHERE user_id = :user_id AND target_user_id = :target_id"), {"user_id": user_id, "target_id": target_id})
        # Only enqueue an invalidation when the block was actually lifted;
        # DELETE with rowcount 0 means the pair was not blocked.
        if getattr(deleted, "rowcount", 1) > 0:
            await increment_revision_and_enqueue(db, user_id, RevisionKind.RELATIONSHIP, ("block",), "relationship_unblocked", 10)
            await increment_revision_and_enqueue(db, target_id, RevisionKind.RELATIONSHIP, ("block",), "relationship_unblocked", 10)
    await db.commit()


async def _ensure_report_images(db: AsyncSession, user_id: int, images: list[str]) -> None:
    if not images:
        return
    media = await db.execute(
        text("SELECT file_url FROM user_media WHERE user_id=:user_id AND deleted_at IS NULL"),
        {"user_id": user_id},
    )
    owned = {row[0] for row in media.all()}
    if any(image not in owned for image in images):
        raise HTTPException(422, detail="举报证据必须来自当前用户已上传的媒体")


async def _insert_report(
    db: AsyncSession,
    *,
    user_id: int,
    target_user_id: int,
    target_type: str,
    target_id: int | None,
    report_type: str,
    description: str | None,
    images: list[str],
) -> ReportResponse:
    result = await db.execute(
        text(
            """INSERT INTO user_report
            (user_id, target_user_id, target_type, target_id, type, `desc`, images)
            VALUES (:user_id, :target_user_id, :target_type, :target_id, :type, :description, :images)"""
        ),
        {
            "user_id": user_id,
            "target_user_id": target_user_id,
            "target_type": target_type,
            "target_id": target_id,
            "type": report_type,
            "description": description,
            "images": json.dumps(images, ensure_ascii=False),
        },
    )
    await db.commit()
    created = await db.execute(
        text(
            """SELECT id, target_user_id, target_type, target_id, type, status, created_at
            FROM user_report WHERE id = :id"""
        ),
        {"id": result.lastrowid},
    )
    row = dict(created.mappings().one())
    return ReportResponse(
        id=int(row["id"]),
        target_user_id=int(row["target_user_id"]),
        target_type=row.get("target_type") or "user",
        target_id=int(row["target_id"]) if row.get("target_id") is not None else None,
        type=row["type"],
        status=int(row["status"]),
        created_at=row["created_at"],
    )


async def create_report(db: AsyncSession, user_id: int, target_id: int, request: ReportRequest) -> ReportResponse:
    await _ensure_target(db, user_id, target_id)
    await _ensure_report_images(db, user_id, request.images)
    return await _insert_report(
        db,
        user_id=user_id,
        target_user_id=target_id,
        target_type="user",
        target_id=target_id,
        report_type=request.type,
        description=request.description,
        images=request.images,
    )


async def create_content_report(
    db: AsyncSession,
    user_id: int,
    *,
    target_type: str,
    target_id: int,
    reason_id: str,
    description: str | None = None,
    images: list[str] | None = None,
) -> ReportResponse:
    from app.services.community import REPORT_REASONS

    allowed = {item["id"] for item in REPORT_REASONS}
    if reason_id not in allowed:
        raise HTTPException(422, detail="举报原因无效")
    image_list = images or []
    await _ensure_report_images(db, user_id, image_list)

    if target_type == "post":
        result = await db.execute(
            text("SELECT id, user_id FROM community_post WHERE id = :target_id"),
            {"target_id": target_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(404, detail="动态不存在")
    elif target_type == "comment":
        result = await db.execute(
            text("SELECT id, user_id FROM community_comment WHERE id = :target_id"),
            {"target_id": target_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(404, detail="评论不存在")
    elif target_type == "paper_plane":
        result = await db.execute(
            text("SELECT id, user_id FROM paper_plane WHERE id = :target_id"),
            {"target_id": target_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(404, detail="纸飞机不存在")
    else:
        raise HTTPException(422, detail="不支持的举报对象类型")

    owner_id = int(row["user_id"])
    if owner_id == user_id:
        raise HTTPException(422, detail="不能举报自己的内容")

    # 防止同一用户对同一目标重复举报，刷爆审核队列
    duplicated = await db.execute(
        text(
            """SELECT 1 FROM user_report
            WHERE user_id = :user_id
              AND target_type = :target_type
              AND target_id = :target_id
              AND status = 0
            LIMIT 1"""
        ),
        {"user_id": user_id, "target_type": target_type, "target_id": target_id},
    )
    if duplicated.scalar():
        raise HTTPException(409, detail="该内容你已举报，请等待处理结果")

    return await _insert_report(
        db,
        user_id=user_id,
        target_user_id=owner_id,
        target_type=target_type,
        target_id=target_id,
        report_type=reason_id,
        description=description,
        images=image_list,
    )


async def list_admin_reports(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    status: int | None = None,
    target_type: str | None = None,
) -> AdminReportPage:
    where = ["1=1"]
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}
    if status is not None:
        where.append("status = :status")
        params["status"] = status
    if target_type is not None:
        where.append("target_type = :target_type")
        params["target_type"] = target_type
    where_sql = " AND ".join(where)
    total = int(
        (
            await db.execute(text(f"SELECT COUNT(*) FROM user_report WHERE {where_sql}"), params)
        ).scalar()
        or 0
    )
    result = await db.execute(
        text(
            f"""SELECT id, user_id AS reporter_user_id, target_user_id, target_type, target_id,
            type, `desc` AS description, status, result, COALESCE(action, 'none') AS action,
            reviewed_by, reviewed_at, created_at, updated_at
            FROM user_report
            WHERE {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit OFFSET :offset"""
        ),
        params,
    )
    items = []
    for row in result.mappings().all():
        data = dict(row)
        items.append(
            AdminReportItem(
                id=int(data["id"]),
                reporter_user_id=int(data["reporter_user_id"]),
                target_user_id=int(data["target_user_id"]),
                target_type=data.get("target_type") or "user",
                target_id=int(data["target_id"]) if data.get("target_id") is not None else None,
                type=data.get("type"),
                description=data.get("description"),
                status=int(data["status"]),
                result=data.get("result"),
                action=data.get("action") or "none",
                reviewed_by=int(data["reviewed_by"]) if data.get("reviewed_by") is not None else None,
                reviewed_at=data.get("reviewed_at"),
                created_at=data["created_at"],
                updated_at=data.get("updated_at"),
            )
        )
    return AdminReportPage(
        items=items,
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


def _user_report_detail(row: dict[str, Any], *, viewer_id: int) -> ReportDetailResponse:
    status = int(row["status"])
    target_user_id = int(row["target_user_id"])
    viewer_role = "reporter" if int(row["reporter_user_id"]) == viewer_id else "subject"
    return ReportDetailResponse(
        id=int(row["id"]),
        target_user_id=target_user_id,
        target_type=row.get("target_type") or "user",
        target_id=int(row["target_id"]) if row.get("target_id") is not None else None,
        viewer_role=viewer_role,
        type=row.get("type"),
        description=row.get("description") if viewer_role == "reporter" else None,
        status=status,
        result=row.get("result"),
        action=row.get("action") or "none",
        reviewed_at=row.get("reviewed_at"),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
        can_appeal=(
            viewer_id is not None
            and viewer_id == target_user_id
            and status == 1
            and not bool(row.get("has_appeal"))
        ),
    )


async def list_my_reports(
    db: AsyncSession, user_id: int, *, page: int, page_size: int
) -> ReportPage:
    where = "user_id = :user_id OR (target_user_id = :user_id AND status = 1)"
    total = int(
        (await db.execute(text(f"SELECT COUNT(*) FROM user_report WHERE {where}"), {"user_id": user_id})).scalar()
        or 0
    )
    result = await db.execute(
        text(
            f"""SELECT r.id, r.user_id AS reporter_user_id, r.target_user_id,
            r.target_type, r.target_id, r.type, r.`desc` AS description, r.status,
            r.result, COALESCE(r.action, 'none') AS action, r.reviewed_by,
            r.reviewed_at, r.created_at, r.updated_at,
            EXISTS (SELECT 1 FROM report_appeal a WHERE a.report_id = r.id) AS has_appeal
            FROM user_report r WHERE {where}
            ORDER BY r.created_at DESC, r.id DESC LIMIT :limit OFFSET :offset"""
        ),
        {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size},
    )
    return ReportPage(
        items=[_user_report_detail(dict(row), viewer_id=user_id) for row in result.mappings().all()],
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


async def get_admin_report(db: AsyncSession, report_id: int) -> AdminReportItem:
    result = await db.execute(
        text(
            """SELECT r.id, r.user_id AS reporter_user_id, r.target_user_id,
            r.target_type, r.target_id, r.type, r.`desc` AS description, r.status,
            r.result, COALESCE(r.action, 'none') AS action, r.reviewed_by,
            r.reviewed_at, r.created_at, r.updated_at,
            EXISTS (SELECT 1 FROM report_appeal a WHERE a.report_id = r.id) AS has_appeal
            FROM user_report r WHERE r.id = :report_id"""
        ),
        {"report_id": report_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="举报记录不存在")
    return AdminReportItem(**dict(row))


def _appeal_response(row: dict[str, Any]) -> ReportAppealResponse:
    return ReportAppealResponse(
        id=int(row["id"]),
        report_id=int(row["report_id"]),
        appellant_user_id=int(row["appellant_user_id"]),
        reason=row["reason"],
        status=int(row["status"]),
        result=row.get("result"),
        reviewed_at=row.get("reviewed_at"),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


async def create_report_appeal(
    db: AsyncSession,
    *,
    user_id: int,
    report_id: int,
    request: ReportAppealCreate,
) -> ReportAppealResponse:
    report_result = await db.execute(
        text(
            """SELECT id, target_user_id, target_type, target_id, status, action, reviewed_by
            FROM user_report WHERE id = :report_id FOR UPDATE"""
        ),
        {"report_id": report_id},
    )
    report = report_result.mappings().first()
    if not report:
        raise HTTPException(404, detail="举报记录不存在")
    if int(report["target_user_id"]) != user_id:
        raise HTTPException(403, detail="只有被举报人可以申诉")
    if int(report["status"]) != 1:
        raise HTTPException(409, detail="该举报结论不支持申诉")
    duplicated = await db.execute(
        text("SELECT 1 FROM report_appeal WHERE report_id = :report_id"),
        {"report_id": report_id},
    )
    if duplicated.scalar():
        raise HTTPException(409, detail="该举报已提交申诉")
    inserted = await db.execute(
        text(
            """INSERT INTO report_appeal (report_id, appellant_user_id, reason)
            VALUES (:report_id, :user_id, :reason)"""
        ),
        {"report_id": report_id, "user_id": user_id, "reason": request.reason},
    )
    await db.commit()
    created = await db.execute(
        text(
            """SELECT id, report_id, appellant_user_id, reason, status, result,
            reviewed_by, reviewed_at, created_at, updated_at
            FROM report_appeal WHERE id = :appeal_id"""
        ),
        {"appeal_id": inserted.lastrowid},
    )
    return _appeal_response(dict(created.mappings().one()))


async def list_my_report_appeals(
    db: AsyncSession, user_id: int, *, page: int, page_size: int
) -> ReportAppealPage:
    total = int(
        (
            await db.execute(
                text("SELECT COUNT(*) FROM report_appeal WHERE appellant_user_id = :user_id"),
                {"user_id": user_id},
            )
        ).scalar()
        or 0
    )
    result = await db.execute(
        text(
            """SELECT id, report_id, appellant_user_id, reason, status, result,
            reviewed_by, reviewed_at, created_at, updated_at FROM report_appeal
            WHERE appellant_user_id = :user_id
            ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"""
        ),
        {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size},
    )
    return ReportAppealPage(
        items=[_appeal_response(dict(row)) for row in result.mappings().all()],
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


async def list_admin_report_appeals(
    db: AsyncSession,
    *,
    page: int,
    page_size: int,
    status: int | None = None,
) -> AdminReportAppealPage:
    where = "1=1" if status is None else "a.status = :status"
    params: dict[str, Any] = {
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    if status is not None:
        params["status"] = status
    total = int(
        (await db.execute(text(f"SELECT COUNT(*) FROM report_appeal a WHERE {where}"), params)).scalar()
        or 0
    )
    result = await db.execute(
        text(
            f"""SELECT a.id, a.report_id, a.appellant_user_id, a.reason, a.status,
            a.result, a.reviewed_by, a.reviewed_at, a.created_at, a.updated_at,
            r.target_type, r.target_id, r.action AS original_action,
            r.reviewed_by AS original_reviewer_id
            FROM report_appeal a JOIN user_report r ON r.id = a.report_id
            WHERE {where} ORDER BY a.created_at ASC, a.id ASC
            LIMIT :limit OFFSET :offset"""
        ),
        params,
    )
    return AdminReportAppealPage(
        items=[AdminReportAppealItem(**dict(row)) for row in result.mappings().all()],
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


async def moderate_content(
    db: AsyncSession,
    *,
    target_type: str,
    target_id: int,
    hide: bool,
    reason: str | None = None,
    actor_id: int | None = None,
    source_report_id: int | None = None,
    expected_report_id: int | None = None,
) -> bool:
    expected_clause = (
        " AND moderation_status = 2 AND moderation_report_id = :expected_report_id"
        if expected_report_id is not None
        else ""
    )
    params = {
        "status": 2 if hide else 1,
        "target_id": target_id,
        "source_report_id": source_report_id if hide else None,
        "expected_report_id": expected_report_id,
    }
    if target_type == "post":
        result = await db.execute(
            text(
                "UPDATE community_post SET moderation_status = :status, "
                "moderation_report_id = :source_report_id, updated_at = UTC_TIMESTAMP() "
                f"WHERE id = :target_id{expected_clause}"
            ),
            params,
        )
        if result.rowcount == 0:
            if expected_report_id is not None:
                return False
            raise HTTPException(404, detail="动态不存在")
    elif target_type == "comment":
        result = await db.execute(
            text(
                "UPDATE community_comment SET moderation_status = :status, "
                "moderation_report_id = :source_report_id "
                f"WHERE id = :target_id{expected_clause}"
            ),
            params,
        )
        if result.rowcount == 0:
            if expected_report_id is not None:
                return False
            raise HTTPException(404, detail="评论不存在")
    elif target_type == "paper_plane":
        # lifecycle status 不动；仅改 moderation_status 1正常 2下架
        status = 2 if hide else 1
        result = await db.execute(
            text(
                "UPDATE paper_plane SET moderation_status = :status, "
                "moderation_report_id = :source_report_id "
                f"WHERE id = :target_id{expected_clause}"
            ),
            {**params, "status": status},
        )
        if result.rowcount == 0:
            if expected_report_id is not None:
                return False
            raise HTTPException(404, detail="纸飞机不存在")
    else:
        raise HTTPException(422, detail="不支持的内容类型")

    if actor_id is not None:
        await db.execute(
            text(
                """INSERT INTO business_audit_log
                (actor_user_id, action, resource_type, resource_id, reason)
                VALUES (:actor_id, :action, :resource_type, :resource_id, :reason)"""
            ),
            {
                "actor_id": actor_id,
                "action": "hide_content" if hide else "restore_content",
                "resource_type": target_type,
                "resource_id": target_id,
                "reason": reason,
            },
        )


    return True


async def review_report(db: AsyncSession, report_id: int, request: ReportReviewRequest, *, actor_id: int | None = None) -> ReportReviewResponse:
    action = request.action or "none"
    if action in ("hide_content", "restore_content") and request.status != 1:
        raise HTTPException(422, detail="内容处置只能用于成立的举报")
    if action == "dismiss" and request.status != 2:
        raise HTTPException(422, detail="dismiss 只能用于驳回的举报")

    result = await db.execute(
        text(
            """SELECT id, user_id, target_user_id, target_type, target_id, status
            FROM user_report
            WHERE id = :report_id FOR UPDATE"""
        ),
        {"report_id": report_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="举报记录不存在")
    if int(row["status"]) != 0:
        raise HTTPException(409, detail="举报已进入终态，不能重复审核")

    status = request.status

    content_moderated = False
    restriction_created = False
    target_type = row.get("target_type") or "user"
    target_id = row.get("target_id")
    if action in ("hide_content", "restore_content"):
        if target_type == "user" or target_id is None:
            raise HTTPException(422, detail="用户举报不支持内容处置，请使用内容下架接口")
        await moderate_content(
            db,
            target_type=target_type,
            target_id=int(target_id),
            hide=(action == "hide_content"),
            reason=request.result,
            actor_id=actor_id,
            source_report_id=report_id if action == "hide_content" else None,
        )
        content_moderated = True
    if action == "restrict_user":
        await create_restriction(
            db,
            int(row["target_user_id"]),
            RestrictionCreate(
                restriction_type=request.restriction_type,
                reason_code=request.restriction_reason_code,
                reason=request.result,
                ends_at=request.restriction_ends_at,
            ),
            actor_id or 0,
            commit=False,
        )
        restriction_created = True

    await db.execute(
        text(
            """UPDATE user_report
            SET status = :status, result = :result, action = :action,
                reviewed_by = :reviewed_by, reviewed_at = UTC_TIMESTAMP(),
                updated_at = UTC_TIMESTAMP()
            WHERE id = :report_id"""
        ),
        {
            "report_id": report_id,
            "status": status,
            "result": request.result,
            "action": action,
            "reviewed_by": actor_id,
        },
    )
    await emit_notification(
        db,
        recipient_user_id=int(row["user_id"]),
        actor_user_id=None,
        event_type="report_result",
        title="举报处理结果",
        content=request.result,
        target_type="report",
        target_id=report_id,
    )
    if status == 1:
        await emit_notification(
            db,
            recipient_user_id=int(row["target_user_id"]),
            actor_user_id=None,
            event_type="report_result",
            title="内容治理通知",
            content=request.result,
            target_type="report",
            target_id=report_id,
        )
    await db.commit()
    return ReportReviewResponse(
        report_id=report_id,
        status=status,
        result=request.result,
        action=action,
        content_moderated=content_moderated,
        restriction_created=restriction_created,
    )


async def review_report_appeal(
    db: AsyncSession,
    *,
    appeal_id: int,
    request: ReportAppealReviewRequest,
    actor_id: int,
) -> ReportAppealReviewResponse:
    result = await db.execute(
        text(
            """SELECT a.id, a.report_id, a.appellant_user_id, a.status,
            r.target_type, r.target_id, r.action AS original_action,
            r.reviewed_by AS original_reviewer_id
            FROM report_appeal a JOIN user_report r ON r.id = a.report_id
            WHERE a.id = :appeal_id FOR UPDATE"""
        ),
        {"appeal_id": appeal_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="申诉记录不存在")
    if int(row["status"]) != 0:
        raise HTTPException(409, detail="申诉已进入终态，不能重复审核")
    original_reviewer = row.get("original_reviewer_id")
    if original_reviewer is not None and int(original_reviewer) == actor_id:
        raise HTTPException(409, detail="申诉必须由不同于原举报审核人的管理员复审")

    content_restored = False
    target_type = row.get("target_type") or "user"
    target_id = row.get("target_id")
    if (
        request.status == 1
        and row.get("original_action") == "hide_content"
        and target_type != "user"
        and target_id is not None
    ):
        content_restored = await moderate_content(
            db,
            target_type=target_type,
            target_id=int(target_id),
            hide=False,
            reason=request.result,
            actor_id=actor_id,
            expected_report_id=int(row["report_id"]),
        )
    await db.execute(
        text(
            """UPDATE report_appeal SET status = :status, result = :result,
            reviewed_by = :reviewed_by, reviewed_at = UTC_TIMESTAMP(),
            updated_at = UTC_TIMESTAMP() WHERE id = :appeal_id"""
        ),
        {
            "appeal_id": appeal_id,
            "status": request.status,
            "result": request.result,
            "reviewed_by": actor_id,
        },
    )
    await emit_notification(
        db,
        recipient_user_id=int(row["appellant_user_id"]),
        actor_user_id=None,
        event_type="appeal_result",
        title="申诉复审结果",
        content=request.result,
        target_type="report",
        target_id=int(row["report_id"]),
    )
    await db.commit()
    return ReportAppealReviewResponse(
        appeal_id=appeal_id,
        report_id=int(row["report_id"]),
        status=request.status,
        result=request.result,
        content_restored=content_restored,
    )


async def set_chat_session_visibility(db: AsyncSession, user_id: int, session_id: int, *, hidden: bool | None = None, pinned: bool | None = None) -> None:
    session, _ = await _session(db, user_id, session_id)
    updates: list[str] = []
    if hidden is not None:
        field = "is_user1_hidden" if int(session["user1_id"]) == user_id else "is_user2_hidden"
        updates.append(f"{field} = {1 if hidden else 0}")
    if pinned is not None:
        field = "user1_pinned_at" if int(session["user1_id"]) == user_id else "user2_pinned_at"
        updates.append(f"{field} = {'UTC_TIMESTAMP()' if pinned else 'NULL'}")
    if not updates:
        return
    await db.execute(text(f"UPDATE chat_session SET {', '.join(updates)}, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": session_id})
    await db.commit()


async def list_messages_cursor(db: AsyncSession, user_id: int, session_id: int, cursor: int | None, page_size: int) -> ChatMessagePage:
    await _session(db, user_id, session_id)
    condition = "AND id < :cursor" if cursor is not None else ""
    params = {"session_id": session_id, "cursor": cursor, "limit": page_size + 1}
    rows = (await db.execute(text(f"""SELECT id, session_id, from_user_id, to_user_id, type, content, media_url,
        is_read, revoked_at, created_at FROM chat_message WHERE session_id = :session_id {condition}
        ORDER BY id DESC LIMIT :limit"""), params)).mappings().all()
    has_more = len(rows) > page_size
    selected = rows[:page_size]
    items = [_message(dict(row)) for row in reversed(selected)]
    return ChatMessagePage(items=items, next_cursor=int(selected[-1]["id"]) if has_more and selected else None, has_more=has_more)


def _request_response(row: Any) -> ChatSessionRequestResponse:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    return ChatSessionRequestResponse(**{**dict(row), "payload": payload})


async def create_chat_session_request(db: AsyncSession, user_id: int, session_id: int, request: ChatSessionRequestCreate) -> ChatSessionRequestResponse:
    _, target_id = await _session(db, user_id, session_id)
    existing = await db.execute(text("""SELECT id FROM chat_session_request WHERE session_id = :session_id
        AND requester_id = :user_id AND request_type = :request_type AND status = 'PENDING'
        AND (expire_at IS NULL OR expire_at > UTC_TIMESTAMP()) LIMIT 1"""),
        {"session_id": session_id, "user_id": user_id, "request_type": request.request_type})
    if existing.scalar():
        raise HTTPException(409, detail="同类型请求正在等待处理")
    expire_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=request.expire_hours)
    result = await db.execute(text("""INSERT INTO chat_session_request
        (session_id, requester_id, responder_id, request_type, payload, expire_at)
        VALUES (:session_id, :requester_id, :responder_id, :request_type, :payload, :expire_at)"""),
        {"session_id": session_id, "requester_id": user_id, "responder_id": target_id,
         "request_type": request.request_type, "payload": json.dumps(request.payload, ensure_ascii=False) if request.payload else None,
         "expire_at": expire_at})
    await emit_notification(
        db, recipient_user_id=target_id, actor_user_id=user_id,
        event_type="chat_session_request", title="??????",
        content="????????????", target_type="chat_session_request",
        target_id=int(result.lastrowid), payload={"request_type": request.request_type},
    )
    await db.commit()
    row = (await db.execute(text("SELECT * FROM chat_session_request WHERE id = :id"), {"id": result.lastrowid})).mappings().one()
    return _request_response(row)


async def list_chat_session_requests(db: AsyncSession, user_id: int, session_id: int) -> list[ChatSessionRequestResponse]:
    await _session(db, user_id, session_id)
    await db.execute(text("UPDATE chat_session_request SET status = 'EXPIRED' WHERE session_id = :id AND status = 'PENDING' AND expire_at <= UTC_TIMESTAMP()"), {"id": session_id})
    rows = (await db.execute(text("""SELECT * FROM chat_session_request WHERE session_id = :id
        AND (requester_id = :user_id OR responder_id = :user_id) ORDER BY id DESC"""), {"id": session_id, "user_id": user_id})).mappings().all()
    await db.commit()
    return [_request_response(row) for row in rows]


async def respond_chat_session_request(db: AsyncSession, user_id: int, request_id: int, action: str) -> ChatSessionRequestResponse:
    row = (await db.execute(text("SELECT * FROM chat_session_request WHERE id = :id FOR UPDATE"), {"id": request_id})).mappings().first()
    if not row:
        raise HTTPException(404, detail="会话请求不存在")
    await _session(db, user_id, int(row["session_id"]))
    if row["status"] != "PENDING" or (row["expire_at"] and row["expire_at"] <= datetime.now(UTC).replace(tzinfo=None)):
        raise HTTPException(409, detail="会话请求已处理或已过期")
    if action == "WITHDRAW":
        if int(row["requester_id"]) != user_id:
            raise HTTPException(403, detail="只有发起人可以撤回")
        status = "WITHDRAWN"
    else:
        if int(row["responder_id"]) != user_id:
            raise HTTPException(403, detail="只有接收人可以处理")
        status = "ACCEPTED" if action == "ACCEPT" else "REJECTED"
    await db.execute(text("UPDATE chat_session_request SET status = :status, responded_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"status": status, "id": request_id})
    recipient_id = int(row["requester_id"]) if user_id == int(row["responder_id"]) else int(row["responder_id"])
    await emit_notification(
        db, recipient_user_id=recipient_id, actor_user_id=user_id,
        event_type="chat_session_request_result", title="????????",
        content=f"????????{status}", target_type="chat_session_request",
        target_id=request_id, payload={"status": status, "request_type": row["request_type"]},
    )
    await db.commit()
    updated = (await db.execute(text("SELECT * FROM chat_session_request WHERE id = :id"), {"id": request_id})).mappings().one()
    return _request_response(updated)


async def notification_unread_summary(db: AsyncSession, user_id: int) -> NotificationUnreadSummary:
    rows = (await db.execute(text("""SELECT notification_type, COUNT(*) AS count FROM user_notification
        WHERE user_id = :user_id AND is_read = 0 GROUP BY notification_type"""), {"user_id": user_id})).mappings().all()
    categories = {str(row["notification_type"]): int(row["count"]) for row in rows}
    return NotificationUnreadSummary(total=sum(categories.values()), categories=categories)
