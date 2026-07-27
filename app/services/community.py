"""Community, topic, activity and paper-plane services backed by existing tables."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import consume_daily, get_daily_used, refund_daily
from app.schemas.community import (
    ActivityPage,
    ActivityResponse,
    ActivitySignupCreate,
    ActivitySignupResponse,
    CommunityBannerResponse,
    CommunityCityResponse,
    CommunityCollectResponse,
    CommunityCommentCreate,
    CommunityCommentResponse,
    CommunityPostCreate,
    CommunityPostPage,
    CommunityPostResponse,
    CommunityQuotaItem,
    CommunityQuotasResponse,
    CommunityTopicDetailResponse,
    CommunityTopicJoinResponse,
    CommunityTopicPage,
    CommunityTopicResponse,
    PaperPlaneConversationResponse,
    PaperPlaneCreate,
    PaperPlaneMessageCreate,
    PaperPlaneMessageResponse,
    PaperPlaneReplyCreate,
    PaperPlaneReplyResponse,
    PaperPlaneResponse,
)
from app.services.community_media import (
    assert_owned_media_urls,
    bind_media,
    resolve_owned_ready_media,
)
from app.services.profile import _calculate_age, _json_list

logger = logging.getLogger(__name__)


def _json_values(value: Any) -> list[str]:
    return _json_list(value)


ACTIVITY_STATUS_TEXT = {
    1: "recruiting",
    2: "full",
    3: "ongoing",
    4: "ended",
    5: "cancelled",
}

SIGNUP_STATUS_TEXT = {
    0: "pending",
    1: "joined",
    2: "cancelled",
    3: "rejected",
}

REPORT_REASONS = [
    {"id": "harass", "label": "骚扰或不适内容"},
    {"id": "fake", "label": "虚假资料或冒充"},
    {"id": "ad", "label": "广告或引流"},
    {"id": "other", "label": "其他安全问题"},
]


def _post_select_sql(extra_where: str = "") -> str:
    # school 在 user_auth，不在 user_profile（实测 Unknown column up.school）
    return f"""SELECT p.id, p.user_id, u.nickname, u.avatar, u.gender, u.birthday,
        p.content, p.images, p.video, p.location, p.visibility, p.declaration, p.topic_id, t.name AS topic_name,
        p.like_count, p.comment_count, p.created_at,
        up.mbti, ua.school, up.hometown, up.residence, COALESCE(ua.realname_status, 0) AS realname_status,
        EXISTS (SELECT 1 FROM community_like l WHERE l.user_id = :user_id AND l.target_id = p.id AND l.type = 1) AS is_liked,
        EXISTS (SELECT 1 FROM community_like c WHERE c.user_id = :user_id AND c.target_id = p.id AND c.type = 3) AS is_collected,
        (SELECT COUNT(*) FROM community_like c2 WHERE c2.target_id = p.id AND c2.type = 3) AS collect_count,
        EXISTS (SELECT 1 FROM user_favorite f WHERE f.user_id = :user_id AND f.target_user_id = p.user_id AND f.type = 3) AS is_followed
        FROM community_post p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN community_topic t ON t.id = p.topic_id
        LEFT JOIN user_profile up ON up.user_id = p.user_id
        LEFT JOIN user_auth ua ON ua.user_id = p.user_id
        LEFT JOIN user_privacy pr ON pr.user_id = p.user_id
        WHERE p.status = 1
          AND NOT EXISTS (
            SELECT 1 FROM user_block b
            WHERE (b.user_id = :user_id AND b.target_user_id = p.user_id)
               OR (b.user_id = p.user_id AND b.target_user_id = :user_id)
          )
          {extra_where}"""


def _post_visibility_clause() -> str:
    """Make every post read path apply the same audience boundary."""
    return """ AND (
        (p.visibility IN (0, 1) AND p.user_id = :user_id)
        OR (p.visibility = 2 AND p.user_id = :user_id)
        OR (p.visibility = 0 AND COALESCE(pr.show_posts, 1) = 1)
        OR (
            p.visibility = 1
            AND EXISTS (
                SELECT 1 FROM user_match outgoing
                JOIN user_match incoming
                  ON incoming.user_id = p.user_id
                 AND incoming.target_user_id = :user_id
                 AND incoming.status IN (1, 2)
                WHERE outgoing.user_id = :user_id
                  AND outgoing.target_user_id = p.user_id
                  AND outgoing.status IN (1, 2)
            )
        )
    )"""


def _post_response(row: dict[str, Any]) -> CommunityPostResponse:
    age = _calculate_age(row["birthday"]) if row.get("birthday") else None
    return CommunityPostResponse(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        nickname=row.get("nickname"),
        avatar=row.get("avatar"),
        content=row["content"] or "",
        images=_json_values(row.get("images")),
        video=row.get("video"),
        location=row.get("location"),
        visibility=int(row.get("visibility") or 0),
        declaration=row.get("declaration") or "",
        topic_id=int(row["topic_id"]) if row.get("topic_id") is not None else None,
        topic_name=row.get("topic_name"),
        like_count=int(row.get("like_count") or 0),
        comment_count=int(row.get("comment_count") or 0),
        collect_count=int(row.get("collect_count") or 0),
        is_liked=bool(row.get("is_liked")),
        is_collected=bool(row.get("is_collected")),
        is_followed=bool(row.get("is_followed")),
        gender=int(row["gender"]) if row.get("gender") is not None else None,
        age=age,
        mbti=row.get("mbti"),
        school=row.get("school"),
        hometown=row.get("hometown"),
        residence=row.get("residence"),
        realname_status=int(row.get("realname_status") or 0),
        created_at=row["created_at"],
    )


async def create_post(
    db: AsyncSession,
    user_id: int,
    request: CommunityPostCreate,
    *,
    commit: bool = True,
) -> CommunityPostResponse:
    from app.services.content_filter import assert_text_allowed

    content = (request.content or "").strip()
    if content:
        await assert_text_allowed(db, content, field="动态内容")

    image_urls: list[str] = []
    video_url: str | None = None
    bind_ids: list[int] = []

    if request.image_media_ids:
        rows = await resolve_owned_ready_media(
            db,
            user_id,
            list(request.image_media_ids),
            purpose="post",
            media_type="image",
        )
        image_urls = [str(r["file_url"]) for r in rows]
        bind_ids = [int(r["id"]) for r in rows]
    elif request.video_media_id is not None:
        rows = await resolve_owned_ready_media(
            db,
            user_id,
            [int(request.video_media_id)],
            purpose="post",
            media_type="video",
        )
        video_url = str(rows[0]["file_url"])
        bind_ids = [int(rows[0]["id"])]
    else:
        # Legacy transition: only controlled owned community_media URLs
        if request.images:
            image_rows = await assert_owned_media_urls(
                db,
                user_id,
                list(request.images),
                purpose="post",
                media_type="image",
            )
            image_urls = [str(r["file_url"]) for r in image_rows]
            bind_ids.extend(
                int(r["id"]) for r in image_rows if str(r.get("status") or "") == "ready"
            )
        if request.video:
            video_rows = await assert_owned_media_urls(
                db,
                user_id,
                [request.video],
                purpose="post",
                media_type="video",
            )
            video_url = str(video_rows[0]["file_url"])
            if str(video_rows[0].get("status") or "") == "ready":
                bind_ids.append(int(video_rows[0]["id"]))

    if request.topic_id is not None:
        topic = await db.execute(
            text("SELECT id FROM community_topic WHERE id = :topic_id AND is_active = 1"),
            {"topic_id": request.topic_id},
        )
        if not topic.scalar():
            raise HTTPException(404, detail="话题不存在")
    result = await db.execute(
        text(
            """INSERT INTO community_post
            (user_id, topic_id, content, images, video, location, visibility, declaration, status)
            VALUES (:user_id, :topic_id, :content, :images, :video, :location, :visibility, :declaration, 1)"""
        ),
        {
            "user_id": user_id,
            "topic_id": request.topic_id,
            "content": content,
            "images": json.dumps(image_urls, ensure_ascii=False),
            "video": video_url,
            "location": request.location,
            "visibility": request.visibility,
            "declaration": request.declaration,
        },
    )
    post_id = int(result.lastrowid)
    if bind_ids:
        await bind_media(
            db,
            media_ids=bind_ids,
            target_type="post",
            target_id=post_id,
        )
    if request.topic_id is not None:
        await db.execute(
            text(
                """INSERT IGNORE INTO community_topic_participant (topic_id, user_id)
                VALUES (:topic_id, :user_id)"""
            ),
            {"topic_id": request.topic_id, "user_id": user_id},
        )
    if commit:
        await db.commit()
    return await get_post(db, user_id, post_id)


async def get_post(db: AsyncSession, user_id: int, post_id: int) -> CommunityPostResponse:
    visibility = f"{_post_visibility_clause()} AND p.id = :post_id"
    result = await db.execute(
        text(_post_select_sql(visibility)),
        {"user_id": user_id, "post_id": post_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="动态不存在")
    return _post_response(dict(row))


# 社区同城常用市名 → 6 位市码（与 discovery / profile 对齐；短码 3301 规范化为 330100）
_CITY_NAME_TO_CODE: dict[str, str] = {
    "北京": "110100",
    "北京市": "110100",
    "上海": "310100",
    "上海市": "310100",
    "天津": "120100",
    "天津市": "120100",
    "重庆": "500100",
    "重庆市": "500100",
    "南京": "320100",
    "南京市": "320100",
    "杭州": "330100",
    "杭州市": "330100",
    "苏州": "320500",
    "苏州市": "320500",
    "宁波": "330200",
    "宁波市": "330200",
    "无锡": "320200",
    "无锡市": "320200",
    "合肥": "340100",
    "合肥市": "340100",
    "武汉": "420100",
    "武汉市": "420100",
    "成都": "510100",
    "成都市": "510100",
    "深圳": "440300",
    "深圳市": "440300",
    "广州": "440100",
    "广州市": "440100",
}


def normalize_city_code(raw: str | None) -> str | None:
    """统一为 6 位市码。regions 短码 4 位右补 00；已是 6 位则原样。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s == "未设置":
        return None
    if s.isdigit():
        if len(s) == 4:
            return s + "00"
        if len(s) == 6:
            return s
        if len(s) == 2:
            # 省级码不能当市码
            return None
    return s if s.isdigit() else None


def city_code_from_name(name: str | None) -> str | None:
    if name is None:
        return None
    n = str(name).strip()
    if not n or n == "未设置":
        return None
    if n in _CITY_NAME_TO_CODE:
        return _CITY_NAME_TO_CODE[n]
    if n.endswith("市") and n[:-1] in _CITY_NAME_TO_CODE:
        return _CITY_NAME_TO_CODE[n[:-1]]
    return None


def city_name_from_code(code: str | None) -> str | None:
    """6 位市码 → 标准短市名（无「市」后缀），供 location 匹配。"""
    code_norm = normalize_city_code(code)
    if not code_norm:
        return None
    for name, mapped in _CITY_NAME_TO_CODE.items():
        if mapped == code_norm and not name.endswith("市"):
            return name
    for name, mapped in _CITY_NAME_TO_CODE.items():
        if mapped == code_norm:
            return name[:-1] if name.endswith("市") else name
    return None


def _normalize_city_display_name(name: str | None) -> str:
    n = (name or "").strip()
    if not n or n == "未设置":
        return ""
    if n.endswith("市") and len(n) > 1:
        short = n[:-1]
        if short in _CITY_NAME_TO_CODE or n in _CITY_NAME_TO_CODE:
            return short
    return n


def _feed_clauses(
    mode: Literal["latest", "following", "city", "liked_users", "following_and_liked", "topic"],
    *,
    city: str | None,
    city_code: str | None,
    topic_id: int | None,
    filter_key: str | None,
    me: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    clauses: list[str] = []

    if mode == "following":
        clauses.append(
            " AND EXISTS (SELECT 1 FROM user_favorite f WHERE f.user_id = :user_id AND f.target_user_id = p.user_id AND f.type = 3)"
        )
    elif mode == "liked_users":
        clauses.append(
            " AND EXISTS (SELECT 1 FROM user_favorite f WHERE f.user_id = :user_id AND f.target_user_id = p.user_id AND f.type = 1)"
        )
    elif mode == "following_and_liked":
        # 关注·全部：关注(type=3) ∪ 用户级喜欢(type=1)，服务端去重分页
        clauses.append(
            " AND EXISTS ("
            "SELECT 1 FROM user_favorite f "
            "WHERE f.user_id = :user_id AND f.target_user_id = p.user_id AND f.type IN (1, 3)"
            ")"
        )
    elif mode == "city":
        # 同城动态：只按帖子发布地 p.location；作者现居不参与命中
        code_norm = normalize_city_code(city_code)
        city_name = _normalize_city_display_name(city)
        if code_norm is None and city_name:
            code_norm = city_code_from_name(city_name)
        if not city_name and code_norm:
            city_name = city_name_from_code(code_norm) or ""
        if not city_name:
            raise HTTPException(422, detail="同城动态需要选择或完善城市")
        params["city"] = city_name
        clauses.append(
            " AND ("
            "TRIM(COALESCE(p.location, '')) = :city "
            "OR TRIM(COALESCE(p.location, '')) LIKE CONCAT(:city, '%')"
            ")"
        )
    elif mode == "topic":
        if topic_id is None:
            raise HTTPException(422, detail="话题动态需要提供 topic_id")
        params["topic_id"] = topic_id
        clauses.append(" AND p.topic_id = :topic_id")

    if filter_key == "mbti" and me and me.get("mbti"):
        params["mbti"] = me["mbti"]
        clauses.append(" AND up.mbti = :mbti")
    elif filter_key == "alumni" and me and me.get("school"):
        params["school"] = me["school"]
        clauses.append(" AND ua.school = :school")
    elif filter_key == "hometown" and me and me.get("hometown"):
        params["hometown"] = me["hometown"]
        clauses.append(
            " AND (up.hometown = :hometown OR up.hometown LIKE CONCAT('%', :hometown, '%') OR :hometown LIKE CONCAT('%', up.hometown, '%'))"
        )

    return "".join(clauses), params


async def _viewer_profile(db: AsyncSession, user_id: int) -> dict[str, Any]:
    result = await db.execute(
        text(
            """SELECT up.mbti, ua.school, up.hometown, up.residence, up.residence_city_code,
                      up.community_city_name, up.community_city_code, up.community_city_updated_at
               FROM user_profile up
               LEFT JOIN user_auth ua ON ua.user_id = up.user_id
               WHERE up.user_id = :user_id"""
        ),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row else {}


def _resolve_city_anchor(
    *,
    city: str | None,
    city_code: str | None,
    me: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """请求（city_code 优先）→ 同城偏好（仅 viewer 锚点，不参与帖子 OR）。"""
    code_from_req = normalize_city_code(city_code)
    name_from_req = _normalize_city_display_name(city if city is not None else "")
    if code_from_req is not None:
        # Known codes keep precedence; an unknown but valid code may still be
        # paired with an explicit city name that can drive the location query.
        return city_name_from_code(code_from_req) or name_from_req or None, code_from_req
    # 显式传了「未设置」或空字面量：不回落
    if city is not None and (city or "").strip() == "未设置":
        return None, None
    if name_from_req:
        return name_from_req, city_code_from_name(name_from_req)

    me = me or {}
    pref_code = normalize_city_code(me.get("community_city_code"))
    pref_name = _normalize_city_display_name(me.get("community_city_name"))
    if pref_code or pref_name:
        code = pref_code or city_code_from_name(pref_name)
        name = pref_name or city_name_from_code(code) or None
        return name, code

    return None, None


async def list_posts(
    db: AsyncSession,
    user_id: int,
    mode: Literal[
        "latest", "following", "city", "liked_users", "following_and_liked", "topic"
    ] = "latest",
    page: int = 1,
    page_size: int = 20,
    *,
    city: str | None = None,
    city_code: str | None = None,
    topic_id: int | None = None,
    filter_key: str | None = None,
    sort: Literal["latest", "hot"] = "latest",
) -> CommunityPostPage:
    need_me = filter_key in {"mbti", "alumni", "hometown"} or mode == "city"
    me = await _viewer_profile(db, user_id) if need_me else {}
    resolved_city = city
    resolved_code = city_code
    if mode == "city":
        resolved_city, resolved_code = _resolve_city_anchor(
            city=city, city_code=city_code, me=me
        )
    extra_where, extra_params = _feed_clauses(
        mode,
        city=resolved_city,
        city_code=resolved_code,
        topic_id=topic_id,
        filter_key=filter_key,
        me=me,
    )
    extra_where = f"{_post_visibility_clause()}{extra_where}"
    order_sql = "ORDER BY p.like_count DESC, p.created_at DESC" if sort == "hot" else "ORDER BY p.is_top DESC, p.created_at DESC"
    params = {
        "user_id": user_id,
        "limit": page_size,
        "offset": (page - 1) * page_size,
        **extra_params,
    }
    result = await db.execute(text(f"{_post_select_sql(extra_where)} {order_sql} LIMIT :limit OFFSET :offset"), params)
    count_sql = text(
        f"""SELECT COUNT(*) FROM community_post p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN user_profile up ON up.user_id = p.user_id
        LEFT JOIN user_auth ua ON ua.user_id = p.user_id
        LEFT JOIN user_privacy pr ON pr.user_id = p.user_id
        WHERE p.status = 1
          AND NOT EXISTS (
            SELECT 1 FROM user_block b
            WHERE (b.user_id = :user_id AND b.target_user_id = p.user_id)
               OR (b.user_id = p.user_id AND b.target_user_id = :user_id)
          )
          {extra_where}"""
    )
    total = int((await db.execute(count_sql, {"user_id": user_id, **extra_params})).scalar() or 0)
    return CommunityPostPage(
        items=[_post_response(dict(row)) for row in result.mappings().all()],
        page=page,
        page_size=page_size,
        total=total,
    )


async def delete_post(db: AsyncSession, user_id: int, post_id: int) -> None:
    result = await db.execute(
        text(
            "UPDATE community_post SET status = 3, updated_at = UTC_TIMESTAMP() "
            "WHERE id = :post_id AND user_id = :user_id AND status = 1"
        ),
        {"post_id": post_id, "user_id": user_id},
    )
    if not result.rowcount:
        raise HTTPException(404, detail="动态不存在或无权删除")
    # Soft-lifecycle attached media: mark deleted for cleanup without clearing post URL fields
    attached = await db.execute(
        text(
            """SELECT media_id FROM community_media_attachment
            WHERE target_type = 'post' AND target_id = :post_id"""
        ),
        {"post_id": post_id},
    )
    media_ids = [int(row["media_id"]) for row in attached.mappings().all()]
    if media_ids:
        placeholders = ", ".join(f":id{i}" for i in range(len(media_ids)))
        params = {f"id{i}": mid for i, mid in enumerate(media_ids)}
        await db.execute(
            text(
                f"""UPDATE community_media
                SET status = 'deleted', deleted_at = UTC_TIMESTAMP()
                WHERE id IN ({placeholders})
                  AND deleted_at IS NULL"""
            ),
            params,
        )
    await db.commit()


async def like_post(db: AsyncSession, user_id: int, post_id: int, enabled: bool) -> CommunityPostResponse:
    await get_post(db, user_id, post_id)
    if enabled:
        await db.execute(
            text("INSERT IGNORE INTO community_like (user_id, target_id, type) VALUES (:user_id, :post_id, 1)"),
            {"user_id": user_id, "post_id": post_id},
        )
    else:
        await db.execute(
            text("DELETE FROM community_like WHERE user_id = :user_id AND target_id = :post_id AND type = 1"),
            {"user_id": user_id, "post_id": post_id},
        )
    await db.execute(
        text("UPDATE community_post SET like_count = (SELECT COUNT(*) FROM community_like WHERE target_id = :post_id AND type = 1) WHERE id = :post_id"),
        {"post_id": post_id},
    )
    await db.commit()
    return await get_post(db, user_id, post_id)


async def collect_post(db: AsyncSession, user_id: int, post_id: int, enabled: bool) -> CommunityCollectResponse:
    await get_post(db, user_id, post_id)
    if enabled:
        await db.execute(
            text("INSERT IGNORE INTO community_like (user_id, target_id, type) VALUES (:user_id, :post_id, 3)"),
            {"user_id": user_id, "post_id": post_id},
        )
    else:
        await db.execute(
            text("DELETE FROM community_like WHERE user_id = :user_id AND target_id = :post_id AND type = 3"),
            {"user_id": user_id, "post_id": post_id},
        )
    await db.commit()
    count = int(
        (
            await db.execute(
                text("SELECT COUNT(*) FROM community_like WHERE target_id = :post_id AND type = 3"),
                {"post_id": post_id},
            )
        ).scalar()
        or 0
    )
    liked = bool(
        (
            await db.execute(
                text("SELECT 1 FROM community_like WHERE user_id = :user_id AND target_id = :post_id AND type = 3"),
                {"user_id": user_id, "post_id": post_id},
            )
        ).scalar()
    )
    return CommunityCollectResponse(id=post_id, is_collected=liked, collect_count=count)


_COMMENT_SELECT = """SELECT c.id, c.post_id, c.user_id, u.nickname, u.avatar, c.parent_id,
            c.content, c.like_count, c.created_at,
            EXISTS (
              SELECT 1 FROM community_like l
              WHERE l.user_id = :user_id AND l.target_id = c.id AND l.type = 2
            ) AS is_liked
            FROM community_comment c JOIN users u ON u.id = c.user_id"""


def _comment_response(row: dict[str, Any]) -> CommunityCommentResponse:
    return CommunityCommentResponse(
        id=int(row["id"]),
        post_id=int(row["post_id"]),
        user_id=int(row["user_id"]),
        nickname=row.get("nickname"),
        avatar=row.get("avatar"),
        parent_id=row.get("parent_id"),
        content=row["content"],
        like_count=int(row.get("like_count") or 0),
        is_liked=bool(row.get("is_liked")),
        created_at=row["created_at"],
    )


async def _get_comment(
    db: AsyncSession, user_id: int, comment_id: int
) -> CommunityCommentResponse:
    result = await db.execute(
        text(_COMMENT_SELECT + " WHERE c.id = :comment_id AND c.status = 1"),
        {"user_id": user_id, "comment_id": comment_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="评论不存在")
    await get_post(db, user_id, int(row["post_id"]))
    return _comment_response(dict(row))


async def list_comments(
    db: AsyncSession, user_id: int, post_id: int, page: int, page_size: int
) -> list[CommunityCommentResponse]:
    await get_post(db, user_id, post_id)
    result = await db.execute(
        text(
            _COMMENT_SELECT
            + " WHERE c.post_id = :post_id AND c.status = 1 ORDER BY c.created_at ASC LIMIT :limit OFFSET :offset"
        ),
        {
            "user_id": user_id,
            "post_id": post_id,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        },
    )
    return [_comment_response(dict(row)) for row in result.mappings().all()]


async def create_comment(
    db: AsyncSession,
    user_id: int,
    post_id: int,
    request: CommunityCommentCreate,
    *,
    commit: bool = True,
) -> CommunityCommentResponse:
    from app.services.content_filter import assert_text_allowed

    await assert_text_allowed(db, request.content, field="评论内容")
    await get_post(db, user_id, post_id)
    if request.parent_id:
        parent = await db.execute(
            text("SELECT 1 FROM community_comment WHERE id = :parent_id AND post_id = :post_id AND status = 1"),
            {"parent_id": request.parent_id, "post_id": post_id},
        )
        if not parent.scalar():
            raise HTTPException(404, detail="父评论不存在")
    result = await db.execute(
        text(
            "INSERT INTO community_comment (post_id, user_id, parent_id, content) VALUES (:post_id, :user_id, :parent_id, :content)"
        ),
        {
            "post_id": post_id,
            "user_id": user_id,
            "parent_id": request.parent_id,
            "content": request.content,
        },
    )
    # 与 like_count 一致采用重算而非增量，避免并发/回滚下计数永久漂移
    await db.execute(
        text(
            """UPDATE community_post
            SET comment_count = (
              SELECT COUNT(*) FROM community_comment
              WHERE post_id = :post_id AND status = 1
            )
            WHERE id = :post_id"""
        ),
        {"post_id": post_id},
    )
    if commit:
        await db.commit()
    created = await db.execute(
        text(_COMMENT_SELECT + " WHERE c.id = :id"),
        {"user_id": user_id, "id": result.lastrowid},
    )
    return _comment_response(dict(created.mappings().one()))


async def like_comment(
    db: AsyncSession, user_id: int, comment_id: int, enabled: bool
) -> CommunityCommentResponse:
    await _get_comment(db, user_id, comment_id)
    if enabled:
        await db.execute(
            text(
                "INSERT IGNORE INTO community_like (user_id, target_id, type) VALUES (:user_id, :comment_id, 2)"
            ),
            {"user_id": user_id, "comment_id": comment_id},
        )
    else:
        await db.execute(
            text(
                "DELETE FROM community_like WHERE user_id = :user_id AND target_id = :comment_id AND type = 2"
            ),
            {"user_id": user_id, "comment_id": comment_id},
        )
    await db.execute(
        text(
            """UPDATE community_comment
            SET like_count = (
              SELECT COUNT(*) FROM community_like WHERE target_id = :comment_id AND type = 2
            )
            WHERE id = :comment_id"""
        ),
        {"comment_id": comment_id},
    )
    await db.commit()
    return await _get_comment(db, user_id, comment_id)


async def delete_comment(db: AsyncSession, user_id: int, comment_id: int) -> None:
    result = await db.execute(
        text("SELECT post_id FROM community_comment WHERE id = :comment_id AND user_id = :user_id AND status = 1"),
        {"comment_id": comment_id, "user_id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="评论不存在或无权删除")
    await db.execute(text("UPDATE community_comment SET status = 2 WHERE id = :comment_id"), {"comment_id": comment_id})
    # 与 create_comment 一致采用重算，避免增量式计数漂移
    await db.execute(
        text(
            """UPDATE community_post
            SET comment_count = (
              SELECT COUNT(*) FROM community_comment
              WHERE post_id = :post_id AND status = 1
            )
            WHERE id = :post_id"""
        ),
        {"post_id": row["post_id"]},
    )
    await db.commit()


def _topic_response(row: dict[str, Any]) -> CommunityTopicResponse:
    post_count = int(row.get("post_count") or 0)
    participant_count = int(row.get("participant_count") or 0)
    heat = participant_count * 10 + post_count
    return CommunityTopicResponse(
        id=int(row["id"]),
        name=row["name"],
        icon=row.get("icon"),
        sort=int(row.get("sort") or 0),
        post_count=post_count,
        participant_count=participant_count,
        heat=heat,
        joined=bool(row.get("joined")),
        created_at=row.get("created_at"),
    )


_TOPIC_SELECT = """SELECT t.id, t.name, t.icon, t.sort, t.created_at,
    (SELECT COUNT(*) FROM community_post p WHERE p.topic_id = t.id AND p.status = 1) AS post_count,
    (SELECT COUNT(*) FROM community_topic_participant tp WHERE tp.topic_id = t.id) AS participant_count,
    EXISTS (
      SELECT 1 FROM community_topic_participant tp
      WHERE tp.topic_id = t.id AND tp.user_id = :user_id
    ) AS joined
    FROM community_topic t
    WHERE t.is_active = 1"""


async def list_topics(
    db: AsyncSession,
    user_id: int,
    *,
    sort: Literal["hot", "latest"] = "hot",
    page: int = 1,
    page_size: int = 20,
    exclude_ids: list[int] | None = None,
) -> CommunityTopicPage:
    exclude_clause = ""
    params: dict[str, Any] = {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size}
    if exclude_ids:
        placeholders = []
        for index, topic_id in enumerate(exclude_ids):
            key = f"ex_{index}"
            placeholders.append(f":{key}")
            params[key] = topic_id
        exclude_clause = f" AND t.id NOT IN ({', '.join(placeholders)})"
    order_sql = (
        "ORDER BY (participant_count * 10 + post_count) DESC, t.sort DESC, t.id DESC"
        if sort == "hot"
        else "ORDER BY t.created_at DESC, t.id DESC"
    )
    result = await db.execute(
        text(f"{_TOPIC_SELECT}{exclude_clause} {order_sql} LIMIT :limit OFFSET :offset"),
        params,
    )
    count_sql = text(f"SELECT COUNT(*) FROM community_topic t WHERE t.is_active = 1{exclude_clause}")
    total = int((await db.execute(count_sql, params)).scalar() or 0)
    return CommunityTopicPage(
        items=[_topic_response(dict(row)) for row in result.mappings().all()],
        page=page,
        page_size=page_size,
        total=total,
    )


async def get_topic(db: AsyncSession, user_id: int, topic_id: int) -> CommunityTopicResponse:
    result = await db.execute(text(f"{_TOPIC_SELECT} AND t.id = :topic_id"), {"user_id": user_id, "topic_id": topic_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="话题不存在")
    return _topic_response(dict(row))


async def get_topic_detail(
    db: AsyncSession,
    user_id: int,
    topic_id: int,
    sort: Literal["hot", "latest"] = "hot",
    page: int = 1,
    page_size: int = 20,
) -> CommunityTopicDetailResponse:
    topic = await get_topic(db, user_id, topic_id)
    posts = await list_posts(
        db,
        user_id,
        mode="topic",
        page=page,
        page_size=page_size,
        topic_id=topic_id,
        sort=sort,
    )
    return CommunityTopicDetailResponse(topic=topic, posts=posts, sort=sort)


async def _topic_participant_count(db: AsyncSession, topic_id: int) -> int:
    result = await db.execute(
        text("SELECT COUNT(*) FROM community_topic_participant WHERE topic_id = :topic_id"),
        {"topic_id": topic_id},
    )
    return int(result.scalar() or 0)


async def join_topic(db: AsyncSession, user_id: int, topic_id: int) -> CommunityTopicJoinResponse:
    await get_topic(db, user_id, topic_id)
    await db.execute(
        text(
            """INSERT IGNORE INTO community_topic_participant (topic_id, user_id)
            VALUES (:topic_id, :user_id)"""
        ),
        {"topic_id": topic_id, "user_id": user_id},
    )
    await db.commit()
    count = await _topic_participant_count(db, topic_id)
    return CommunityTopicJoinResponse(
        success=True,
        joined=True,
        topic_id=topic_id,
        participant_count=count,
    )


async def leave_topic(db: AsyncSession, user_id: int, topic_id: int) -> CommunityTopicJoinResponse:
    await get_topic(db, user_id, topic_id)
    await db.execute(
        text(
            """DELETE FROM community_topic_participant
            WHERE topic_id = :topic_id AND user_id = :user_id"""
        ),
        {"topic_id": topic_id, "user_id": user_id},
    )
    await db.commit()
    count = await _topic_participant_count(db, topic_id)
    return CommunityTopicJoinResponse(
        success=True,
        joined=False,
        topic_id=topic_id,
        participant_count=count,
    )


def _activity_response(row: dict[str, Any], *, reveal_address: bool) -> ActivityResponse:
    status = int(row.get("status") or 1)
    my_status = row.get("my_status")
    my_status_int = int(my_status) if my_status is not None else None
    return ActivityResponse(
        id=int(row["id"]),
        title=row["title"],
        cover=row.get("cover"),
        type=row.get("type"),
        city=row.get("city"),
        address=row.get("address") if reveal_address else None,
        start_time=row["start_time"],
        end_time=row["end_time"],
        signup_deadline=row.get("signup_deadline"),
        max_people=int(row.get("max_people") or 0),
        current_people=int(row.get("current_people") or 0),
        price=float(row.get("price") or 0),
        status=status,
        status_text=ACTIVITY_STATUS_TEXT.get(status, "recruiting"),
        description=row.get("description"),
        my_status=my_status_int,
        my_status_text=SIGNUP_STATUS_TEXT.get(my_status_int, "none") if my_status_int is not None else "none",
        created_at=row["created_at"],
    )


_ACTIVITY_SELECT = """SELECT a.id, a.title, a.cover, a.type, a.city, a.address, a.start_time, a.end_time,
    a.signup_deadline, a.max_people, a.current_people, a.price, a.status, a.description, a.created_at,
    s.status AS my_status
    FROM offline_activity a
    LEFT JOIN activity_signup s ON s.activity_id = a.id AND s.user_id = :user_id"""


async def list_activities(
    db: AsyncSession,
    user_id: int,
    *,
    filter_key: Literal["all", "recruiting", "mine"] = "all",
    page: int = 1,
    page_size: int = 20,
) -> ActivityPage:
    clauses = [" WHERE 1 = 1"]
    if filter_key == "recruiting":
        clauses.append(" AND a.status = 1")
    elif filter_key == "mine":
        clauses.append(" AND s.status IN (0, 1)")
    where_sql = "".join(clauses)
    params = {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size}
    result = await db.execute(
        text(f"{_ACTIVITY_SELECT}{where_sql} ORDER BY a.start_time ASC, a.id DESC LIMIT :limit OFFSET :offset"),
        params,
    )
    count_sql = text(
        f"""SELECT COUNT(*) FROM offline_activity a
        LEFT JOIN activity_signup s ON s.activity_id = a.id AND s.user_id = :user_id
        {where_sql}"""
    )
    total = int((await db.execute(count_sql, {"user_id": user_id})).scalar() or 0)
    items = []
    for row in result.mappings().all():
        data = dict(row)
        reveal = data.get("my_status") == 1
        items.append(_activity_response(data, reveal_address=reveal))
    return ActivityPage(items=items, page=page, page_size=page_size, total=total)


async def get_activity(db: AsyncSession, user_id: int, activity_id: int) -> ActivityResponse:
    result = await db.execute(
        text(f"{_ACTIVITY_SELECT} WHERE a.id = :activity_id"),
        {"user_id": user_id, "activity_id": activity_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="活动不存在")
    data = dict(row)
    return _activity_response(data, reveal_address=data.get("my_status") == 1)


async def signup_activity(
    db: AsyncSession,
    user_id: int,
    activity_id: int,
    request: ActivitySignupCreate | None = None,
) -> ActivitySignupResponse:
    body = request or ActivitySignupCreate()
    async with db.begin():
        locked = await db.execute(
            text(
                """SELECT a.status, a.signup_deadline, a.max_people,
                s.status AS my_status
                FROM offline_activity a
                LEFT JOIN activity_signup s ON s.activity_id = a.id AND s.user_id = :user_id
                WHERE a.id = :activity_id
                FOR UPDATE"""
            ),
            {"activity_id": activity_id, "user_id": user_id},
        )
        activity = locked.mappings().first()
        if not activity:
            raise HTTPException(404, detail="活动不存在")
        data = dict(activity)
        status = int(data["status"])
        my_status = data.get("my_status")
        deadline = data.get("signup_deadline")
        if status not in (1, 2):
            raise HTTPException(422, detail="当前活动不可报名")
        if deadline and deadline < datetime.now(UTC).replace(tzinfo=None):
            raise HTTPException(422, detail="报名已截止")
        if my_status in (0, 1):
            current_status = int(my_status)
            return ActivitySignupResponse(
                success=True,
                activity_id=activity_id,
                my_status=current_status,
                my_status_text=SIGNUP_STATUS_TEXT[current_status],
                message="已报名，无需重复提交",
            )

        active_count = int(
            (
                await db.execute(
                    text(
                        """SELECT COUNT(*) FROM activity_signup
                        WHERE activity_id = :activity_id AND status IN (0, 1)"""
                    ),
                    {"activity_id": activity_id},
                )
            ).scalar()
            or 0
        )
        max_people = int(data.get("max_people") or 0)
        if max_people > 0 and active_count >= max_people:
            raise HTTPException(422, detail="活动名额已满")

        contact = await db.execute(
            text(
                """SELECT ua.real_name, u.phone
                FROM users u
                LEFT JOIN user_auth ua ON ua.user_id = u.id
                WHERE u.id = :user_id"""
            ),
            {"user_id": user_id},
        )
        account = contact.mappings().first()
        if not account:
            raise HTTPException(404, detail="用户不存在")
        canonical = dict(account)
        await db.execute(
            text(
                """INSERT INTO activity_signup (activity_id, user_id, real_name, phone, remark, status)
                VALUES (:activity_id, :user_id, :real_name, :phone, :remark, 0)
                ON DUPLICATE KEY UPDATE real_name = VALUES(real_name), phone = VALUES(phone),
                  remark = VALUES(remark), status = 0, cancel_reason = NULL, updated_at = UTC_TIMESTAMP()"""
            ),
            {
                "activity_id": activity_id,
                "user_id": user_id,
                "real_name": canonical.get("real_name"),
                "phone": canonical.get("phone"),
                "remark": body.remark,
            },
        )
        current_people = active_count + 1
        await db.execute(
            text(
                """UPDATE offline_activity
                SET current_people = :current_people,
                    status = CASE
                      WHEN max_people > 0 AND :current_people >= max_people THEN 2
                      WHEN status = 2 THEN 1
                      ELSE status
                    END,
                    updated_at = UTC_TIMESTAMP()
                WHERE id = :activity_id"""
            ),
            {"activity_id": activity_id, "current_people": current_people},
        )
    return ActivitySignupResponse(
        success=True,
        activity_id=activity_id,
        my_status=0,
        my_status_text="pending",
        message="报名已提交，审核通过后告知集合信息",
    )


async def list_my_activities(
    db: AsyncSession,
    user_id: int,
    *,
    filter_key: Literal["all", "pending", "joined", "ended"] = "all",
    page: int = 1,
    page_size: int = 20,
) -> ActivityPage:
    clauses = [" WHERE s.status IN (0, 1)"]
    if filter_key == "pending":
        clauses.append(" AND s.status = 0")
    elif filter_key == "joined":
        clauses.append(" AND s.status = 1")
    elif filter_key == "ended":
        clauses.append(" AND a.status = 4")
    where_sql = "".join(clauses)
    params = {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size}
    result = await db.execute(
        text(f"{_ACTIVITY_SELECT}{where_sql} ORDER BY a.start_time DESC, a.id DESC LIMIT :limit OFFSET :offset"),
        params,
    )
    count_sql = text(
        f"""SELECT COUNT(*) FROM offline_activity a
        JOIN activity_signup s ON s.activity_id = a.id AND s.user_id = :user_id
        {where_sql}"""
    )
    total = int((await db.execute(count_sql, {"user_id": user_id})).scalar() or 0)
    items = [_activity_response(dict(row), reveal_address=dict(row).get("my_status") == 1) for row in result.mappings().all()]
    return ActivityPage(items=items, page=page, page_size=page_size, total=total)


async def list_banners(db: AsyncSession, position: str = "community") -> list[CommunityBannerResponse]:
    result = await db.execute(
        text(
            """SELECT id, title, image_url, link_type, link_value, sort, position
            FROM config_banner
            WHERE is_active = 1 AND position = :position
              AND (start_at IS NULL OR start_at <= UTC_TIMESTAMP())
              AND (end_at IS NULL OR end_at >= UTC_TIMESTAMP())
            ORDER BY sort DESC, id DESC"""
        ),
        {"position": position},
    )
    return [
        CommunityBannerResponse(
            id=int(row["id"]),
            title=row.get("title"),
            image_url=row["image_url"],
            link_type=row.get("link_type"),
            link_value=row.get("link_value"),
            sort=int(row.get("sort") or 0),
            position=row.get("position") or position,
        )
        for row in result.mappings().all()
    ]


async def get_community_quotas(db: AsyncSession, user_id: int) -> CommunityQuotasResponse:
    vip = bool(
        (
            await db.execute(
                text(
                    """SELECT 1 FROM user_membership
                    WHERE user_id = :user_id AND status = 1
                      AND (start_at IS NULL OR start_at <= UTC_TIMESTAMP())
                      AND (end_at IS NULL OR end_at > UTC_TIMESTAMP())
                    LIMIT 1"""
                ),
                {"user_id": user_id},
            )
        ).scalar()
    )
    apply_total = settings.apply_daily_vip_limit if vip else settings.apply_daily_free_limit
    paper_total = 3
    # UTC 日键，与 discovery 申请扣次 / consume_daily 重置对齐
    from app.core.redis import daily_quota_key

    apply_key = daily_quota_key("discovery:apply", user_id)
    paper_key = daily_quota_key("paper-plane", user_id)
    apply_used = await get_daily_used(apply_key)
    paper_used = await get_daily_used(paper_key)
    return CommunityQuotasResponse(
        apply_daily=CommunityQuotaItem(
            total=apply_total,
            used=min(apply_used, apply_total),
            remain=max(apply_total - apply_used, 0),
            # 积分加次写路径未接前不虚标可加次
            points_available=False,
            points_cost=20,
        ),
        paper_plane_daily=CommunityQuotaItem(
            total=paper_total,
            used=min(paper_used, paper_total),
            remain=max(paper_total - paper_used, 0),
            points_available=False,
            points_cost=settings.point_cost_paper_plane_unlock or 10,
        ),
    )


async def get_current_city(db: AsyncSession, user_id: int) -> CommunityCityResponse:
    """同城浏览偏好（独立字段）；不读资料现居。"""
    result = await db.execute(
        text(
            """SELECT community_city_name, community_city_code
               FROM user_profile WHERE user_id = :user_id"""
        ),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    raw = ((row.get("community_city_name") if row else None) or "").strip()
    name = raw if raw and raw != "未设置" else "未设置"
    code = normalize_city_code(row.get("community_city_code") if row else None)
    if code is None and name != "未设置":
        code = city_code_from_name(name)
    if name == "未设置" and code is not None:
        name = city_name_from_code(code) or "未设置"
    return CommunityCityResponse(name=name, code=code)


async def set_current_city(
    db: AsyncSession,
    user_id: int,
    name: str,
    code: str | None = None,
) -> CommunityCityResponse:
    """写入同城浏览偏好；不改 residence / residence_*_code。一周内不可换城。"""
    city_name = _normalize_city_display_name(name)
    if not city_name:
        raise HTTPException(422, detail="城市名称不能为空")
    code_norm = normalize_city_code(code) or city_code_from_name(city_name)
    if code_norm and not city_name_from_code(code_norm):
        # 未知码仍允许用展示名；标准名优先
        pass
    std_from_code = city_name_from_code(code_norm) if code_norm else None
    if std_from_code:
        city_name = std_from_code

    result = await db.execute(
        text(
            """SELECT community_city_name, community_city_code, community_city_updated_at
               FROM user_profile WHERE user_id = :user_id"""
        ),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    if row:
        prev_name = _normalize_city_display_name(row.get("community_city_name"))
        prev_code = normalize_city_code(row.get("community_city_code"))
        same = prev_name == city_name and (
            (prev_code is None and code_norm is None)
            or (prev_code is not None and code_norm is not None and prev_code == code_norm)
            or (prev_code is None and code_norm is not None and city_code_from_name(prev_name) == code_norm)
            or (code_norm is None and prev_code is not None and city_code_from_name(city_name) == prev_code)
        )
        if same:
            return CommunityCityResponse(
                name=prev_name or city_name,
                code=prev_code or code_norm,
            )

        updated_at = row.get("community_city_updated_at")
        cooldown_days = int(getattr(settings, "community_city_cooldown_days", 7) or 7)
        if updated_at is not None and cooldown_days > 0 and prev_name:
            if getattr(updated_at, "tzinfo", None) is None:
                updated_at_aware = updated_at.replace(tzinfo=UTC)
            else:
                updated_at_aware = updated_at.astimezone(UTC)
            earliest = updated_at_aware + timedelta(days=cooldown_days)
            now = datetime.now(UTC)
            if now < earliest:
                next_date = earliest.date().isoformat()
                raise HTTPException(
                    429,
                    detail=f"同城城市一周内仅可更换一次，请于 {next_date} 后再试",
                )

        await db.execute(
            text(
                """UPDATE user_profile
                   SET community_city_name = :city_name,
                       community_city_code = :city_code,
                       community_city_updated_at = UTC_TIMESTAMP(),
                       updated_at = UTC_TIMESTAMP()
                   WHERE user_id = :user_id"""
            ),
            {"user_id": user_id, "city_name": city_name, "city_code": code_norm},
        )
    else:
        await db.execute(
            text(
                """INSERT INTO user_profile (user_id, community_city_name, community_city_code, community_city_updated_at)
                   VALUES (:user_id, :city_name, :city_code, UTC_TIMESTAMP())"""
            ),
            {"user_id": user_id, "city_name": city_name, "city_code": code_norm},
        )
    await db.commit()
    return CommunityCityResponse(name=city_name, code=code_norm)


def list_report_reasons() -> list[dict[str, str]]:
    return list(REPORT_REASONS)


async def _paper_response(row: dict[str, Any]) -> PaperPlaneResponse:
    duration = row.get("voice_duration_sec")
    return PaperPlaneResponse(
        id=int(row["id"]),
        content=row["content"] or "",
        images=_json_values(row.get("images")),
        city=row.get("city"),
        tags=_json_values(row.get("tags")),
        is_anonymous=bool(row["is_anonymous"]),
        reply_count=int(row.get("reply_count") or 0),
        voice_url=row.get("voice_url"),
        voice_duration_sec=int(duration) if duration is not None else None,
        created_at=row["created_at"],
    )


async def _refund_quota_after_failure(key: str) -> None:
    try:
        await refund_daily(key)
    except Exception:
        logger.exception("Failed to refund daily quota after database failure")


_PAPER_SELECT = """SELECT id, content, images, city, tags, is_anonymous,
                reply_count, voice_url, voice_duration_sec, created_at FROM paper_plane"""


async def create_paper_plane(
    db: AsyncSession,
    user_id: int,
    request: PaperPlaneCreate,
    *,
    commit: bool = True,
    quota_key: str | None = None,
) -> PaperPlaneResponse:
    from app.core.redis import daily_quota_key
    from app.services.content_filter import assert_text_allowed

    content = (request.content or "").strip()
    if content:
        await assert_text_allowed(db, content, field="纸飞机内容")

    image_urls: list[str] = []
    bind_ids: list[int] = []
    if request.image_media_ids:
        rows = await resolve_owned_ready_media(
            db,
            user_id,
            list(request.image_media_ids),
            purpose="paper_plane",
            media_type="image",
        )
        image_urls = [str(r["file_url"]) for r in rows]
        bind_ids = [int(r["id"]) for r in rows]
    elif request.images:
        image_rows = await assert_owned_media_urls(
            db,
            user_id,
            list(request.images),
            purpose="paper_plane",
            media_type="image",
        )
        image_urls = [str(r["file_url"]) for r in image_rows]
        bind_ids = [
            int(r["id"]) for r in image_rows if str(r.get("status") or "") == "ready"
        ]

    key = quota_key or daily_quota_key("paper-plane", user_id)
    if not await consume_daily(key, 3):
        raise HTTPException(429, detail="今日纸飞机次数已用完")
    committed = False
    try:
        result = await db.execute(
            text(
                """INSERT INTO paper_plane (
                    user_id, content, images, city, tags, is_anonymous,
                    voice_url, voice_duration_sec, expire_at
                ) VALUES (
                    :user_id, :content, :images, :city, :tags, :is_anonymous,
                    :voice_url, :voice_duration_sec, :expire_at
                )"""
            ),
            {
                "user_id": user_id,
                "content": content,
                "images": json.dumps(image_urls, ensure_ascii=False),
                "city": request.city,
                "tags": json.dumps(request.tags, ensure_ascii=False),
                "is_anonymous": int(request.is_anonymous),
                "voice_url": request.voice_url,
                "voice_duration_sec": request.voice_duration_sec,
                "expire_at": datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=24),
            },
        )
        plane_id = int(result.lastrowid)
        if bind_ids:
            await bind_media(
                db,
                media_ids=bind_ids,
                target_type="paper_plane",
                target_id=plane_id,
            )
        if commit:
            await db.commit()
            committed = True
        created = await db.execute(
            text(_PAPER_SELECT + " WHERE id = :id"),
            {"id": plane_id},
        )
        return await _paper_response(dict(created.mappings().one()))
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Failed to roll back paper-plane create transaction")
        if not committed:
            await _refund_quota_after_failure(key)
        raise


async def list_paper_planes(db: AsyncSession, user_id: int, page: int, page_size: int, own: bool = False) -> list[PaperPlaneResponse]:
    own_clause = (
        "p.user_id = :user_id"
        if own
        else "p.user_id <> :user_id AND NOT EXISTS (SELECT 1 FROM paper_plane_reply r WHERE r.plane_id = p.id AND r.user_id = :user_id)"
    )
    result = await db.execute(
        text(
            f"""SELECT p.id, p.content, p.images, p.city, p.tags, p.is_anonymous, p.reply_count,
            p.voice_url, p.voice_duration_sec, p.created_at
            FROM paper_plane p
            WHERE {own_clause}
              AND p.status = 1
              AND COALESCE(p.moderation_status, 1) = 1
              AND (p.expire_at IS NULL OR p.expire_at > UTC_TIMESTAMP())
            ORDER BY p.created_at DESC LIMIT :limit OFFSET :offset"""
        ),
        {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size},
    )
    return [await _paper_response(dict(row)) for row in result.mappings().all()]


def _conversation_response(row: dict[str, Any], viewer_id: int) -> PaperPlaneConversationResponse:
    owner_id = int(row["owner_id"])
    replier_id = int(row["replier_id"])
    if viewer_id == owner_id:
        unread = int(row.get("owner_unread") or 0)
        peer_label = "匿名回复者"
    else:
        unread = int(row.get("replier_unread") or 0)
        peer_label = "纸飞机主人"
    return PaperPlaneConversationResponse(
        id=int(row["id"]),
        plane_id=int(row["plane_id"]),
        owner_id=owner_id,
        replier_id=replier_id,
        status=int(row["status"]),
        last_message=row.get("last_message"),
        last_message_at=row.get("last_message_at"),
        unread_count=unread,
        plane_content=row.get("plane_content"),
        peer_label=peer_label,
        created_at=row["created_at"],
    )


async def _get_or_create_plane_conversation(
    db: AsyncSession,
    *,
    plane_id: int,
    owner_id: int,
    replier_id: int,
    first_message: str,
    reply_id: int | None = None,
) -> int:
    existing = await db.execute(
        text(
            """SELECT id FROM paper_plane_conversation
            WHERE plane_id = :plane_id AND replier_id = :replier_id LIMIT 1"""
        ),
        {"plane_id": plane_id, "replier_id": replier_id},
    )
    row = existing.mappings().first()
    if row:
        conversation_id = int(row["id"])
    else:
        created = await db.execute(
            text(
                """INSERT INTO paper_plane_conversation (
                    plane_id, owner_id, replier_id, status, last_message, last_message_at,
                    owner_unread, replier_unread
                ) VALUES (
                    :plane_id, :owner_id, :replier_id, 1, :last_message, UTC_TIMESTAMP(),
                    1, 0
                )"""
            ),
            {
                "plane_id": plane_id,
                "owner_id": owner_id,
                "replier_id": replier_id,
                "last_message": first_message[:200],
            },
        )
        conversation_id = int(created.lastrowid)
    await db.execute(
        text(
            """INSERT INTO paper_plane_message (
                conversation_id, from_user_id, content, type, reply_id
            ) VALUES (
                :conversation_id, :from_user_id, :content, 1, :reply_id
            )"""
        ),
        {
            "conversation_id": conversation_id,
            "from_user_id": replier_id,
            "content": first_message,
            "reply_id": reply_id,
        },
    )
    if row:
        await db.execute(
            text(
                """UPDATE paper_plane_conversation
                SET last_message = :last_message,
                    last_message_at = UTC_TIMESTAMP(),
                    owner_unread = owner_unread + 1,
                    status = CASE WHEN status = 2 THEN 1 ELSE status END
                WHERE id = :id"""
            ),
            {"id": conversation_id, "last_message": first_message[:200]},
        )
    return conversation_id


async def reply_paper_plane(
    db: AsyncSession,
    user_id: int,
    plane_id: int,
    request: PaperPlaneReplyCreate,
    *,
    commit: bool = True,
) -> PaperPlaneReplyResponse:
    from app.services.content_filter import assert_text_allowed

    await assert_text_allowed(db, request.content, field="纸飞机回复")
    result = await db.execute(
        text(
            """SELECT id, user_id FROM paper_plane
            WHERE id = :plane_id
              AND status = 1
              AND COALESCE(moderation_status, 1) = 1
              AND (expire_at IS NULL OR expire_at > UTC_TIMESTAMP())"""
        ),
        {"plane_id": plane_id},
    )
    plane = result.mappings().first()
    if not plane:
        raise HTTPException(404, detail="纸飞机不存在或已过期")
    if plane["user_id"] == user_id:
        raise HTTPException(422, detail="不能回复自己的纸飞机")
    result = await db.execute(
        text(
            "INSERT INTO paper_plane_reply (plane_id, user_id, content, is_anonymous) VALUES (:plane_id, :user_id, :content, :is_anonymous)"
        ),
        {
            "plane_id": plane_id,
            "user_id": user_id,
            "content": request.content,
            "is_anonymous": int(request.is_anonymous),
        },
    )
    reply_id = int(result.lastrowid)
    await db.execute(
        text(
            "UPDATE paper_plane SET reply_count = reply_count + 1, status = CASE WHEN reply_count + 1 >= 5 THEN 2 ELSE 1 END WHERE id = :plane_id"
        ),
        {"plane_id": plane_id},
    )
    conversation_id = await _get_or_create_plane_conversation(
        db,
        plane_id=plane_id,
        owner_id=int(plane["user_id"]),
        replier_id=user_id,
        first_message=request.content,
        reply_id=reply_id,
    )
    if commit:
        await db.commit()
    created = await db.execute(
        text("SELECT id, plane_id, user_id, content, is_anonymous, created_at FROM paper_plane_reply WHERE id = :id"),
        {"id": reply_id},
    )
    payload = dict(created.mappings().one())
    return PaperPlaneReplyResponse(
        id=int(payload["id"]),
        plane_id=int(payload["plane_id"]),
        user_id=int(payload["user_id"]),
        content=payload["content"],
        is_anonymous=bool(payload["is_anonymous"]),
        conversation_id=conversation_id,
        created_at=payload["created_at"],
    )


async def list_paper_plane_conversations(
    db: AsyncSession, user_id: int, page: int, page_size: int
) -> list[PaperPlaneConversationResponse]:
    result = await db.execute(
        text(
            """SELECT c.id, c.plane_id, c.owner_id, c.replier_id, c.status,
            c.last_message, c.last_message_at, c.owner_unread, c.replier_unread,
            c.created_at, p.content AS plane_content
            FROM paper_plane_conversation c
            JOIN paper_plane p ON p.id = c.plane_id
            WHERE c.owner_id = :user_id OR c.replier_id = :user_id
            ORDER BY COALESCE(c.last_message_at, c.created_at) DESC, c.id DESC
            LIMIT :limit OFFSET :offset"""
        ),
        {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size},
    )
    return [_conversation_response(dict(row), user_id) for row in result.mappings().all()]


async def _require_plane_conversation(
    db: AsyncSession, user_id: int, conversation_id: int
) -> dict[str, Any]:
    result = await db.execute(
        text(
            """SELECT id, plane_id, owner_id, replier_id, status, last_message, last_message_at,
            owner_unread, replier_unread, created_at
            FROM paper_plane_conversation WHERE id = :id"""
        ),
        {"id": conversation_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="纸飞机会话不存在")
    if user_id not in (int(row["owner_id"]), int(row["replier_id"])):
        raise HTTPException(403, detail="无权访问该纸飞机会话")
    return dict(row)


async def list_paper_plane_messages(
    db: AsyncSession, user_id: int, conversation_id: int, page: int, page_size: int
) -> list[PaperPlaneMessageResponse]:
    await _require_plane_conversation(db, user_id, conversation_id)
    result = await db.execute(
        text(
            """SELECT id, conversation_id, from_user_id, content, type, media_url,
            voice_duration_sec, created_at
            FROM paper_plane_message
            WHERE conversation_id = :conversation_id
            ORDER BY created_at DESC, id DESC
            LIMIT :limit OFFSET :offset"""
        ),
        {
            "conversation_id": conversation_id,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        },
    )
    items: list[PaperPlaneMessageResponse] = []
    for row in result.mappings().all():
        duration = row.get("voice_duration_sec")
        items.append(
            PaperPlaneMessageResponse(
                id=int(row["id"]),
                conversation_id=int(row["conversation_id"]),
                from_user_id=int(row["from_user_id"]),
                content=row["content"] or "",
                type=int(row["type"]),
                media_url=row.get("media_url"),
                voice_duration_sec=int(duration) if duration is not None else None,
                created_at=row["created_at"],
            )
        )
    return items


async def send_paper_plane_message(
    db: AsyncSession,
    user_id: int,
    conversation_id: int,
    request: PaperPlaneMessageCreate,
    *,
    commit: bool = True,
) -> PaperPlaneMessageResponse:
    from app.services.content_filter import assert_text_allowed

    # 文本消息过滤敏感词；语音消息（type=3）content 为空，无需过滤
    if request.type == 1 and (request.content or "").strip():
        await assert_text_allowed(db, request.content, field="会话消息")

    conv = await _require_plane_conversation(db, user_id, conversation_id)
    if int(conv["status"]) != 1:
        raise HTTPException(422, detail="对话已结束")
    preview = request.content if request.type == 1 else "[语音]"
    result = await db.execute(
        text(
            """INSERT INTO paper_plane_message (
                conversation_id, from_user_id, content, type, media_url, voice_duration_sec
            ) VALUES (
                :conversation_id, :from_user_id, :content, :type, :media_url, :voice_duration_sec
            )"""
        ),
        {
            "conversation_id": conversation_id,
            "from_user_id": user_id,
            "content": request.content or "",
            "type": int(request.type),
            "media_url": request.media_url,
            "voice_duration_sec": request.voice_duration_sec,
        },
    )
    if user_id == int(conv["owner_id"]):
        unread_sql = "replier_unread = replier_unread + 1"
    else:
        unread_sql = "owner_unread = owner_unread + 1"
    await db.execute(
        text(
            f"""UPDATE paper_plane_conversation
            SET last_message = :last_message, last_message_at = UTC_TIMESTAMP(), {unread_sql}
            WHERE id = :id"""
        ),
        {"id": conversation_id, "last_message": preview[:200]},
    )
    if commit:
        await db.commit()
    created = await db.execute(
        text(
            """SELECT id, conversation_id, from_user_id, content, type, media_url,
            voice_duration_sec, created_at FROM paper_plane_message WHERE id = :id"""
        ),
        {"id": result.lastrowid},
    )
    row = created.mappings().one()
    duration = row.get("voice_duration_sec")
    return PaperPlaneMessageResponse(
        id=int(row["id"]),
        conversation_id=int(row["conversation_id"]),
        from_user_id=int(row["from_user_id"]),
        content=row["content"] or "",
        type=int(row["type"]),
        media_url=row.get("media_url"),
        voice_duration_sec=int(duration) if duration is not None else None,
        created_at=row["created_at"],
    )


async def read_paper_plane_conversation(
    db: AsyncSession, user_id: int, conversation_id: int
) -> PaperPlaneConversationResponse:
    conv = await _require_plane_conversation(db, user_id, conversation_id)
    if user_id == int(conv["owner_id"]):
        field = "owner_unread = 0"
    else:
        field = "replier_unread = 0"
    await db.execute(
        text(f"UPDATE paper_plane_conversation SET {field} WHERE id = :id"),
        {"id": conversation_id},
    )
    await db.commit()
    result = await db.execute(
        text(
            """SELECT c.id, c.plane_id, c.owner_id, c.replier_id, c.status,
            c.last_message, c.last_message_at, c.owner_unread, c.replier_unread,
            c.created_at, p.content AS plane_content
            FROM paper_plane_conversation c
            JOIN paper_plane p ON p.id = c.plane_id
            WHERE c.id = :id"""
        ),
        {"id": conversation_id},
    )
    return _conversation_response(dict(result.mappings().one()), user_id)


async def end_paper_plane_conversation(
    db: AsyncSession, user_id: int, conversation_id: int
) -> PaperPlaneConversationResponse:
    await _require_plane_conversation(db, user_id, conversation_id)
    await db.execute(
        text("UPDATE paper_plane_conversation SET status = 2 WHERE id = :id"),
        {"id": conversation_id},
    )
    await db.commit()
    result = await db.execute(
        text(
            """SELECT c.id, c.plane_id, c.owner_id, c.replier_id, c.status,
            c.last_message, c.last_message_at, c.owner_unread, c.replier_unread,
            c.created_at, p.content AS plane_content
            FROM paper_plane_conversation c
            JOIN paper_plane p ON p.id = c.plane_id
            WHERE c.id = :id"""
        ),
        {"id": conversation_id},
    )
    return _conversation_response(dict(result.mappings().one()), user_id)
