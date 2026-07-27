"""Relationship, chat, notification and safety services."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.social import (
    BlockRequest,
    ChatMessageCreate,
    ChatMessageResponse,
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
    SocialUser,
)
from app.schemas.admin import ReportReviewRequest, ReportReviewResponse
from app.services.discovery import _ensure_target, _target_rows
from app.services.profile import _calculate_age


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


async def _notify(db: AsyncSession, user_id: int, notification_type: str, title: str, content: str, related_user_id: int, related_id: int | None = None) -> None:
    await db.execute(text("""INSERT INTO user_notification
        (user_id, notification_type, title, content, payload, related_user_id, related_id)
        VALUES (:user_id, :notification_type, :title, :content, :payload, :related_user_id, :related_id)"""), {
        "user_id": user_id,
        "notification_type": notification_type,
        "title": title,
        "content": content,
        "payload": json.dumps({"related_user_id": related_user_id}),
        "related_user_id": related_user_id,
        "related_id": related_id,
    })


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
        await db.execute(text("INSERT IGNORE INTO user_favorite (user_id, target_user_id, type) VALUES (:user_id, :target_id, 3)"), {"user_id": user_id, "target_id": target_id})
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
          AND NOT EXISTS (SELECT 1 FROM user_block ub WHERE (ub.user_id = :user_id
                      AND ub.target_user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END)
                   OR (ub.target_user_id = :user_id
                      AND ub.user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END))"""), {"user_id": user_id})
    total = int(count_result.scalar() or 0)
    result = await db.execute(text("""SELECT s.*, u.id AS target_id, u.nickname, u.avatar, u.birthday,
        CASE WHEN s.user1_id = :user_id THEN s.unread_count_user1 ELSE s.unread_count_user2 END AS unread_count
        FROM chat_session s JOIN users u ON u.id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END
        WHERE ((s.user1_id = :user_id AND s.is_user1_hidden = 0) OR (s.user2_id = :user_id AND s.is_user2_hidden = 0))
          AND EXISTS (SELECT 1 FROM user_match um WHERE um.user_id = :user_id
                      AND um.target_user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END
                      AND um.status IN (1, 2))
          AND NOT EXISTS (SELECT 1 FROM user_block ub WHERE (ub.user_id = :user_id
                      AND ub.target_user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END)
                   OR (ub.target_user_id = :user_id
                      AND ub.user_id = CASE WHEN s.user1_id = :user_id THEN s.user2_id ELSE s.user1_id END))
        ORDER BY COALESCE(s.last_message_time, s.created_at) DESC LIMIT :limit OFFSET :offset"""), {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size})
    items = [ChatSessionResponse(id=int(row["id"]), target=SocialUser(user_id=int(row["target_id"]), nickname=row["nickname"], avatar=row["avatar"], age=_calculate_age(row["birthday"]) if row["birthday"] else None), last_message=row["last_message"], last_message_time=row["last_message_time"], unread_count=int(row["unread_count"] or 0)) for row in result.mappings().all()]
    return ChatSessionPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


def _message(row: dict[str, Any]) -> ChatMessageResponse:
    revoked = row.get("revoked_at") is not None
    return ChatMessageResponse(id=int(row["id"]), session_id=int(row["session_id"]), from_user_id=int(row["from_user_id"]), to_user_id=int(row["to_user_id"]), type=int(row["type"]), content="消息已撤回" if revoked else row.get("content"), media_url=None if revoked else row.get("media_url"), is_read=bool(row["is_read"]), revoked=revoked, created_at=row["created_at"])


async def list_messages(db: AsyncSession, user_id: int, session_id: int, page: int, page_size: int) -> list[ChatMessageResponse]:
    await _session(db, user_id, session_id)
    result = await db.execute(text("SELECT id, session_id, from_user_id, to_user_id, type, content, media_url, is_read, revoked_at, created_at FROM chat_message WHERE session_id = :session_id ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"), {"session_id": session_id, "limit": page_size, "offset": (page - 1) * page_size})
    return [_message(dict(row)) for row in reversed(result.mappings().all())]


async def send_message(db: AsyncSession, user_id: int, session_id: int, request: ChatMessageCreate) -> ChatMessageResponse:
    session, target_id = await _session(db, user_id, session_id)
    result = await db.execute(text("""INSERT INTO chat_message (session_id, from_user_id, to_user_id, type, content, media_url)
        VALUES (:session_id, :from_id, :to_id, :type, :content, :media_url)"""), {"session_id": session_id, "from_id": user_id, "to_id": target_id, **request.model_dump()})
    preview = request.content if request.type == 1 else "[媒体消息]"
    unread_field = "unread_count_user1" if session["user1_id"] == target_id else "unread_count_user2"
    await db.execute(text(f"""UPDATE chat_session SET last_message = :last_message,
        last_message_time = UTC_TIMESTAMP(), {unread_field} = {unread_field} + 1,
        updated_at = UTC_TIMESTAMP() WHERE id = :session_id"""), {"last_message": preview, "session_id": session_id})
    await db.commit()
    created = await db.execute(text("SELECT id, session_id, from_user_id, to_user_id, type, content, media_url, is_read, revoked_at, created_at FROM chat_message WHERE id = :id"), {"id": result.lastrowid})
    return _message(dict(created.mappings().one()))


async def mark_messages_read(db: AsyncSession, user_id: int, session_id: int) -> None:
    session, _ = await _session(db, user_id, session_id)
    unread_field = "unread_count_user1" if session["user1_id"] == user_id else "unread_count_user2"
    await db.execute(text("UPDATE chat_message SET is_read = 1, read_at = UTC_TIMESTAMP() WHERE session_id = :session_id AND to_user_id = :user_id AND is_read = 0"), {"session_id": session_id, "user_id": user_id})
    await db.execute(text(f"UPDATE chat_session SET {unread_field} = 0 WHERE id = :session_id"), {"session_id": session_id})
    await db.commit()


async def revoke_message(db: AsyncSession, user_id: int, message_id: int) -> None:
    result = await db.execute(text("SELECT id, session_id FROM chat_message WHERE id = :id AND from_user_id = :user_id AND revoked_at IS NULL"), {"id": message_id, "user_id": user_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="消息不存在或无法撤回")
    await _session(db, user_id, int(row["session_id"]))
    await db.execute(text("UPDATE chat_message SET revoked_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": message_id})
    await db.commit()


async def list_notifications(db: AsyncSession, user_id: int, page: int, page_size: int) -> NotificationPage:
    params = {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size}
    result = await db.execute(text("SELECT id, notification_type, title, content, payload, related_user_id, related_id, is_read, created_at FROM user_notification WHERE user_id = :user_id ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"), params)
    items = []
    for row in result.mappings().all():
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        items.append(NotificationItem(id=int(row["id"]), notification_type=row["notification_type"], title=row["title"], content=row["content"] or "", payload=payload, related_user_id=row["related_user_id"], related_id=row["related_id"], is_read=bool(row["is_read"]), created_at=row["created_at"]))
    total = int((await db.execute(text("SELECT COUNT(*) FROM user_notification WHERE user_id = :user_id"), {"user_id": user_id})).scalar() or 0)
    unread = int((await db.execute(text("SELECT COUNT(*) FROM user_notification WHERE user_id = :user_id AND is_read = 0"), {"user_id": user_id})).scalar() or 0)
    return NotificationPage(items=items, page=page, page_size=page_size, total=total, unread_count=unread)


async def mark_notification_read(db: AsyncSession, user_id: int, notification_id: int | None) -> None:
    condition = "notification_id = :notification_id" if notification_id is not None else "1 = 1"
    params = {"user_id": user_id, "notification_id": notification_id}
    await db.execute(text(f"UPDATE user_notification SET is_read = 1, read_at = UTC_TIMESTAMP() WHERE user_id = :user_id AND {condition}"), params)
    await db.commit()


async def get_privacy(db: AsyncSession, user_id: int) -> PrivacyResponse:
    result = await db.execute(text("SELECT * FROM user_privacy WHERE user_id = :user_id"), {"user_id": user_id})
    row = result.mappings().first()
    values = dict(row) if row else {"user_id": user_id}
    defaults = {"hide_phone": 0, "hide_school": 0, "hide_company": 0, "hide_distance": 0, "hide_online_status": 0, "only_auth_can_contact": 0, "only_vip_can_see_detail": 0, "who_can_see_me": 1, "match_status": 1, "anonymous_browse_enabled": 0, "show_profile": 1, "show_likes": 1, "show_posts": 1, "notify_like": 1, "notify_comment": 1, "notify_match": 1, "notify_apply": 1, "notify_system": 1, "notify_activity": 1}
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
        await db.commit()
    return await get_privacy(db, user_id)


async def list_blocks(db: AsyncSession, user_id: int) -> list[SocialUser]:
    result = await db.execute(text("SELECT target_user_id FROM user_block WHERE user_id = :user_id ORDER BY created_at DESC"), {"user_id": user_id})
    targets = await _target_rows(db, user_id, [int(row[0]) for row in result])
    return [_social_user(targets[target_id]) for target_id in targets]


async def set_block(db: AsyncSession, user_id: int, target_id: int, request: BlockRequest, enabled: bool) -> None:
    if enabled:
        await _ensure_target(db, user_id, target_id)
        await db.execute(text("INSERT IGNORE INTO user_block (user_id, target_user_id, reason) VALUES (:user_id, :target_id, :reason)"), {"user_id": user_id, "target_id": target_id, "reason": request.reason if request else None})
        await db.execute(text("UPDATE user_match SET status = 3, updated_at = UTC_TIMESTAMP() WHERE (user_id = :user_id AND target_user_id = :target_id) OR (user_id = :target_id AND target_user_id = :user_id"), {"user_id": user_id, "target_id": target_id})
        await db.execute(text("UPDATE match_apply SET status = 3, updated_at = UTC_TIMESTAMP() WHERE status = 0 AND ((from_user_id = :user_id AND to_user_id = :target_id) OR (from_user_id = :target_id AND to_user_id = :user_id))"), {"user_id": user_id, "target_id": target_id})
    else:
        result = await db.execute(text("SELECT 1 FROM users WHERE id = :target_id AND status = 1"), {"target_id": target_id})
        if not result.scalar():
            raise HTTPException(404, detail="目标用户不存在")
        await db.execute(text("DELETE FROM user_block WHERE user_id = :user_id AND target_user_id = :target_id"), {"user_id": user_id, "target_id": target_id})
    await db.commit()


async def create_report(db: AsyncSession, user_id: int, target_id: int, request: ReportRequest) -> ReportResponse:
    await _ensure_target(db, user_id, target_id)
    if request.images:
        media = await db.execute(text("SELECT file_url FROM user_media WHERE user_id=:user_id AND deleted_at IS NULL"), {"user_id": user_id})
        owned = {row[0] for row in media.all()}
        if any(image not in owned for image in request.images):
            raise HTTPException(422, detail="举报证据必须来自当前用户已上传的媒体")
    result = await db.execute(text("""INSERT INTO user_report (user_id, target_user_id, type, `desc`, images)
        VALUES (:user_id, :target_id, :type, :description, :images)"""), {"user_id": user_id, "target_id": target_id, "type": request.type, "description": request.description, "images": json.dumps(request.images, ensure_ascii=False)})
    await db.commit()
    created = await db.execute(text("SELECT id, target_user_id, type, status, created_at FROM user_report WHERE id = :id"), {"id": result.lastrowid})
    return ReportResponse(**created.mappings().one())


async def review_report(db: AsyncSession, report_id: int, request: ReportReviewRequest) -> ReportReviewResponse:
    result = await db.execute(text("SELECT id FROM user_report WHERE id = :report_id FOR UPDATE"), {"report_id": report_id})
    if not result.scalar():
        raise HTTPException(404, detail="举报记录不存在")
    await db.execute(text("UPDATE user_report SET status = :status, result = :result, updated_at = UTC_TIMESTAMP() WHERE id = :report_id"), {"report_id": report_id, "status": request.status, "result": request.result})
    await db.commit()
    return ReportReviewResponse(report_id=report_id, status=request.status, result=request.result)
