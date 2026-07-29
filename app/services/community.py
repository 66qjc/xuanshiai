"""Community and paper-plane services backed by the existing tables."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import consume_daily, refund_daily
from app.core.config import settings
from app.schemas.community import (
    CommunityCommentCreate,
    CommunityCommentResponse,
    CommunityPostCreate,
    CommunityPostPage,
    CommunityPostResponse,
    PaperPlaneCreate,
    PaperPlaneReplyCreate,
    PaperPlaneReplyResponse,
    PaperPlaneResponse,
)
from app.services.profile import _json_list
from app.services.quotas import consume_extra


def _json_values(value: Any) -> list[str]:
    return _json_list(value)


def _post_response(row: dict[str, Any]) -> CommunityPostResponse:
    return CommunityPostResponse(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        nickname=row.get("nickname"),
        avatar=row.get("avatar"),
        content=row["content"],
        images=_json_values(row.get("images")),
        video=row.get("video"),
        location=row.get("location"),
        like_count=int(row.get("like_count") or 0),
        comment_count=int(row.get("comment_count") or 0),
        is_liked=bool(row.get("is_liked")),
        created_at=row["created_at"],
    )


async def create_post(db: AsyncSession, user_id: int, request: CommunityPostCreate) -> CommunityPostResponse:
    result = await db.execute(text("""INSERT INTO community_post
        (user_id, topic_id, content, images, video, location, status)
        VALUES (:user_id, :topic_id, :content, :images, :video, :location, 1)"""), {
        "user_id": user_id,
        "topic_id": request.topic_id,
        "content": request.content,
        "images": json.dumps(request.images, ensure_ascii=False),
        "video": request.video,
        "location": request.location,
    })
    await db.commit()
    return await get_post(db, user_id, int(result.lastrowid))


async def get_post(db: AsyncSession, user_id: int, post_id: int) -> CommunityPostResponse:
    result = await db.execute(text("""SELECT p.id, p.user_id, u.nickname, u.avatar, p.content, p.images,
        p.video, p.location, p.like_count, p.comment_count, p.created_at,
        EXISTS (SELECT 1 FROM community_like l WHERE l.user_id = :user_id AND l.target_id = p.id AND l.type = 1) AS is_liked
        FROM community_post p JOIN users u ON u.id = p.user_id
        LEFT JOIN user_privacy pr ON pr.user_id = p.user_id
        WHERE p.id = :post_id AND p.status = 1
          AND (p.user_id = :user_id OR COALESCE(pr.show_posts, 1) = 1)"""), {"user_id": user_id, "post_id": post_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="动态不存在")
    return _post_response(dict(row))


async def list_posts(db: AsyncSession, user_id: int, mode: Literal["latest", "following"], page: int, page_size: int) -> CommunityPostPage:
    following_clause = "" if mode == "latest" else " AND EXISTS (SELECT 1 FROM user_favorite f WHERE f.user_id = :user_id AND f.target_user_id = p.user_id AND f.type = 3)"
    visibility_clause = " AND COALESCE(pr.show_posts, 1) = 1"
    query = text(f"""SELECT p.id, p.user_id, u.nickname, u.avatar, p.content, p.images, p.video, p.location,
        p.like_count, p.comment_count, p.created_at,
        EXISTS (SELECT 1 FROM community_like l WHERE l.user_id = :user_id AND l.target_id = p.id AND l.type = 1) AS is_liked
        FROM community_post p JOIN users u ON u.id = p.user_id
        LEFT JOIN user_privacy pr ON pr.user_id = p.user_id
        WHERE p.status = 1{following_clause}{visibility_clause}
        ORDER BY p.is_top DESC, p.created_at DESC LIMIT :limit OFFSET :offset""")
    params = {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size}
    result = await db.execute(query, params)
    count_sql = text(f"SELECT COUNT(*) FROM community_post p LEFT JOIN user_privacy pr ON pr.user_id = p.user_id WHERE p.status = 1{following_clause}{visibility_clause}")
    total = int((await db.execute(count_sql, {"user_id": user_id})).scalar() or 0)
    return CommunityPostPage(items=[_post_response(dict(row)) for row in result.mappings().all()], page=page, page_size=page_size, total=total)


async def delete_post(db: AsyncSession, user_id: int, post_id: int) -> None:
    result = await db.execute(text("UPDATE community_post SET status = 3, updated_at = UTC_TIMESTAMP() WHERE id = :post_id AND user_id = :user_id AND status = 1"), {"post_id": post_id, "user_id": user_id})
    if not result.rowcount:
        raise HTTPException(404, detail="动态不存在或无权删除")
    await db.commit()


async def like_post(db: AsyncSession, user_id: int, post_id: int, enabled: bool) -> CommunityPostResponse:
    await get_post(db, user_id, post_id)
    if enabled:
        await db.execute(text("INSERT IGNORE INTO community_like (user_id, target_id, type) VALUES (:user_id, :post_id, 1)"), {"user_id": user_id, "post_id": post_id})
    else:
        await db.execute(text("DELETE FROM community_like WHERE user_id = :user_id AND target_id = :post_id AND type = 1"), {"user_id": user_id, "post_id": post_id})
    await db.execute(text("UPDATE community_post SET like_count = (SELECT COUNT(*) FROM community_like WHERE target_id = :post_id AND type = 1) WHERE id = :post_id"), {"post_id": post_id})
    await db.commit()
    return await get_post(db, user_id, post_id)


def _comment_response(row: dict[str, Any]) -> CommunityCommentResponse:
    return CommunityCommentResponse(id=int(row["id"]), post_id=int(row["post_id"]), user_id=int(row["user_id"]), nickname=row.get("nickname"), avatar=row.get("avatar"), parent_id=row.get("parent_id"), content=row["content"], like_count=int(row.get("like_count") or 0), created_at=row["created_at"])


async def list_comments(db: AsyncSession, post_id: int, page: int, page_size: int) -> list[CommunityCommentResponse]:
    result = await db.execute(text("""SELECT c.id, c.post_id, c.user_id, u.nickname, u.avatar, c.parent_id,
        c.content, c.like_count, c.created_at FROM community_comment c JOIN users u ON u.id = c.user_id
        WHERE c.post_id = :post_id AND c.status = 1 ORDER BY c.created_at ASC LIMIT :limit OFFSET :offset"""), {"post_id": post_id, "limit": page_size, "offset": (page - 1) * page_size})
    return [_comment_response(dict(row)) for row in result.mappings().all()]


async def create_comment(db: AsyncSession, user_id: int, post_id: int, request: CommunityCommentCreate) -> CommunityCommentResponse:
    await get_post(db, user_id, post_id)
    if request.parent_id:
        parent = await db.execute(text("SELECT 1 FROM community_comment WHERE id = :parent_id AND post_id = :post_id AND status = 1"), {"parent_id": request.parent_id, "post_id": post_id})
        if not parent.scalar():
            raise HTTPException(404, detail="父评论不存在")
    result = await db.execute(text("INSERT INTO community_comment (post_id, user_id, parent_id, content) VALUES (:post_id, :user_id, :parent_id, :content)"), {"post_id": post_id, "user_id": user_id, "parent_id": request.parent_id, "content": request.content})
    await db.execute(text("UPDATE community_post SET comment_count = comment_count + 1 WHERE id = :post_id"), {"post_id": post_id})
    await db.commit()
    created = await db.execute(text("""SELECT c.id, c.post_id, c.user_id, u.nickname, u.avatar, c.parent_id,
        c.content, c.like_count, c.created_at FROM community_comment c JOIN users u ON u.id = c.user_id WHERE c.id = :id"""), {"id": result.lastrowid})
    return _comment_response(dict(created.mappings().one()))


async def delete_comment(db: AsyncSession, user_id: int, comment_id: int) -> None:
    result = await db.execute(text("SELECT post_id FROM community_comment WHERE id = :comment_id AND user_id = :user_id AND status = 1"), {"comment_id": comment_id, "user_id": user_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="评论不存在或无权删除")
    await db.execute(text("UPDATE community_comment SET status = 2 WHERE id = :comment_id"), {"comment_id": comment_id})
    await db.execute(text("UPDATE community_post SET comment_count = GREATEST(comment_count - 1, 0) WHERE id = :post_id"), {"post_id": row["post_id"]})
    await db.commit()


async def _paper_response(row: dict[str, Any]) -> PaperPlaneResponse:
    return PaperPlaneResponse(id=int(row["id"]), content=row["content"], images=_json_values(row.get("images")), city=row.get("city"), tags=_json_values(row.get("tags")), is_anonymous=bool(row["is_anonymous"]), reply_count=int(row.get("reply_count") or 0), created_at=row["created_at"])


async def create_paper_plane(db: AsyncSession, user_id: int, request: PaperPlaneCreate) -> PaperPlaneResponse:
    key = f"paper-plane:{user_id}:{date.today().isoformat()}"
    used_daily = await consume_daily(key, settings.paper_plane_daily_limit)
    if not used_daily and not await consume_extra(db, user_id, "paper_plane", "积分兑换纸飞机次数"):
        raise HTTPException(429, detail="今日纸飞机次数已用完")
    try:
        if used_daily:
            await db.execute(text("INSERT INTO user_quota_usage (user_id,quota_code,quota_date,source,reason) VALUES (:user_id,'paper_plane',:quota_date,'free','每日纸飞机次数')"), {"user_id": user_id, "quota_date": date.today()})
        result = await db.execute(text("""INSERT INTO paper_plane (user_id, content, images, city, tags, is_anonymous, expire_at)
            VALUES (:user_id, :content, :images, :city, :tags, :is_anonymous, :expire_at)"""), {"user_id": user_id, "content": request.content, "images": json.dumps(request.images, ensure_ascii=False), "city": request.city, "tags": json.dumps(request.tags, ensure_ascii=False), "is_anonymous": int(request.is_anonymous), "expire_at": datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=24)})
        await db.commit()
    except Exception:
        await db.rollback()
        if used_daily:
            await refund_daily(key)
        raise
    created = await db.execute(text("SELECT id, content, images, city, tags, is_anonymous, reply_count, created_at FROM paper_plane WHERE id = :id"), {"id": result.lastrowid})
    return await _paper_response(dict(created.mappings().one()))


async def list_paper_planes(db: AsyncSession, user_id: int, page: int, page_size: int, own: bool = False) -> list[PaperPlaneResponse]:
    own_clause = "p.user_id = :user_id" if own else "p.user_id <> :user_id AND NOT EXISTS (SELECT 1 FROM paper_plane_reply r WHERE r.plane_id = p.id AND r.user_id = :user_id)"
    result = await db.execute(text(f"""SELECT p.id, p.content, p.images, p.city, p.tags, p.is_anonymous, p.reply_count, p.created_at
        FROM paper_plane p WHERE {own_clause} AND p.status = 1 AND (p.expire_at IS NULL OR p.expire_at > UTC_TIMESTAMP())
        ORDER BY p.created_at DESC LIMIT :limit OFFSET :offset"""), {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size})
    return [await _paper_response(dict(row)) for row in result.mappings().all()]


async def reply_paper_plane(db: AsyncSession, user_id: int, plane_id: int, request: PaperPlaneReplyCreate) -> PaperPlaneReplyResponse:
    result = await db.execute(text("SELECT id, user_id FROM paper_plane WHERE id = :plane_id AND status = 1 AND (expire_at IS NULL OR expire_at > UTC_TIMESTAMP())"), {"plane_id": plane_id})
    plane = result.mappings().first()
    if not plane:
        raise HTTPException(404, detail="纸飞机不存在或已过期")
    if plane["user_id"] == user_id:
        raise HTTPException(422, detail="不能回复自己的纸飞机")
    result = await db.execute(text("INSERT INTO paper_plane_reply (plane_id, user_id, content, is_anonymous) VALUES (:plane_id, :user_id, :content, :is_anonymous)"), {"plane_id": plane_id, "user_id": user_id, "content": request.content, "is_anonymous": int(request.is_anonymous)})
    await db.execute(text("UPDATE paper_plane SET reply_count = reply_count + 1, status = CASE WHEN reply_count + 1 >= 5 THEN 2 ELSE 1 END WHERE id = :plane_id"), {"plane_id": plane_id})
    await db.commit()
    created = await db.execute(text("SELECT id, plane_id, user_id, content, is_anonymous, created_at FROM paper_plane_reply WHERE id = :id"), {"id": result.lastrowid})
    return PaperPlaneReplyResponse(**created.mappings().one())
