"""Community moderation queue and immutable review transitions."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.admin import (
    AdminGrantRequest,
    AdminGrantResponse,
    ModerationItem,
    ModerationItemPage,
    ModerationReviewRequest,
    ModerationReviewResponse,
)
from app.services.notifications import emit_notification

TARGET_TABLES = {
    "post": ("community_post", "moderation_status", "content"),
    "comment": ("community_comment", "moderation_status", "content"),
    "paper_plane": ("paper_plane", "moderation_status", "content"),
    "paper_plane_reply": ("paper_plane_reply", "moderation_status", "content"),
    "paper_plane_message": ("paper_plane_message", "moderation_status", "content"),
    "media": ("community_media", "moderation_status", "file_url"),
}

_NUMERIC_MODERATION_TARGETS = {"post", "comment", "paper_plane"}


async def list_moderation_items(db: AsyncSession, *, page: int, page_size: int, status: str = "pending", target_type: str | None = None) -> ModerationItemPage:
    where = ["status = :status"]
    params: dict[str, Any] = {"status": status, "limit": page_size, "offset": (page - 1) * page_size}
    if target_type:
        if target_type not in TARGET_TABLES:
            raise HTTPException(422, detail="不支持的审核对象类型")
        where.append("target_type = :target_type")
        params["target_type"] = target_type
    where_sql = " AND ".join(where)
    total = int((await db.execute(text(f"SELECT COUNT(*) FROM community_moderation_task WHERE {where_sql}"), params)).scalar() or 0)
    result = await db.execute(text(f"""SELECT id, target_type, target_id, user_id, status,
        risk_level, provider, matched_words, raw_content, display_content, reason, created_at, expires_at
        FROM community_moderation_task WHERE {where_sql}
        ORDER BY risk_level DESC, created_at ASC, id ASC LIMIT :limit OFFSET :offset"""), params)
    items = []
    for row in result.mappings().all():
        data = dict(row)
        raw_words = data.get("matched_words") or []
        if isinstance(raw_words, str):
            try:
                raw_words = json.loads(raw_words)
            except json.JSONDecodeError:
                raw_words = []
        items.append(ModerationItem(**data, matched_words=list(raw_words)))
    return ModerationItemPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def review_moderation_item(db: AsyncSession, task_id: int, request: ModerationReviewRequest, *, admin_id: int) -> ModerationReviewResponse:
    result = await db.execute(text("SELECT * FROM community_moderation_task WHERE id = :id FOR UPDATE"), {"id": task_id})
    task = result.mappings().first()
    if not task:
        raise HTTPException(404, detail="审核任务不存在")
    if task["status"] != "pending":
        raise HTTPException(409, detail="审核任务已完成，不允许重复改判")
    action_status = {"approve": "approved", "reject": "rejected", "replace": "replaced", "delete": "deleted", "hide": "hidden"}[request.action]
    if request.action == "replace" and not request.display_content:
        raise HTTPException(422, detail="替换审核必须提供展示内容")
    if task["target_type"] == "media" and request.action == "replace":
        raise HTTPException(422, detail="媒体不支持文本替换，请通过、下架或删除")
    table, status_column, content_column = TARGET_TABLES[task["target_type"]]
    if task["target_type"] in _NUMERIC_MODERATION_TARGETS:
        # Community tables use 0=pending, 1=approved, 2=hidden.
        db_status = 1 if action_status in {"approved", "replaced"} else 2
    else:
        db_status = action_status
    params = {"id": task["target_id"], "status": db_status, "reason": request.reason, "admin_id": admin_id}
    content_sql = ""
    lifecycle_sql = ""
    if request.action == "delete":
        if task["target_type"] in {"post", "comment", "media"}:
            lifecycle_sql = ", deleted_at = UTC_TIMESTAMP()"
        elif task["target_type"] == "paper_plane":
            lifecycle_sql = ", status = 3"
    if request.action == "replace":
        content_sql = f", {content_column} = :display_content"
        params["display_content"] = request.display_content
    await db.execute(text(f"UPDATE `{table}` SET `{status_column}` = :status, moderation_reason = :reason, moderated_by = :admin_id, moderated_at = UTC_TIMESTAMP(){lifecycle_sql}{content_sql} WHERE id = :id"), params)
    await db.execute(text("""UPDATE community_moderation_task
        SET status = :status, reason = :reason, reviewed_by = :admin_id, reviewed_at = UTC_TIMESTAMP(),
            display_content = COALESCE(:display_content, display_content)
        WHERE id = :task_id"""), {**params, "task_id": task_id, "display_content": request.display_content})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, before_json, after_json, reason)
        VALUES (:admin_id, 'moderation_review', :target_type, :target_id, :before_json, :after_json, :reason)"""), {
        "admin_id": admin_id, "target_type": task["target_type"], "target_id": task["target_id"],
        "before_json": json.dumps({"status": "pending"}), "after_json": json.dumps({"status": action_status}), "reason": request.reason,
    })
    await emit_notification(
        db,
        recipient_user_id=int(task["user_id"]),
        actor_user_id=None,
        event_type="community_moderation_result",
        title="内容审核结果",
        content=f"你的内容审核结果：{action_status}",
        target_type=task["target_type"],
        target_id=int(task["target_id"]),
        payload={"status": action_status},
    )
    await db.commit()
    return ModerationReviewResponse(id=task_id, target_type=task["target_type"], target_id=int(task["target_id"]), status=action_status, reason=request.reason)


async def grant_admin(db: AsyncSession, request: AdminGrantRequest, *, granted_by: int) -> AdminGrantResponse:
    user = await db.execute(text("SELECT id FROM users WHERE id = :id AND status = 1"), {"id": request.user_id})
    if not user.scalar():
        raise HTTPException(404, detail="用户不存在或不可用")
    await db.execute(text("""INSERT INTO user_role (user_id, role_code, status, granted_by)
        VALUES (:user_id, 'admin', 1, :granted_by)
        ON DUPLICATE KEY UPDATE status = 1, granted_by = VALUES(granted_by), granted_at = UTC_TIMESTAMP(), revoked_at = NULL"""), {"user_id": request.user_id, "granted_by": granted_by})
    await db.execute(text("DELETE FROM admin_permission WHERE user_id = :user_id"), {"user_id": request.user_id})
    for permission in sorted(set(request.permissions)):
        await db.execute(text("INSERT INTO admin_permission (user_id, permission_code, granted_by) VALUES (:user_id, :permission, :granted_by)"), {"user_id": request.user_id, "permission": permission, "granted_by": granted_by})
    await db.commit()
    return AdminGrantResponse(user_id=request.user_id, role_code="admin", permissions=sorted(set(request.permissions)))
