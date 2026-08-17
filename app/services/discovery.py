"""Recommendation, card browsing and interaction services."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException, Response
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.profile_tags import TAG_OPTIONS_BY_CATEGORY
from app.core.redis import consume_daily, get_daily_used, refund_daily, redis_client
from app.schemas.discovery import (
    ApplicationCreateRequest,
    ApplicationRejectRequest,
    ApplicationResponse,
    BrowseHistoryItem,
    BrowseHistoryPage,
    DiscoveryCard,
    DiscoveryFilters,
    DiscoveryPage,
    DiscoverySearch,
    FavoriteResponse,
    FilterOptionsResponse,
    ApplicationPage,
    FavoritePage,
    FavoriteReceivedItem,
    FavoriteReceivedPage,
    RelationUserSummary,
    PublicProfileResponse,
    SavedFilterResponse,
    SuperLikeResponse,
    SuperLikeItem,
    SuperLikePage,
    VisitorPage,
)
from app.services.notifications import emit_notification
from app.services.profile import _calculate_age, _json_dict, _json_list, get_profile
from app.services.quotas import consume_extra
from app.services.restrictions import ensure_user_allowed

logger = logging.getLogger(__name__)


CARD_SELECT = """
    SELECT u.id AS user_id, u.nickname, u.avatar, u.gender, u.birthday, u.is_married,
           u.is_single_pledge, p.height, p.education_level, p.occupation, p.income,
           p.residence_province_code, p.residence_city_code, p.residence_district_code,
           (p.residence_city_code IS NOT NULL AND p.residence_city_code =
             (SELECT vp2.residence_city_code FROM user_profile vp2 WHERE vp2.user_id = :viewer_id)) AS same_city,
           p.mbti, p.interest_tags, p.personality_tags, p.tags,
           CASE WHEN p.online_status = 2 THEN 2
                WHEN p.last_active_at IS NOT NULL AND p.last_active_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 90 SECOND) THEN 1
                ELSE 0 END AS online_status,
           p.last_active_at, COALESCE(c.score, 0) AS completion_score,
           COALESCE(ua.realname_status, 0) AS realname_status,
           COALESCE(pr.only_vip_can_see_detail, 0) AS only_vip_can_see_detail,
           COALESCE(pr.hide_school, 0) AS hide_school,
           COALESCE(pr.hide_company, 0) AS hide_company,
           COALESCE(pr.hide_distance, 0) AS hide_distance,
           COALESCE(pr.hide_online_status, 0) AS hide_online_status,
           COALESCE(pr.show_profile, 1) AS show_profile,
           COALESCE(pr.who_can_see_me, 1) AS who_can_see_me,
           COALESCE(pr.match_status, 1) AS match_status,
           EXISTS (SELECT 1 FROM user_membership m
                   WHERE m.user_id = u.id AND m.status = 1
                     AND (m.start_at IS NULL OR m.start_at <= UTC_TIMESTAMP())
                     AND (m.end_at IS NULL OR m.end_at > UTC_TIMESTAMP())) AS is_vip,
           EXISTS (SELECT 1 FROM user_boost b
                   WHERE b.target_user_id = u.id AND b.status = 1
                     AND (b.start_at IS NULL OR b.start_at <= UTC_TIMESTAMP())
                     AND (b.end_at IS NULL OR b.end_at > UTC_TIMESTAMP())) AS is_boosted,
           EXISTS (SELECT 1 FROM user_favorite f
                   WHERE f.user_id = :viewer_id AND f.target_user_id = u.id AND f.type = 2) AS is_favorite
    FROM users u
    LEFT JOIN user_profile p ON p.user_id = u.id
    LEFT JOIN user_profile_completion c ON c.user_id = u.id
    LEFT JOIN user_auth ua ON ua.user_id = u.id
    LEFT JOIN user_privacy pr ON pr.user_id = u.id
    LEFT JOIN user_partner_preference vp ON vp.user_id = :viewer_id
"""


async def _viewer_context(db: AsyncSession, user_id: int) -> dict[str, Any]:
    result = await db.execute(
        text("""SELECT u.gender, u.birthday, COALESCE(c.score, 0) AS completion_score,
                      u.phone, p.height, p.education_level, p.income, p.mbti, p.interest_tags,
               p.personality_tags, p.tags, p.residence_city_code,
               COALESCE(ua.realname_status, 0) AS realname_status,
                      pref.age_min, pref.age_max, pref.height_min, pref.height_max,
                      pref.education_min, pref.income_min, pref.marriage_status,
                      pref.preferred_city_codes
               FROM users u LEFT JOIN user_profile p ON p.user_id = u.id
               LEFT JOIN user_profile_completion c ON c.user_id = u.id
               LEFT JOIN user_auth ua ON ua.user_id = u.id
               LEFT JOIN user_partner_preference pref ON pref.user_id = u.id
               WHERE u.id = :user_id"""),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="用户不存在")
    return dict(row)


async def _is_vip(db: AsyncSession, user_id: int) -> bool:
    result = await db.execute(
        text("""SELECT EXISTS (SELECT 1 FROM user_membership
                   WHERE user_id = :user_id AND status = 1
                     AND (start_at IS NULL OR start_at <= UTC_TIMESTAMP())
                     AND (end_at IS NULL OR end_at > UTC_TIMESTAMP()))"""),
        {"user_id": user_id},
    )
    return bool(result.scalar())


def _json_city_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []


def _all_tags(row: dict[str, Any]) -> set[str]:
    tags = set(_json_list(row.get("interest_tags"))) | set(_json_list(row.get("personality_tags")))
    for values in _json_dict(row.get("tags")).values():
        tags.update(values)
    return tags


def _candidate_score(viewer: dict[str, Any], candidate: dict[str, Any]) -> tuple[float, str]:
    reasons: list[str] = []
    points = 0.0
    candidate_age = _calculate_age(candidate["birthday"]) if candidate.get("birthday") else None
    viewer_age = _calculate_age(viewer["birthday"]) if viewer.get("birthday") else None
    if viewer.get("age_min") and candidate_age and viewer["age_min"] <= candidate_age <= (viewer.get("age_max") or 100):
        points += 30
        reasons.append("符合年龄偏好")
    elif candidate_age and viewer_age:
        age_gap = abs(candidate_age - viewer_age)
        if age_gap <= 3:
            points += 30
            reasons.append("年龄相仿")
        elif age_gap <= 6:
            points += 20
            reasons.append("年龄较为接近")
        elif age_gap <= 10:
            points += 10
    if viewer.get("residence_city_code") and viewer["residence_city_code"] == candidate.get("residence_city_code"):
        points += 15
        reasons.append("同城")
    viewer_tags = _all_tags(viewer)
    candidate_tags = _all_tags(candidate)
    overlap = viewer_tags & candidate_tags
    if overlap:
        points += min(35, len(overlap) * 7)
        reasons.append("共同兴趣：" + "、".join(sorted(overlap)[:3]))
    viewer_mbti = viewer.get("mbti")
    candidate_mbti = candidate.get("mbti")
    if viewer_mbti and candidate_mbti:
        complementary = sum(left != right for left, right in zip(viewer_mbti[:3], candidate_mbti[:3]))
        points += complementary * 3
        if complementary >= 2:
            reasons.append("MBTI互补")
    if candidate.get("last_active_at") and (datetime.now(UTC).replace(tzinfo=None) - candidate["last_active_at"]) <= timedelta(days=7):
        points += 10
        reasons.append("近期活跃")
    if candidate.get("realname_status") == 2:
        points += 5
        reasons.append("已实名认证")
    if candidate.get("is_single_pledge") == 1:
        points += 5
        reasons.append("已签署单身承诺")
    return round(min(100.0, points), 2), "、".join(reasons[:3]) or "资料匹配"


def _card(row: dict[str, Any], score: float, reason: str, detail_locked: bool = False) -> DiscoveryCard:
    certification_tags: list[str] = []
    if row.get("realname_status") == 2:
        certification_tags.append("实名认证")
    if row.get("is_single_pledge") == 1:
        certification_tags.append("单身承诺")
    return DiscoveryCard(
        user_id=int(row["user_id"]),
        nickname=row.get("nickname"),
        avatar=row.get("avatar"),
        age=_calculate_age(row["birthday"]) if row.get("birthday") else None,
        height=row.get("height") if not detail_locked else None,
        education_level=row.get("education_level") if not detail_locked and not row.get("hide_school") else None,
        occupation=row.get("occupation") if not detail_locked and not row.get("hide_company") else None,
        city_code=row.get("residence_city_code") if not detail_locked else None,
        income=float(row["income"]) if row.get("income") is not None and not detail_locked else None,
        distance_km=(0.0 if row.get("same_city") else None) if not detail_locked and not row.get("hide_distance") else None,
        is_married=row.get("is_married") if not detail_locked else None,
        online_status=0 if row.get("hide_online_status") else int(row.get("online_status") or 0),
        mbti=row.get("mbti") if not detail_locked else None,
        interest_tags=_json_list(row.get("interest_tags"))[:5] if not detail_locked else [],
        certification_tags=certification_tags,
        match_score=score,
        match_reason=reason,
        is_favorite=bool(row.get("is_favorite")),
        is_vip=bool(row.get("is_vip")),
        is_pure_free=not bool(row.get("is_vip")) and not bool(row.get("is_boosted")),
        is_boosted=bool(row.get("is_boosted")),
        detail_locked=detail_locked,
    )


def _escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _filter_sql(filters: DiscoveryFilters, params: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    if filters.gender:
        clauses.append("u.gender = :filter_gender")
        params["filter_gender"] = filters.gender
    if filters.age_min:
        clauses.append(f"u.birthday <= DATE_SUB(CURDATE(), INTERVAL {filters.age_min} YEAR)")
    if filters.age_max:
        clauses.append(f"u.birthday >= DATE_SUB(CURDATE(), INTERVAL {filters.age_max + 1} YEAR)")
    for field, column in (("province_code", "p.residence_province_code"), ("city_code", "p.residence_city_code"), ("district_code", "p.residence_district_code")):
        value = getattr(filters, field)
        if value:
            clauses.append(f"{column} = :filter_{field}")
            params[f"filter_{field}"] = value
    if filters.marriage_status:
        clauses.append("u.is_married = :filter_marriage")
        params["filter_marriage"] = filters.marriage_status
    if filters.education_min:
        clauses.append("p.education_level >= :filter_education")
        params["filter_education"] = filters.education_min
    if filters.height_min:
        clauses.append("p.height >= :filter_height_min")
        params["filter_height_min"] = filters.height_min
    if filters.height_max:
        clauses.append("p.height <= :filter_height_max")
        params["filter_height_max"] = filters.height_max
    if filters.income_min is not None:
        clauses.append("p.income >= :filter_income_min")
        params["filter_income_min"] = filters.income_min
    if filters.income_max is not None:
        clauses.append("p.income <= :filter_income_max")
        params["filter_income_max"] = filters.income_max
    if filters.pure_free:
        clauses.append("NOT EXISTS (SELECT 1 FROM user_membership m2 WHERE m2.user_id = u.id AND m2.status = 1 AND (m2.end_at IS NULL OR m2.end_at > UTC_TIMESTAMP()))")
        clauses.append("NOT EXISTS (SELECT 1 FROM user_boost b2 WHERE b2.target_user_id = u.id AND b2.status = 1 AND (b2.end_at IS NULL OR b2.end_at > UTC_TIMESTAMP()))")
    return clauses


async def _fetch_rows(
    db: AsyncSession,
    viewer_id: int,
    filters: DiscoveryFilters,
    *,
    plaza: bool,
    nickname: str | None = None,
    tag: str | None = None,
    respect_preferences: bool = True,
) -> list[dict[str, Any]]:
    viewer = await _viewer_context(db, viewer_id)
    viewer_is_vip = await _is_vip(db, viewer_id)
    params: dict[str, Any] = {
        "viewer_id": viewer_id,
        "viewer_is_vip": int(viewer_is_vip),
        "candidate_limit": min(500, filters.page * filters.page_size + 1),
    }
    clauses = [
        "u.id <> :viewer_id", "u.status = 1", "COALESCE(c.score, 0) >= 100",
        "NOT EXISTS (SELECT 1 FROM user_restriction ban WHERE ban.user_id = u.id AND ban.restriction_type = 'TOTAL_BAN' AND ban.status = 1 AND ban.starts_at <= UTC_TIMESTAMP() AND (ban.ends_at IS NULL OR ban.ends_at > UTC_TIMESTAMP()))",
        "COALESCE(pr.show_profile, 1) = 1",
        "COALESCE(pr.who_can_see_me, 1) <> 4", "COALESCE(pr.match_status, 1) = 1",
        "(:viewer_is_vip = 1 OR COALESCE(pr.who_can_see_me, 1) <> 3)",
        "NOT EXISTS (SELECT 1 FROM user_block bl WHERE (bl.user_id = :viewer_id AND bl.target_user_id = u.id) OR (bl.user_id = u.id AND bl.target_user_id = :viewer_id))",
    ]
    if nickname:
        clauses.append("u.nickname LIKE CONCAT('%', :search_nickname, '%') ESCAPE '!' ")
        params["search_nickname"] = _escape_like(nickname)
    if tag:
        clauses.append("""(
            JSON_CONTAINS(p.interest_tags, JSON_QUOTE(:search_tag))
            OR JSON_CONTAINS(p.personality_tags, JSON_QUOTE(:search_tag))
            OR JSON_SEARCH(p.tags, 'one', :search_tag) IS NOT NULL
        )""")
        params["search_tag"] = tag
    if viewer.get("gender") in (1, 2):
        clauses.append("u.gender <> :opposite_gender")
        params["opposite_gender"] = viewer["gender"]
    if respect_preferences:
        clauses.extend([
            "(vp.age_min IS NULL OR TIMESTAMPDIFF(YEAR, u.birthday, CURDATE()) >= vp.age_min)",
            "(vp.age_max IS NULL OR TIMESTAMPDIFF(YEAR, u.birthday, CURDATE()) <= vp.age_max)",
            "(vp.height_min IS NULL OR p.height >= vp.height_min)",
            "(vp.height_max IS NULL OR p.height <= vp.height_max)",
            "(vp.education_min IS NULL OR p.education_level >= vp.education_min)",
            "(vp.income_min IS NULL OR p.income >= vp.income_min)",
            "(vp.marriage_status IS NULL OR vp.marriage_status = 0 OR u.is_married = vp.marriage_status)",
            "(vp.preferred_province_code IS NULL OR p.residence_province_code = vp.preferred_province_code)",
            "(vp.preferred_city_codes IS NULL OR JSON_LENGTH(vp.preferred_city_codes) = 0 OR JSON_CONTAINS(vp.preferred_city_codes, JSON_QUOTE(p.residence_city_code)))",
        ])
    if not plaza:
        clauses.extend([
            "NOT EXISTS (SELECT 1 FROM user_swipe_record sw WHERE sw.user_id = :viewer_id AND sw.target_user_id = u.id AND sw.action = 2)",
            "NOT EXISTS (SELECT 1 FROM match_apply ma WHERE ((ma.from_user_id = :viewer_id AND ma.to_user_id = u.id) OR (ma.from_user_id = u.id AND ma.to_user_id = :viewer_id)) AND ma.status IN (0, 1))",
        ])
    clauses.extend(_filter_sql(filters, params))
    sql = CARD_SELECT + " WHERE " + " AND ".join(clauses) + " LIMIT " + str(params.pop("candidate_limit"))
    result = await db.execute(text(sql), params)
    return [dict(row) for row in result.mappings().all()]


async def get_discovery_page(db: AsyncSession, viewer_id: int, filters: DiscoveryFilters, *, plaza: bool) -> DiscoveryPage:
    viewer = await _viewer_context(db, viewer_id)
    if viewer["completion_score"] < 100:
        raise HTTPException(403, detail="请先完善资料后再进入推荐")
    rows = await _fetch_rows(db, viewer_id, filters, plaza=plaza)
    scored = [(_candidate_score(viewer, row), row) for row in rows]
    scored.sort(key=lambda item: (bool(item[1].get("is_boosted")), item[0][0], item[1].get("last_active_at") or datetime.min), reverse=True)
    start = (filters.page - 1) * filters.page_size
    selected = scored[start:start + filters.page_size]
    viewer_is_vip = await _is_vip(db, viewer_id)
    items = [
        _card(row, score, reason, detail_locked=bool(row.get("only_vip_can_see_detail")) and not viewer_is_vip)
        for (score, reason), row in selected
    ]
    return DiscoveryPage(items=items, page=filters.page, page_size=filters.page_size, total=len(scored), has_more=start + filters.page_size < len(scored))


async def search_discovery(db: AsyncSession, viewer_id: int, query: DiscoverySearch) -> DiscoveryPage:
    filters = DiscoveryFilters(page=query.page, page_size=query.page_size)
    viewer = await _viewer_context(db, viewer_id)
    if viewer["completion_score"] < 100:
        raise HTTPException(403, detail="请先完善资料后再搜索用户")
    rows = await _fetch_rows(
        db,
        viewer_id,
        filters,
        plaza=True,
        nickname=query.nickname,
        tag=query.tag,
        respect_preferences=False,
    )
    scored = [(_candidate_score(viewer, row), row) for row in rows]
    scored.sort(key=lambda item: (item[0][0], item[1].get("last_active_at") or datetime.min), reverse=True)
    start = (query.page - 1) * query.page_size
    selected = scored[start:start + query.page_size]
    viewer_is_vip = await _is_vip(db, viewer_id)
    items = [
        _card(row, score, reason, detail_locked=bool(row.get("only_vip_can_see_detail")) and not viewer_is_vip)
        for (score, reason), row in selected
    ]
    return DiscoveryPage(
        items=items,
        page=query.page,
        page_size=query.page_size,
        total=len(scored),
        has_more=start + query.page_size < len(scored),
    )


async def get_filter_options() -> FilterOptionsResponse:
    return FilterOptionsResponse(
        genders=[{"value": 1, "label": "男"}, {"value": 2, "label": "女"}],
        marriage_statuses=[{"value": 1, "label": "未婚"}, {"value": 2, "label": "离异"}, {"value": 3, "label": "丧偶"}],
        education_levels=[{"value": 1, "label": "博士"}, {"value": 2, "label": "硕士"}, {"value": 3, "label": "本科"}, {"value": 4, "label": "大专"}, {"value": 5, "label": "高中"}],
        cities=sorted(TAG_OPTIONS_BY_CATEGORY["city"]),
    )


async def get_saved_filter(db: AsyncSession, user_id: int) -> SavedFilterResponse:
    result = await db.execute(
        text("SELECT filter_json FROM user_discovery_filter WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    raw = result.scalar()
    if not raw:
        return SavedFilterResponse(filters=None)
    try:
        filters = DiscoveryFilters.model_validate(json.loads(raw) if isinstance(raw, str) else raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(500, detail="已保存的筛选条件无效") from exc
    return SavedFilterResponse(filters=filters)


async def save_filter(db: AsyncSession, user_id: int, filters: DiscoveryFilters) -> SavedFilterResponse:
    await db.execute(
        text("""INSERT INTO user_discovery_filter (user_id, filter_json)
                  VALUES (:user_id, :filter_json)
                  ON DUPLICATE KEY UPDATE filter_json = VALUES(filter_json), updated_at = UTC_TIMESTAMP()"""),
        {
            "user_id": user_id,
            "filter_json": json.dumps(filters.model_dump(mode="json"), ensure_ascii=False),
        },
    )
    await db.commit()
    return SavedFilterResponse(filters=filters)


async def _quota_key(prefix: str, user_id: int) -> str:
    # UTC 日键，与 community quotas / redis consume_daily 重置对齐
    from app.core.redis import daily_quota_key

    return daily_quota_key(f"discovery:{prefix}", user_id)


async def _quota_limit(db: AsyncSession, user_id: int, is_vip: bool) -> int:
    if not is_vip:
        return settings.browse_daily_limit
    result = await db.execute(text("SELECT p.rights FROM user_membership m LEFT JOIN config_membership_package p ON p.code=m.package_type WHERE m.user_id=:user_id AND m.status=1 AND (m.start_at IS NULL OR m.start_at<=UTC_TIMESTAMP()) AND (m.end_at IS NULL OR m.end_at>UTC_TIMESTAMP()) ORDER BY m.end_at DESC LIMIT 1"), {"user_id": user_id})
    row = result.first()
    value = row[0] if row else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    if isinstance(value, dict) and value.get("browse_daily_limit") is not None:
        return max(0, int(value["browse_daily_limit"]))
    return 20


async def _record_quota_usage(db: AsyncSession, user_id: int, quota_code: str, source: str, reason: str, target_user_id: int | None = None) -> None:
    await db.execute(text("INSERT INTO user_quota_usage (user_id,quota_code,quota_date,source,reason,target_user_id) VALUES (:user_id,:quota_code,:quota_date,:source,:reason,:target_user_id)"), {"user_id": user_id, "quota_code": quota_code, "quota_date": date.today(), "source": source, "reason": reason, "target_user_id": target_user_id})


async def _consume_browse(db: AsyncSession, user_id: int, match_score: float, is_vip: bool, target_user_id: int) -> int:
    limit = await _quota_limit(db, user_id, is_vip)
    regular_key = await _quota_key("browse", user_id)
    if await consume_daily(regular_key, limit):
        await _record_quota_usage(db, user_id, "browse", "package" if is_vip else "free", "Daily profile view", target_user_id)
        return limit - await get_daily_used(regular_key)
    if match_score > 80:
        bonus_key = await _quota_key("browse_bonus", user_id)
        if await consume_daily(bonus_key, settings.browse_high_match_bonus):
            await _record_quota_usage(db, user_id, "browse", "bonus", "High-match profile bonus", target_user_id)
            return settings.browse_high_match_bonus - await get_daily_used(bonus_key)
    if await consume_extra(db, user_id, "browse", "积分兑换资料查看次数", target_user_id):
        return 0
    raise HTTPException(429, detail="今日完整浏览额度已用完")


async def _record_browse(db: AsyncSession, viewer_id: int, target_id: int) -> None:
    anonymous = await db.execute(text("SELECT anonymous_browse_enabled FROM user_privacy WHERE user_id = :user_id"), {"user_id": viewer_id})
    if anonymous.scalar():
        return
    await db.execute(text("INSERT INTO user_browse_history (user_id, target_user_id) VALUES (:user_id, :target_id)"), {"user_id": viewer_id, "target_id": target_id})


async def view_profile(db: AsyncSession, viewer_id: int, target_id: int) -> PublicProfileResponse:
    if viewer_id == target_id:
        raise HTTPException(422, detail="不能浏览自己的名片")
    await _ensure_target(db, viewer_id, target_id)
    rows = await _target_rows(db, viewer_id, [target_id])
    row = rows.get(target_id)
    if not row:
        raise HTTPException(404, detail="用户不存在或当前不可见")
    viewer = await _viewer_context(db, viewer_id)
    score, reason = _candidate_score(viewer, row)
    vip = await _is_vip(db, viewer_id)
    if row.get("who_can_see_me") == 3 and not vip:
        raise HTTPException(404, detail="用户不存在或当前不可见")
    privacy_locked = bool(row.get("only_vip_can_see_detail")) and not vip
    quota = None if privacy_locked else await _consume_browse(db, viewer_id, score, vip, target_id)
    full = vip or (not privacy_locked and quota is not None)
    await _record_browse(db, viewer_id, target_id)
    card = _card(row, score, reason, detail_locked=not full)
    profile = await get_profile(db, target_id, public=True) if full else None
    if profile is not None:
        privacy = (await db.execute(text("SELECT hide_school, hide_company FROM user_privacy WHERE user_id = :user_id"), {"user_id": target_id})).mappings().first()
        if privacy:
            if privacy["hide_school"]:
                profile["education_level"] = None
            if privacy["hide_company"]:
                profile["occupation"] = None
                profile["industry"] = None
    await db.commit()
    return PublicProfileResponse(user_id=target_id, card=card, profile=profile, is_vip_viewer=vip, browse_quota_remaining=quota, can_apply=viewer["completion_score"] >= 100)


async def _target_rows(db: AsyncSession, viewer_id: int, target_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not target_ids:
        return {}
    placeholders = ", ".join(f":target_{index}" for index in range(len(target_ids)))
    params = {
        "viewer_id": viewer_id,
        "viewer_is_vip": int(await _is_vip(db, viewer_id)),
        **{f"target_{index}": value for index, value in enumerate(target_ids)},
    }
    result = await db.execute(
        text(CARD_SELECT + f""" WHERE u.id IN ({placeholders}) AND u.status = 1
                 AND COALESCE(pr.who_can_see_me, 1) <> 4
                 AND NOT EXISTS (SELECT 1 FROM user_restriction ban WHERE ban.user_id = u.id AND ban.restriction_type = 'TOTAL_BAN' AND ban.status = 1 AND ban.starts_at <= UTC_TIMESTAMP() AND (ban.ends_at IS NULL OR ban.ends_at > UTC_TIMESTAMP()))
                 AND COALESCE(pr.match_status, 1) = 1
                 AND (:viewer_is_vip = 1 OR COALESCE(pr.who_can_see_me, 1) <> 3)"""),
        params,
    )
    return {int(row["user_id"]): dict(row) for row in result.mappings().all()}


async def browse_history(db: AsyncSession, viewer_id: int, page: int, page_size: int) -> BrowseHistoryPage:
    vip = await _is_vip(db, viewer_id)
    offset = (page - 1) * page_size
    visibility = "" if vip else " AND h.created_at >= CURDATE()"
    visible = """ AND u.status = 1
        AND COALESCE(pr.show_profile, 1) = 1
        AND COALESCE(pr.who_can_see_me, 1) <> 4
        AND COALESCE(pr.match_status, 1) = 1
        AND (:viewer_is_vip = 1 OR COALESCE(pr.who_can_see_me, 1) <> 3)
        AND NOT EXISTS (SELECT 1 FROM user_block bl
                        WHERE (bl.user_id = :user_id AND bl.target_user_id = u.id)
                           OR (bl.user_id = u.id AND bl.target_user_id = :user_id))"""
    params = {"user_id": viewer_id, "viewer_is_vip": int(vip), "limit": page_size, "offset": offset}
    result = await db.execute(text(f"""SELECT h.target_user_id, MAX(h.created_at) AS viewed_at
        FROM user_browse_history h JOIN users u ON u.id = h.target_user_id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE h.user_id = :user_id{visibility}{visible}
        GROUP BY h.target_user_id ORDER BY viewed_at DESC LIMIT :limit OFFSET :offset"""), params)
    rows = list(result.mappings().all())
    targets = await _target_rows(db, viewer_id, [int(row["target_user_id"]) for row in rows])
    viewer = await _viewer_context(db, viewer_id)
    items = []
    for row in rows:
        target = targets.get(int(row["target_user_id"]))
        if target:
            score, reason = _candidate_score(viewer, target)
            items.append(BrowseHistoryItem(target=_card(target, score, reason, detail_locked=bool(target.get("only_vip_can_see_detail")) and not vip), viewed_at=row["viewed_at"]))
    count = await db.execute(text(f"""SELECT COUNT(DISTINCT h.target_user_id)
        FROM user_browse_history h JOIN users u ON u.id = h.target_user_id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE h.user_id = :user_id{visibility}{visible}"""), {"user_id": viewer_id, "viewer_is_vip": int(vip)})
    return BrowseHistoryPage(items=items, page=page, page_size=page_size, total=int(count.scalar() or 0))


async def visitors(db: AsyncSession, viewer_id: int, page: int, page_size: int) -> VisitorPage:
    vip = await _is_vip(db, viewer_id)
    visible = """ AND u.status = 1
        AND COALESCE(pr.show_profile, 1) = 1
        AND COALESCE(pr.who_can_see_me, 1) <> 4
        AND COALESCE(pr.match_status, 1) = 1
        AND (:viewer_is_vip = 1 OR COALESCE(pr.who_can_see_me, 1) <> 3)
        AND NOT EXISTS (SELECT 1 FROM user_block bl
                        WHERE (bl.user_id = :viewer_id AND bl.target_user_id = u.id)
                           OR (bl.user_id = u.id AND bl.target_user_id = :viewer_id))"""
    count_result = await db.execute(text(f"""SELECT COUNT(DISTINCT h.user_id)
        FROM user_browse_history h JOIN users u ON u.id = h.user_id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE h.target_user_id = :viewer_id{visible}"""), {"viewer_id": viewer_id, "user_id": viewer_id, "viewer_is_vip": int(vip)})
    count = int(count_result.scalar() or 0)
    if not vip:
        return VisitorPage(can_view_details=False, count=count, items=[], page=page, page_size=page_size, has_more=False)
    result = await db.execute(text(f"""SELECT h.user_id, MAX(h.created_at) AS viewed_at
        FROM user_browse_history h JOIN users u ON u.id = h.user_id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE h.target_user_id = :viewer_id{visible}
        GROUP BY h.user_id ORDER BY viewed_at DESC LIMIT :limit OFFSET :offset"""), {"viewer_id": viewer_id, "user_id": viewer_id, "viewer_is_vip": 1, "limit": page_size, "offset": (page - 1) * page_size})
    rows = list(result.mappings().all())
    targets = await _target_rows(db, viewer_id, [int(row["user_id"]) for row in rows])
    viewer = await _viewer_context(db, viewer_id)
    items = [
        BrowseHistoryItem(
            target=_card(
                targets[int(row["user_id"])],
                *_candidate_score(viewer, targets[int(row["user_id"])]),
                detail_locked=bool(targets[int(row["user_id"])].get("only_vip_can_see_detail")) and not vip,
            ),
            viewed_at=row["viewed_at"],
        )
        for row in rows
        if int(row["user_id"]) in targets
    ]
    return VisitorPage(can_view_details=True, count=count, items=items, page=page, page_size=page_size, has_more=page * page_size < count)


async def visitor_count(db: AsyncSession, viewer_id: int, target_id: int) -> dict[str, int]:
    """Return only the deduplicated visitor count for a visible user."""
    await _ensure_target(db, viewer_id, target_id)
    result = await db.execute(
        text("SELECT COUNT(DISTINCT user_id) FROM user_browse_history WHERE target_user_id = :target_id"),
        {"target_id": target_id},
    )
    return {"user_id": target_id, "visitor_count": int(result.scalar() or 0)}


async def _ensure_target(db: AsyncSession, viewer_id: int, target_id: int) -> None:
    if viewer_id == target_id:
        raise HTTPException(422, detail="不能对自己执行此操作")
    result = await db.execute(text("""SELECT u.id, COALESCE(pr.who_can_see_me, 1) AS who_can_see_me,
                COALESCE(pr.match_status, 1) AS match_status,
                COALESCE(pr.show_profile, 1) AS show_profile
                FROM users u LEFT JOIN user_privacy pr ON pr.user_id = u.id
                WHERE u.id = :id AND u.status = 1 FOR UPDATE"""), {"id": target_id})
    row = result.mappings().first()
    if not row or row["show_profile"] != 1 or row["who_can_see_me"] == 4 or row["match_status"] != 1:
        raise HTTPException(404, detail="目标用户不存在")
    if row["who_can_see_me"] == 3 and not await _is_vip(db, viewer_id):
        raise HTTPException(404, detail="目标用户不存在")
    blocked = await db.execute(text("SELECT 1 FROM user_block WHERE (user_id = :viewer_id AND target_user_id = :target_id) OR (user_id = :target_id AND target_user_id = :viewer_id)"), {"viewer_id": viewer_id, "target_id": target_id})
    if blocked.scalar():
        raise HTTPException(403, detail="当前用户关系不可操作")


async def _lock_user_pair(db: AsyncSession, first_id: int, second_id: int) -> None:
    """Serialize writes involving the same two users without a schema migration."""
    left, right = sorted((first_id, second_id))
    await db.execute(
        text("SELECT id FROM users WHERE id IN (:left, :right) ORDER BY id FOR UPDATE"),
        {"left": left, "right": right},
    )


async def set_favorite(db: AsyncSession, viewer_id: int, target_id: int, enabled: bool) -> FavoriteResponse:
    await _ensure_target(db, viewer_id, target_id)
    if enabled:
        await db.execute(text("INSERT IGNORE INTO user_favorite (user_id, target_user_id, type) VALUES (:user_id, :target_id, 2)"), {"user_id": viewer_id, "target_id": target_id})
    else:
        await db.execute(text("DELETE FROM user_favorite WHERE user_id = :user_id AND target_user_id = :target_id AND type = 2"), {"user_id": viewer_id, "target_id": target_id})
    await db.commit()
    return FavoriteResponse(target_user_id=target_id, is_favorite=enabled)


async def list_favorites(db: AsyncSession, viewer_id: int, page: int, page_size: int) -> FavoritePage:
    vip = await _is_vip(db, viewer_id)
    visible = """ AND u.status = 1
        AND COALESCE(pr.show_profile, 1) = 1
        AND COALESCE(pr.who_can_see_me, 1) <> 4
        AND COALESCE(pr.match_status, 1) = 1
        AND (:viewer_is_vip = 1 OR COALESCE(pr.who_can_see_me, 1) <> 3)
        AND NOT EXISTS (SELECT 1 FROM user_block bl
                        WHERE (bl.user_id = :user_id AND bl.target_user_id = u.id)
                           OR (bl.user_id = u.id AND bl.target_user_id = :user_id))"""
    params = {"user_id": viewer_id, "viewer_is_vip": int(vip), "limit": page_size, "offset": (page - 1) * page_size}
    total = int((await db.execute(text(f"""SELECT COUNT(*)
        FROM user_favorite f JOIN users u ON u.id = f.target_user_id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE f.user_id = :user_id AND f.type = 2{visible}"""), params)).scalar() or 0)
    result = await db.execute(text(f"""SELECT f.target_user_id
        FROM user_favorite f JOIN users u ON u.id = f.target_user_id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE f.user_id = :user_id AND f.type = 2{visible}
        ORDER BY f.created_at DESC, f.id DESC LIMIT :limit OFFSET :offset"""), params)
    targets = await _target_rows(db, viewer_id, [int(row[0]) for row in result])
    viewer = await _viewer_context(db, viewer_id)
    items = [
        _card(
            targets[target_id],
            *_candidate_score(viewer, targets[target_id]),
            detail_locked=bool(targets[target_id].get("only_vip_can_see_detail")) and not vip,
        )
        for target_id in targets
    ]
    return FavoritePage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def list_received_favorites(db: AsyncSession, viewer_id: int, page: int, page_size: int) -> FavoriteReceivedPage:
    """Return users who saved the viewer, while applying the same privacy rules as cards."""
    visible = """ AND u.status = 1
        AND COALESCE(pr.show_profile, 1) = 1
        AND COALESCE(pr.who_can_see_me, 1) <> 4
        AND COALESCE(pr.match_status, 1) = 1
        AND NOT EXISTS (SELECT 1 FROM user_block bl
                        WHERE (bl.user_id = :user_id AND bl.target_user_id = u.id)
                           OR (bl.user_id = u.id AND bl.target_user_id = :user_id))"""
    params = {"user_id": viewer_id, "limit": page_size, "offset": (page - 1) * page_size}
    total = int((await db.execute(text(f"""SELECT COUNT(*)
        FROM user_favorite f JOIN users u ON u.id = f.user_id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE f.target_user_id = :user_id AND f.type = 2{visible}"""), params)).scalar() or 0)
    result = await db.execute(text(f"""SELECT f.id, f.user_id, u.nickname, u.avatar, u.birthday,
        p.residence_city_code, f.created_at
        FROM user_favorite f JOIN users u ON u.id = f.user_id
        LEFT JOIN user_profile p ON p.user_id = u.id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE f.target_user_id = :user_id AND f.type = 2{visible}
        ORDER BY f.created_at DESC, f.id DESC LIMIT :limit OFFSET :offset"""), params)
    items = [
        FavoriteReceivedItem(
            id=int(row["id"]),
            user=RelationUserSummary(user_id=int(row["user_id"]), nickname=row["nickname"], avatar=row["avatar"], age=_calculate_age(row["birthday"]) if row["birthday"] else None, city_code=row["residence_city_code"]),
            relation="received",
            created_at=row["created_at"],
        )
        for row in result.mappings().all()
    ]
    return FavoriteReceivedPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def _notify(db: AsyncSession, user_id: int, notification_type: str, title: str, content: str, related_user_id: int, related_id: int | None = None) -> None:
    target_type = "message" if notification_type.startswith("match_application") else "user"
    await emit_notification(
        db,
        recipient_user_id=user_id,
        actor_user_id=related_user_id,
        event_type=notification_type,
        title=title,
        content=content,
        target_type=target_type,
        target_id=related_id if related_id is not None else related_user_id,
    )


async def _consume_apply_quota(db: AsyncSession, viewer_id: int, vip: bool) -> bool:
    limit = settings.apply_daily_vip_limit if vip else settings.apply_daily_free_limit
    if vip:
        result = await db.execute(text("SELECT p.rights FROM user_membership m LEFT JOIN config_membership_package p ON p.code=m.package_type WHERE m.user_id=:user_id AND m.status=1 AND (m.start_at IS NULL OR m.start_at<=UTC_TIMESTAMP()) AND (m.end_at IS NULL OR m.end_at>UTC_TIMESTAMP()) ORDER BY m.end_at DESC LIMIT 1"), {"user_id": viewer_id})
        value = result.first()
        rights = value[0] if value else None
        if isinstance(rights, str):
            try:
                rights = json.loads(rights)
            except json.JSONDecodeError:
                rights = {}
        if isinstance(rights, dict):
            limit = settings.apply_daily_free_limit + int(rights.get("apply_bonus") or 0)

    if not await consume_daily(await _quota_key("apply", viewer_id), limit):
        if await consume_extra(db, viewer_id, "apply", "积分兑换申请次数"):
            return False
        raise HTTPException(429, detail="今日认识申请次数已用完")
    return True


async def _refund_quota_after_database_failure(key: str) -> None:
    try:
        await refund_daily(key)
    except Exception:
        logger.exception("Failed to refund daily quota after database failure")


async def create_application(db: AsyncSession, viewer_id: int, target_id: int, request: ApplicationCreateRequest) -> ApplicationResponse:
    await ensure_user_allowed(db, viewer_id, "APPLICATION_RESTRICTED")
    await _lock_user_pair(db, viewer_id, target_id)
    await _ensure_target(db, viewer_id, target_id)
    await _expire_pending_applications(db)
    viewer = await _viewer_context(db, viewer_id)
    if viewer["completion_score"] < 100:
        raise HTTPException(403, detail="请先完善资料后再申请认识")
    if not viewer.get("phone"):
        raise HTTPException(403, detail="请先绑定手机号")
    if viewer.get("realname_status") != 2:
        raise HTTPException(403, detail="请先完成实名认证")
    existing = await db.execute(text("SELECT id, status FROM match_apply WHERE ((from_user_id = :from_id AND to_user_id = :to_id) OR (from_user_id = :to_id AND to_user_id = :from_id)) AND status IN (0, 1) LIMIT 1"), {"from_id": viewer_id, "to_id": target_id})
    if existing.first():
        raise HTTPException(409, detail="双方已有进行中的认识申请或匹配")
    quota_key = await _quota_key("apply", viewer_id)
    vip = await _is_vip(db, viewer_id)
    if await _consume_apply_quota(db, viewer_id, vip):
        await _record_quota_usage(db, viewer_id, "apply", "package" if vip else "free", "Daily application quota", target_id)
    expire_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=48)
    try:
        result = await db.execute(text("INSERT INTO match_apply (from_user_id, to_user_id, message, status, expire_at) VALUES (:from_id, :to_id, :message, 0, :expire_at)"), {"from_id": viewer_id, "to_id": target_id, "message": request.message, "expire_at": expire_at})
        await db.execute(text("INSERT IGNORE INTO user_swipe_record (user_id, target_user_id, action, scene) VALUES (:user_id, :target_id, 3, 'recommend')"), {"user_id": viewer_id, "target_id": target_id})
        await _notify(db, target_id, "match_application", "收到新的认识申请", request.message or "有人申请认识你", viewer_id, result.lastrowid)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Failed to roll back application create transaction")
        await _refund_quota_after_database_failure(quota_key)
        raise
    created = await db.execute(text("SELECT id, from_user_id, to_user_id, message, status, expire_at, created_at FROM match_apply WHERE id = :id"), {"id": result.lastrowid})
    return ApplicationResponse(**created.mappings().one())


async def list_applications(db: AsyncSession, viewer_id: int, incoming: bool, page: int, page_size: int) -> ApplicationPage:
    await _expire_pending_applications(db)
    field = "to_user_id" if incoming else "from_user_id"
    params = {"user_id": viewer_id, "limit": page_size, "offset": (page - 1) * page_size}
    total = int((await db.execute(text(f"SELECT COUNT(*) FROM match_apply WHERE {field} = :user_id"), params)).scalar() or 0)
    result = await db.execute(text(f"""SELECT a.id, a.from_user_id, a.to_user_id, a.message, a.status, a.expire_at, a.created_at,
        fu.nickname AS from_nickname, fu.avatar AS from_avatar, fu.birthday AS from_birthday, fp.residence_city_code AS from_city_code,
        tu.nickname AS to_nickname, tu.avatar AS to_avatar, tu.birthday AS to_birthday, tp.residence_city_code AS to_city_code
        FROM match_apply a
        JOIN users fu ON fu.id = a.from_user_id LEFT JOIN user_profile fp ON fp.user_id = fu.id
        JOIN users tu ON tu.id = a.to_user_id LEFT JOIN user_profile tp ON tp.user_id = tu.id
        WHERE a.{field} = :user_id ORDER BY a.created_at DESC, a.id DESC LIMIT :limit OFFSET :offset"""), params)
    items = []
    for row in result.mappings().all():
        data = dict(row)
        data["from_user"] = RelationUserSummary(user_id=data.pop("from_user_id"), nickname=data.pop("from_nickname"), avatar=data.pop("from_avatar"), age=_calculate_age(data.pop("from_birthday")) if data.get("from_birthday") else None, city_code=data.pop("from_city_code"))
        data["to_user"] = RelationUserSummary(user_id=data.pop("to_user_id"), nickname=data.pop("to_nickname"), avatar=data.pop("to_avatar"), age=_calculate_age(data.pop("to_birthday")) if data.get("to_birthday") else None, city_code=data.pop("to_city_code"))
        items.append(ApplicationResponse(**data))
    return ApplicationPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def respond_application(db: AsyncSession, viewer_id: int, application_id: int, accepted: bool, request: ApplicationRejectRequest | None = None) -> ApplicationResponse:
    await _expire_pending_applications(db)
    result = await db.execute(text("SELECT id, from_user_id, to_user_id, message, status, expire_at, created_at FROM match_apply WHERE id = :id AND to_user_id = :user_id FOR UPDATE"), {"id": application_id, "user_id": viewer_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="认识申请不存在")
    if row["status"] != 0:
        raise HTTPException(409, detail="当前申请已处理")
    status = 1 if accepted else 2
    await db.execute(text("UPDATE match_apply SET status = :status, responded_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"status": status, "id": application_id})
    if accepted:
        for left, right in ((row["from_user_id"], row["to_user_id"]), (row["to_user_id"], row["from_user_id"])):
            await db.execute(text("INSERT INTO user_match (user_id, target_user_id, status) VALUES (:left, :right, 1) ON DUPLICATE KEY UPDATE status = 1, updated_at = UTC_TIMESTAMP()"), {"left": left, "right": right})
        first, second = sorted((row["from_user_id"], row["to_user_id"]))
        session = await db.execute(text("SELECT id FROM chat_session WHERE user1_id = :first AND user2_id = :second LIMIT 1"), {"first": first, "second": second})
        session_id = session.scalar()
        if not session_id:
            created_session = await db.execute(text("INSERT INTO chat_session (user1_id, user2_id) VALUES (:first, :second)"), {"first": first, "second": second})
            session_id = int(created_session.lastrowid)
        greeting = "你们已经互相同意认识，可以开始聊天了。"
        existing_greeting = await db.execute(text("""SELECT id FROM chat_message
            WHERE session_id = :session_id AND type = 6 AND content = :greeting LIMIT 1"""),
            {"session_id": session_id, "greeting": greeting})
        if not existing_greeting.scalar():
            await db.execute(text("""INSERT INTO chat_message
                (session_id, from_user_id, to_user_id, type, content, is_read)
                VALUES (:session_id, :from_user_id, :to_user_id, 6, :greeting, 0)"""), {
                "session_id": session_id, "from_user_id": viewer_id,
                "to_user_id": row["from_user_id"], "greeting": greeting,
            })
            unread_field = "unread_count_user1" if first == row["from_user_id"] else "unread_count_user2"
            await db.execute(text(f"""UPDATE chat_session SET last_message = :greeting,
                last_message_time = UTC_TIMESTAMP(), {unread_field} = {unread_field} + 1,
                updated_at = UTC_TIMESTAMP() WHERE id = :session_id"""),
                {"greeting": greeting, "session_id": session_id})
        await _notify(db, row["from_user_id"], "match_application_accepted", "认识申请已通过", "对方接受了你的认识申请", viewer_id, application_id)
    else:
        await _notify(db, row["from_user_id"], "match_application_rejected", "认识申请未通过", (request.reason if request else None) or "对方暂时婉拒了你的申请", viewer_id, application_id)
        created_at = row["created_at"]
        if created_at is not None and created_at.date() == datetime.now(UTC).date():
            await refund_daily(await _quota_key("apply", row["from_user_id"]))
            await db.execute(text("INSERT INTO user_quota_usage (user_id,quota_code,quota_date,source,reason,target_user_id) VALUES (:user_id,'apply',:quota_date,'refund','申请被拒绝，返还申请次数',:target_user_id)"), {"user_id": row["from_user_id"], "quota_date": date.today(), "target_user_id": viewer_id})
    await db.commit()
    updated = await db.execute(text("SELECT id, from_user_id, to_user_id, message, status, expire_at, created_at FROM match_apply WHERE id = :id"), {"id": application_id})
    return ApplicationResponse(**updated.mappings().one())


async def _expire_pending_applications(db: AsyncSession) -> None:
    await db.execute(text("""UPDATE match_apply
        SET status = 3, updated_at = UTC_TIMESTAMP()
        WHERE status = 0 AND expire_at IS NOT NULL AND expire_at <= UTC_TIMESTAMP()"""))


async def create_superlike(db: AsyncSession, viewer_id: int, target_id: int, idempotency_key: str) -> SuperLikeResponse:
    await _lock_user_pair(db, viewer_id, target_id)
    await _ensure_target(db, viewer_id, target_id)
    viewer_result = await db.execute(text("""SELECT u.phone, COALESCE(c.score, 0) AS completion_score,
        COALESCE(ua.realname_status, 0) AS realname_status
        FROM users u LEFT JOIN user_profile_completion c ON c.user_id = u.id
        LEFT JOIN user_auth ua ON ua.user_id = u.id
        WHERE u.id = :user_id FOR UPDATE"""), {"user_id": viewer_id})
    viewer = viewer_result.mappings().first()
    if not viewer or not viewer["phone"]:
        raise HTTPException(403, detail="请先绑定手机号")
    if float(viewer["completion_score"] or 0) < 100:
        raise HTTPException(403, detail="请先完善资料后再爆灯")
    if int(viewer["realname_status"] or 0) != 2:
        raise HTTPException(403, detail="请先完成实名认证后再爆灯")
    order_no = "free-superlike-" + idempotency_key
    existing = await db.execute(text("""SELECT created_at FROM user_boost
        WHERE user_id = :user_id AND target_user_id = :target_id AND order_no = :order_no
        ORDER BY id DESC LIMIT 1"""), {"user_id": viewer_id, "target_id": target_id, "order_no": order_no})
    existing_row = existing.mappings().first()
    if existing_row:
        vip = await _is_vip(db, viewer_id)
        limit = settings.superlike_daily_vip_limit if vip else settings.superlike_daily_free_limit
        used = int(await redis_client.get(await _quota_key("superlike", viewer_id)) or 0)
        return SuperLikeResponse(target_user_id=target_id, remaining_today=max(0, limit - used), created_at=existing_row["created_at"])
    vip = await _is_vip(db, viewer_id)
    limit = settings.superlike_daily_vip_limit if vip else settings.superlike_daily_free_limit
    key = await _quota_key("superlike", viewer_id)
    if not await consume_daily(key, limit):
        if await consume_extra(db, viewer_id, "superlike", "积分兑换爆灯次数", target_id):
            # The extra grant is consumed below as part of the same operation.
            pass
        else:
            raise HTTPException(429, detail="今日爆灯次数已用完")
    created_at = datetime.now(UTC).replace(tzinfo=None)
    try:
        await db.execute(text("""INSERT INTO user_boost (user_id, target_user_id, amount, order_no, start_at, end_at, status)
            VALUES (:user_id, :target_id, 0, :order_no, :start_at, :end_at, 1)"""), {
            "user_id": viewer_id, "target_id": target_id, "order_no": order_no,
            "start_at": created_at, "end_at": created_at + timedelta(days=1),
        })
        await _notify(db, target_id, "superlike", "收到爆灯", "有人对你发出了爆灯信号", viewer_id)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Failed to roll back superlike create transaction")
        await _refund_quota_after_database_failure(key)
        raise
    used = int(await redis_client.get(key) or 0)
    return SuperLikeResponse(target_user_id=target_id, remaining_today=max(0, limit - used), created_at=created_at)


async def list_superlikes(db: AsyncSession, viewer_id: int, direction: str, page: int, page_size: int) -> SuperLikePage:
    if direction not in {"sent", "received"}:
        raise ValueError("invalid superlike direction")
    owner_field = "b.user_id" if direction == "sent" else "b.target_user_id"
    other_field = "b.target_user_id" if direction == "sent" else "b.user_id"
    params = {"user_id": viewer_id, "limit": page_size, "offset": (page - 1) * page_size}
    where = f"""{owner_field} = :user_id AND u.status = 1
        AND COALESCE(pr.show_profile, 1) = 1
        AND COALESCE(pr.match_status, 1) = 1
        AND NOT EXISTS (SELECT 1 FROM user_block bl
                        WHERE (bl.user_id = :user_id AND bl.target_user_id = u.id)
                           OR (bl.user_id = u.id AND bl.target_user_id = :user_id))"""
    total = int((await db.execute(text(f"""SELECT COUNT(*) FROM user_boost b
        JOIN users u ON u.id = {other_field}
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE {where}"""), params)).scalar() or 0)
    result = await db.execute(text(f"""SELECT b.id, b.order_no, b.created_at, b.status,
        u.id AS other_id, u.nickname, u.avatar, u.birthday, p.residence_city_code,
        EXISTS (SELECT 1 FROM user_match m WHERE m.user_id = :user_id AND m.target_user_id = u.id AND m.status IN (1, 2)) AS matched
        FROM user_boost b JOIN users u ON u.id = {other_field}
        LEFT JOIN user_profile p ON p.user_id = u.id
        LEFT JOIN user_privacy pr ON pr.user_id = u.id
        WHERE {where} ORDER BY b.created_at DESC, b.id DESC LIMIT :limit OFFSET :offset"""), params)
    items = [
        SuperLikeItem(
            id=int(row["id"]),
            user=RelationUserSummary(user_id=int(row["other_id"]), nickname=row["nickname"], avatar=row["avatar"], age=_calculate_age(row["birthday"]) if row["birthday"] else None, city_code=row["residence_city_code"]),
            direction=direction,
            status=int(row["status"] or 1),
            created_at=row["created_at"],
            matched=bool(row["matched"]),
            order_no=row["order_no"],
        )
        for row in result.mappings().all()
    ]
    return SuperLikePage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def create_poster(db: AsyncSession, viewer_id: int, target_id: int, template: int) -> Response:
    await _ensure_target(db, viewer_id, target_id)
    if not settings.wechat_app_id or not settings.wechat_app_secret:
        raise HTTPException(503, detail="微信小程序码服务未配置")
    profile = await get_profile(db, target_id)
    async with httpx.AsyncClient(timeout=10) as client:
        token_response = await client.get("https://api.weixin.qq.com/cgi-bin/token", params={"grant_type": "client_credential", "appid": settings.wechat_app_id, "secret": settings.wechat_app_secret})
        token_data = token_response.json()
        if token_data.get("errcode") or not token_data.get("access_token"):
            raise HTTPException(503, detail="微信访问令牌获取失败")
        qr_response = await client.post(f"https://api.weixin.qq.com/wxa/getwxacodeunlimit?access_token={token_data['access_token']}", json={"scene": f"uid={target_id}", "page": settings.wechat_mini_program_page, "check_path": False, "env_version": "release"})
    if "image" not in qr_response.headers.get("content-type", ""):
        raise HTTPException(503, detail="微信小程序码生成失败")
    qr = Image.open(__import__("io").BytesIO(qr_response.content)).convert("RGB")
    colors = [(247, 250, 252), (255, 246, 238), (241, 248, 245), (246, 244, 252), (244, 248, 255)]
    canvas = Image.new("RGB", (750, 1100), colors[(template - 1) % len(colors)])
    draw = ImageDraw.Draw(canvas)
    draw.text((60, 70), profile.get("nickname") or "Xuanshi AI", fill=(30, 41, 59), font=ImageFont.load_default(size=40))
    draw.text((60, 135), "发现真实、认真而有趣的连接", fill=(71, 85, 105), font=ImageFont.load_default(size=24))
    qr.thumbnail((480, 480))
    canvas.paste(qr, ((750 - qr.width) // 2, 300))
    output = __import__("io").BytesIO()
    canvas.save(output, format="PNG")
    return Response(content=output.getvalue(), media_type="image/png", headers={"Content-Disposition": f"inline; filename=profile-{target_id}.png"})
