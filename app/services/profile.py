"""Profile, media, completion and partner-preference business rules."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError
from PIL.Image import DecompressionBombError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.profile_tags import TAG_CATEGORIES
from app.schemas.admin import MediaReviewRequest, MediaReviewResponse
from app.schemas.auth import (
    CompletionItemResponse,
    CompletionResponse,
    IntroTemplateResponse,
    PhotoOrderRequest,
    PreferenceUpdateRequest,
    ProfilePreviewResponse,
    ProfileOverviewResponse,
    ProfileUpdateRequest,
    TagCategoryResponse,
    TagOptionsResponse,
)

IMAGE_MAX_BYTES = 5 * 1024 * 1024
VIDEO_MAX_BYTES = 50 * 1024 * 1024
VIDEO_MAX_SECONDS = 30
MAX_PHOTOS = 9
IMAGE_MAX_PIXELS = 25_000_000

COMPLETION_RULES: tuple[tuple[str, str, int], ...] = (
    ("gender", "性别", 7),
    ("birthday", "出生日期/年龄", 7),
    ("location", "所在地区", 7),
    ("marriage", "婚姻状况", 5),
    ("occupation", "职业", 4),
    ("education", "学历", 4),
    ("income", "收入", 4),
    ("height", "身高", 4),
    ("avatar", "头像", 15),
    ("intro", "自我介绍", 10),
    ("album", "相册", 10),
    ("interest", "兴趣标签", 5),
    ("personality", "性格标签", 3),
    ("mbti", "MBTI", 2),
    ("preference", "择偶要求", 3),
    ("realname", "实名认证", 5),
    ("single_pledge", "单身承诺", 5),
)

INTRO_TEMPLATES: tuple[dict[str, str], ...] = (
    {
        "key": "active_life",
        "title": "热爱生活的行动派",
        "content": "热爱生活，也愿意认真经营一段关系。平时喜欢运动、旅行和发现城市里的小美好。",
    },
    {
        "key": "steady_growth",
        "title": "稳定成长型",
        "content": "认真工作，也认真生活。希望遇到一个真诚、尊重彼此、愿意一起成长的人。",
    },
    {
        "key": "simple_companion",
        "title": "简单真诚的陪伴",
        "content": "性格真诚随和，期待从一次自然的聊天开始，慢慢了解彼此，建立舒服的陪伴。",
    },
)


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(item) for item in decoded] if isinstance(decoded, list) else []
    return []


def _json_dict(value: Any) -> dict[str, list[str]]:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(category): [str(item) for item in selected]
        for category, selected in value.items()
        if isinstance(selected, list)
    }


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _calculate_age(birthday: date) -> int:
    today = date.today()
    return today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))


def _media_response(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "media_type": row["media_type"],
        "file_url": row["file_url"],
        "thumbnail_url": row["thumbnail_url"],
        "sort_order": int(row["sort_order"]),
        "is_primary": bool(row["is_primary"]),
        "duration_seconds": row["duration_seconds"],
    }


async def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "wb") as output:
        await output.write(data)


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, detail=f"文件大小不能超过{limit // 1024 // 1024}MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _image_outputs(data: bytes) -> tuple[bytes, bytes]:
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG"}:
                raise HTTPException(415, detail="仅支持JPG、JPEG或PNG图片")
            if source.width * source.height > IMAGE_MAX_PIXELS:
                raise HTTPException(413, detail="图片像素不能超过2500万")
            source.verify()
        with Image.open(BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = BytesIO()
            image.save(output, format="WEBP", quality=85, method=6)
            thumbnail = image.copy()
            thumbnail.thumbnail((480, 480), Image.Resampling.LANCZOS)
            thumb_output = BytesIO()
            thumbnail.save(thumb_output, format="WEBP", quality=80, method=6)
            return output.getvalue(), thumb_output.getvalue()
    except DecompressionBombError as exc:
        raise HTTPException(413, detail="图片像素过大") from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(415, detail="图片内容无法识别") from exc


def _user_media_dir(user_id: int) -> Path:
    return Path(settings.upload_dir) / str(user_id)


def _media_url(user_id: int, filename: str) -> str:
    return f"/storage/uploads/{user_id}/{filename}"


async def _get_media(db: AsyncSession, user_id: int, media_type: str | None = None, approved_only: bool = False) -> list[dict[str, Any]]:
    query = """SELECT id, media_type, file_url, thumbnail_url, sort_order, is_primary, duration_seconds
               FROM user_media WHERE user_id = :user_id AND deleted_at IS NULL"""
    params: dict[str, Any] = {"user_id": user_id}
    if media_type:
        query += " AND media_type = :media_type"
        params["media_type"] = media_type
    if approved_only:
        query += " AND review_status = 1"
    query += " ORDER BY sort_order ASC, id ASC"
    result = await db.execute(text(query), params)
    return [_media_response(row) for row in result.mappings().all()]


async def get_profile(db: AsyncSession, user_id: int, public: bool = False) -> dict[str, Any]:
    result = await db.execute(
        text("""SELECT u.id AS user_id, u.nickname, u.gender, u.birthday, u.is_married, u.avatar,
                      p.height, p.weight, p.occupation, p.industry, p.education_level, p.income,
                      p.hometown_province_code, p.hometown_city_code, p.hometown_district_code,
                      p.residence_province_code, p.residence_city_code, p.residence_district_code,
                      p.self_intro, p.interest_tags, p.personality_tags, p.mbti, p.tags,
                      COALESCE(c.score, 0) AS completion_score,
                      COALESCE(pr.hide_school, 0) AS hide_school,
                      COALESCE(pr.hide_company, 0) AS hide_company,
                      COALESCE(pr.only_vip_can_see_detail, 0) AS only_vip_can_see_detail
               FROM users u LEFT JOIN user_profile p ON p.user_id = u.id
               LEFT JOIN user_profile_completion c ON c.user_id = u.id
               LEFT JOIN user_privacy pr ON pr.user_id = u.id
               WHERE u.id = :id"""),
        {"id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="用户不存在")
    media = await _get_media(db, user_id, approved_only=public)
    photos = [item for item in media if item["media_type"] == "photo"]
    videos = [item for item in media if item["media_type"] == "video"]
    backgrounds = [item for item in media if item["media_type"] == "background"]
    data = dict(row)
    data["age"] = _calculate_age(data["birthday"]) if data["birthday"] else None
    data["interest_tags"] = _json_list(data["interest_tags"])
    data["personality_tags"] = _json_list(data["personality_tags"])
    data["tag_selections"] = _json_dict(data["tags"])
    data["photos"] = photos
    data["video"] = videos[0] if videos else None
    data["background_wall"] = backgrounds[0]["file_url"] if backgrounds else None
    if public:
        # Public profile responses must not expose exact location or income by default.
        for field in ("income", "hometown_province_code", "hometown_city_code", "hometown_district_code", "residence_province_code", "residence_city_code", "residence_district_code"):
            data[field] = None
        if data["hide_school"]:
            data["education_level"] = None
        if data["hide_company"]:
            data["occupation"] = None
            data["industry"] = None
        if data["only_vip_can_see_detail"]:
            for field in ("height", "occupation", "industry", "education_level", "is_married", "mbti"):
                data[field] = None
            data["interest_tags"] = []
            data["personality_tags"] = []
            data["tag_selections"] = {}
    return data


async def update_profile(db: AsyncSession, user_id: int, request: ProfileUpdateRequest) -> dict[str, Any]:
    values = request.model_dump(exclude_unset=True)
    if "birthday" in values and values["birthday"] and _calculate_age(values["birthday"]) < 18:
        raise HTTPException(422, detail="用户必须年满18周岁")
    if "gender" in values:
        result = await db.execute(text("SELECT gender FROM users WHERE id = :id FOR UPDATE"), {"id": user_id})
        old_gender = result.scalar()
        if old_gender is not None and old_gender != values["gender"]:
            raise HTTPException(409, detail="性别提交后不可自行修改")

    user_fields = {key: values.pop(key) for key in ("gender", "birthday", "is_married") if key in values}
    if user_fields:
        assignments = ", ".join(f"{key} = :{key}" for key in user_fields)
        await db.execute(
            text(f"UPDATE users SET {assignments}, updated_at = UTC_TIMESTAMP() WHERE id = :user_id"),
            {**user_fields, "user_id": user_id},
        )

    if "interest_tags" in values:
        values["interest_tags"] = _json_value(values["interest_tags"])
        values["tags"] = values["interest_tags"]
    if "personality_tags" in values:
        values["personality_tags"] = _json_value(values["personality_tags"])
    if "tag_selections" in values:
        values["tags"] = _json_value(values.pop("tag_selections"))
    if values:
        columns = ["user_id", *values]
        placeholders = ", ".join(f":{column}" for column in columns)
        updates = ", ".join(f"{column} = VALUES({column})" for column in values)
        await db.execute(
            text(f"INSERT INTO user_profile ({', '.join(columns)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {updates}"),
            {"user_id": user_id, **values},
        )
    await recalculate_completion(db, user_id)
    await db.commit()
    return await get_profile(db, user_id)


async def recalculate_completion(db: AsyncSession, user_id: int) -> float:
    result = await db.execute(
        text("""SELECT u.gender, u.birthday, u.is_married, u.avatar, u.is_single_pledge,
                      COALESCE(ua.realname_status, 0) AS realname_status,
                      p.occupation, p.education_level, p.income, p.height, p.self_intro,
                      p.residence_province_code, p.residence_city_code, p.interest_tags,
                      p.personality_tags, p.mbti, p.tags, pref.age_min AS preference_age_min,
                      pref.age_max AS preference_age_max,
                      EXISTS (SELECT 1 FROM user_media m WHERE m.user_id = u.id
                              AND m.media_type = 'photo' AND m.deleted_at IS NULL) AS album_done
               FROM users u LEFT JOIN user_profile p ON p.user_id = u.id
               LEFT JOIN user_partner_preference pref ON pref.user_id = u.id
               LEFT JOIN user_auth ua ON ua.user_id = u.id
               WHERE u.id = :id"""),
        {"id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="用户不存在")
    completed = {
        "gender": row["gender"] in (1, 2),
        "birthday": bool(row["birthday"] and _calculate_age(row["birthday"]) >= 18),
        "location": bool(row["residence_province_code"] and row["residence_city_code"]),
        "marriage": row["is_married"] in (1, 2, 3),
        "occupation": bool(row["occupation"]),
        "education": row["education_level"] is not None,
        "income": row["income"] is not None,
        "height": row["height"] is not None,
        "avatar": bool(row["avatar"]),
        "intro": bool(row["self_intro"] and len(row["self_intro"].strip()) >= 20),
        "album": bool(row["album_done"]),
        "interest": len(_json_list(row["interest_tags"])) >= 3 or sum(len(items) for items in _json_dict(row["tags"]).values()) >= 3,
        "personality": len(_json_list(row["personality_tags"])) >= 3,
        "mbti": bool(row["mbti"]),
        "preference": row["preference_age_min"] is not None and row["preference_age_max"] is not None,
        "realname": row["realname_status"] == 2,
        "single_pledge": row["is_single_pledge"] == 1,
    }
    score = float(sum(weight for key, _, weight in COMPLETION_RULES if completed[key]))
    columns = {
        "gender_completed": completed["gender"],
        "birthday_completed": completed["birthday"],
        "location_completed": completed["location"],
        "marriage_completed": completed["marriage"],
        "occupation_completed": completed["occupation"],
        "education_completed": completed["education"],
        "income_completed": completed["income"],
        "height_completed": completed["height"],
        "avatar_completed": completed["avatar"],
        "intro_completed": completed["intro"],
        "album_completed": completed["album"],
        "interest_completed": completed["interest"] and completed["personality"],
        "preference_completed": completed["preference"],
        "realname_completed": completed["realname"],
        "mbti_completed": completed["mbti"],
        "single_pledge_completed": completed["single_pledge"],
    }
    assignments = ", ".join(f"{key} = :{key}" for key in (*columns, "score"))
    await db.execute(
        text(f"""INSERT INTO user_profile_completion (user_id, {', '.join(columns)}, score, algorithm_version, calculated_at)
                   VALUES (:user_id, {', '.join(f':{key}' for key in columns)}, :score, 'profile-v2', UTC_TIMESTAMP())
                   ON DUPLICATE KEY UPDATE {assignments}, algorithm_version = 'profile-v2', calculated_at = UTC_TIMESTAMP()"""),
        {"user_id": user_id, **{key: int(value) for key, value in columns.items()}, "score": score},
    )
    await db.execute(text("UPDATE users SET data_complete_rate = :score WHERE id = :id"), {"score": score, "id": user_id})
    return score


async def get_completion(db: AsyncSession, user_id: int) -> CompletionResponse:
    score = await recalculate_completion(db, user_id)
    await db.commit()
    result = await db.execute(
        text("""SELECT u.gender, u.birthday, u.is_married, u.is_single_pledge, u.avatar,
                      COALESCE(ua.realname_status, 0) AS realname_status,
                      p.occupation, p.education_level, p.income, p.height, p.self_intro,
                      p.residence_province_code, p.residence_city_code, p.interest_tags,
                      p.personality_tags, p.mbti, p.tags, pref.age_min AS preference_age_min,
                      pref.age_max AS preference_age_max,
                      EXISTS (SELECT 1 FROM user_media m WHERE m.user_id = u.id
                              AND m.media_type = 'photo' AND m.deleted_at IS NULL) AS album_done
               FROM users u LEFT JOIN user_profile p ON p.user_id = u.id
               LEFT JOIN user_partner_preference pref ON pref.user_id = u.id
               LEFT JOIN user_auth ua ON ua.user_id = u.id
               WHERE u.id = :id"""),
        {"id": user_id},
    )
    row = result.mappings().one()
    completed = {
        "gender": row["gender"] in (1, 2),
        "birthday": bool(row["birthday"] and _calculate_age(row["birthday"]) >= 18),
        "location": bool(row["residence_province_code"] and row["residence_city_code"]),
        "marriage": row["is_married"] in (1, 2, 3),
        "occupation": bool(row["occupation"]),
        "education": row["education_level"] is not None,
        "income": row["income"] is not None,
        "height": row["height"] is not None,
        "avatar": bool(row["avatar"]),
        "intro": bool(row["self_intro"] and len(row["self_intro"].strip()) >= 20),
        "album": bool(row["album_done"]),
        "interest": len(_json_list(row["interest_tags"])) >= 3 or sum(len(items) for items in _json_dict(row["tags"]).values()) >= 3,
        "personality": len(_json_list(row["personality_tags"])) >= 3,
        "mbti": bool(row["mbti"]),
        "preference": row["preference_age_min"] is not None and row["preference_age_max"] is not None,
        "realname": row["realname_status"] == 2,
        "single_pledge": row["is_single_pledge"] == 1,
    }
    items = [
        CompletionItemResponse(key=key, label=label, weight=weight, completed=completed[key])
        for key, label, weight in COMPLETION_RULES
    ]
    missing = [item.label for item in items if not item.completed]
    return CompletionResponse(
        score=score,
        missing_items=missing,
        items=items,
        can_browse=score >= 100,
        can_apply=score >= 100 and row["realname_status"] == 2,
        can_chat=score >= 100 and row["realname_status"] == 2,
    )


async def get_profile_overview(db: AsyncSession, user_id: int) -> ProfileOverviewResponse:
    completion = await get_completion(db, user_id)
    result = await db.execute(text("""SELECT u.id, u.nickname, u.avatar, u.status,
        COALESCE(ua.realname_status, 0) AS realname_status,
        EXISTS (SELECT 1 FROM user_membership m WHERE m.user_id = u.id AND m.status = 1
          AND (m.start_at IS NULL OR m.start_at <= UTC_TIMESTAMP())
          AND (m.end_at IS NULL OR m.end_at > UTC_TIMESTAMP())) AS is_vip,
        (SELECT m.package_type FROM user_membership m WHERE m.user_id = u.id AND m.status = 1
          AND (m.start_at IS NULL OR m.start_at <= UTC_TIMESTAMP())
          AND (m.end_at IS NULL OR m.end_at > UTC_TIMESTAMP()) ORDER BY m.end_at DESC LIMIT 1) AS package_type,
        (SELECT m.end_at FROM user_membership m WHERE m.user_id = u.id AND m.status = 1
          AND (m.start_at IS NULL OR m.start_at <= UTC_TIMESTAMP())
          AND (m.end_at IS NULL OR m.end_at > UTC_TIMESTAMP()) ORDER BY m.end_at DESC LIMIT 1) AS expires_at,
        (SELECT COUNT(*) FROM user_notification n WHERE n.user_id = u.id AND n.is_read = 0) AS unread_count,
        (SELECT COUNT(*) FROM match_apply a WHERE a.to_user_id = u.id AND a.status = 0) AS incoming_count,
        (SELECT COUNT(*) FROM match_apply a WHERE a.from_user_id = u.id AND a.status = 0) AS outgoing_count,
        (SELECT COUNT(*) FROM user_match m WHERE m.user_id = u.id AND m.status IN (1, 2)) AS match_count,
        (SELECT COUNT(DISTINCT bh.user_id) FROM user_browse_history bh WHERE bh.target_user_id = u.id) AS visitor_count,
        (SELECT COUNT(*) FROM user_favorite f WHERE f.user_id = u.id AND f.type = 2) AS favorite_count,
        (SELECT COUNT(*) FROM user_favorite f WHERE f.target_user_id = u.id AND f.type = 2) AS favorite_received_count,
        (SELECT COUNT(*) FROM user_boost b WHERE b.user_id = u.id) AS superlike_sent_count,
        (SELECT COUNT(*) FROM user_boost b WHERE b.target_user_id = u.id) AS superlike_received_count
        FROM users u LEFT JOIN user_auth ua ON ua.user_id = u.id WHERE u.id = :user_id"""), {"user_id": user_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="用户不存在")
    realname_status = int(row["realname_status"] or 0)
    labels = {0: "未提交", 1: "审核中", 2: "已通过", 3: "未通过"}
    return ProfileOverviewResponse(
        user_id=int(row["id"]), nickname=row["nickname"], avatar=row["avatar"],
        account_status=int(row["status"]), completion_score=completion.score,
        certification={"status": realname_status, "label": labels[realname_status]},
        membership={"is_vip": bool(row["is_vip"]), "package_type": row["package_type"], "expires_at": row["expires_at"]},
        unread_notification_count=int(row["unread_count"] or 0),
        incoming_application_count=int(row["incoming_count"] or 0),
        outgoing_application_count=int(row["outgoing_count"] or 0),
        match_count=int(row["match_count"] or 0),
        visitor_count=int(row["visitor_count"] or 0),
        favorite_count=int(row["favorite_count"] or 0),
        favorite_received_count=int(row["favorite_received_count"] or 0),
        superlike_sent_count=int(row["superlike_sent_count"] or 0),
        superlike_received_count=int(row["superlike_received_count"] or 0),
        shortcuts={"can_browse": completion.can_browse, "can_apply": completion.can_apply, "can_chat": completion.can_chat},
    )


async def get_preferences(db: AsyncSession, user_id: int) -> dict[str, Any]:
    result = await db.execute(text("SELECT * FROM user_partner_preference WHERE user_id = :user_id"), {"user_id": user_id})
    row = result.mappings().first()
    if not row:
        return {
            "user_id": user_id, "age_min": None, "age_max": None, "height_min": None,
            "height_max": None, "education_min": None, "income_min": None,
            "marriage_status": None, "preferred_province_code": None, "preferred_city_codes": [],
            "accept_long_distance": False, "accept_cross_province": False,
            "housing_requirement": None, "smoking_requirement": None,
            "drinking_requirement": None, "extra_requirement": None,
        }
    data = dict(row)
    data["preferred_city_codes"] = _json_list(data["preferred_city_codes"])
    data["accept_long_distance"] = bool(data["accept_long_distance"])
    data["accept_cross_province"] = bool(data["accept_cross_province"])
    return data


async def update_preferences(db: AsyncSession, user_id: int, request: PreferenceUpdateRequest) -> dict[str, Any]:
    values = request.model_dump(exclude_unset=True)
    if "preferred_city_codes" in values:
        values["preferred_city_codes"] = _json_value(values["preferred_city_codes"])
    values = {key: int(value) if isinstance(value, bool) else value for key, value in values.items()}
    result = await db.execute(text("SELECT id FROM user_partner_preference WHERE user_id = :user_id"), {"user_id": user_id})
    exists = result.scalar()
    if exists and values:
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        await db.execute(
            text(f"UPDATE user_partner_preference SET {assignments}, updated_at = UTC_TIMESTAMP() WHERE user_id = :user_id"),
            {**values, "user_id": user_id},
        )
    elif not exists:
        columns = ["user_id", *values]
        placeholders = ", ".join(f":{column}" for column in columns)
        await db.execute(
            text(f"INSERT INTO user_partner_preference ({', '.join(columns)}) VALUES ({placeholders})"),
            {"user_id": user_id, **values},
        )
    await recalculate_completion(db, user_id)
    await db.commit()
    return await get_preferences(db, user_id)


async def upload_avatar(db: AsyncSession, user_id: int, file: UploadFile) -> dict[str, Any]:
    data = await _read_limited(file, IMAGE_MAX_BYTES)
    image_data, thumbnail_data = _image_outputs(data)
    name = uuid.uuid4().hex
    directory = _user_media_dir(user_id)
    image_path = directory / f"avatar-{name}.webp"
    thumbnail_path = directory / f"avatar-{name}-thumb.webp"
    await _write_bytes(image_path, image_data)
    await _write_bytes(thumbnail_path, thumbnail_data)
    url = _media_url(user_id, image_path.name)
    thumbnail_url = _media_url(user_id, thumbnail_path.name)
    await db.execute(text("UPDATE user_media SET deleted_at = UTC_TIMESTAMP() WHERE user_id = :user_id AND media_type = 'avatar' AND deleted_at IS NULL"), {"user_id": user_id})
    result = await db.execute(
        text("""INSERT INTO user_media (user_id, media_type, file_url, storage_key, thumbnail_url,
                   mime_type, file_size, sort_order, is_primary, review_status)
                   VALUES (:user_id, 'avatar', :url, :storage_key, :thumbnail_url,
                   'image/webp', :file_size, 0, 1, 0)"""),
        {"user_id": user_id, "url": url, "storage_key": str(image_path), "thumbnail_url": thumbnail_url, "file_size": len(image_data)},
    )
    await db.execute(text("UPDATE users SET avatar = :avatar, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"avatar": url, "id": user_id})
    await recalculate_completion(db, user_id)
    await db.commit()
    media = await db.execute(text("SELECT id, media_type, file_url, thumbnail_url, sort_order, is_primary, duration_seconds FROM user_media WHERE id = :id"), {"id": result.lastrowid})
    return _media_response(media.mappings().one())


async def upload_background(db: AsyncSession, user_id: int, file: UploadFile) -> dict[str, Any]:
    data = await _read_limited(file, IMAGE_MAX_BYTES)
    image_data, thumbnail_data = _image_outputs(data)
    name = uuid.uuid4().hex
    directory = _user_media_dir(user_id)
    image_path = directory / f"background-{name}.webp"
    thumbnail_path = directory / f"background-{name}-thumb.webp"
    await _write_bytes(image_path, image_data)
    await _write_bytes(thumbnail_path, thumbnail_data)
    url = _media_url(user_id, image_path.name)
    thumbnail_url = _media_url(user_id, thumbnail_path.name)
    await db.execute(text("UPDATE user_media SET deleted_at = UTC_TIMESTAMP() WHERE user_id = :user_id AND media_type = 'background' AND deleted_at IS NULL"), {"user_id": user_id})
    result = await db.execute(
        text("""INSERT INTO user_media (user_id, media_type, file_url, storage_key, thumbnail_url,
                   mime_type, file_size, sort_order, is_primary, review_status)
                   VALUES (:user_id, 'background', :url, :storage_key, :thumbnail_url,
                   'image/webp', :file_size, 0, 1, 0)"""),
        {"user_id": user_id, "url": url, "storage_key": str(image_path), "thumbnail_url": thumbnail_url, "file_size": len(image_data)},
    )
    await db.commit()
    media = await db.execute(text("SELECT id, media_type, file_url, thumbnail_url, sort_order, is_primary, duration_seconds FROM user_media WHERE id = :id"), {"id": result.lastrowid})
    return _media_response(media.mappings().one())


async def upload_photo(db: AsyncSession, user_id: int, file: UploadFile) -> dict[str, Any]:
    result = await db.execute(text("SELECT COUNT(*) FROM user_media WHERE user_id = :user_id AND media_type = 'photo' AND deleted_at IS NULL"), {"user_id": user_id})
    if result.scalar() >= MAX_PHOTOS:
        raise HTTPException(409, detail="相册最多保存9张图片")
    data = await _read_limited(file, IMAGE_MAX_BYTES)
    image_data, thumbnail_data = _image_outputs(data)
    result = await db.execute(text("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM user_media WHERE user_id = :user_id AND media_type = 'photo' AND deleted_at IS NULL"), {"user_id": user_id})
    sort_order = int(result.scalar())
    is_primary = sort_order == 0
    name = uuid.uuid4().hex
    directory = _user_media_dir(user_id)
    image_path = directory / f"photo-{name}.webp"
    thumbnail_path = directory / f"photo-{name}-thumb.webp"
    await _write_bytes(image_path, image_data)
    await _write_bytes(thumbnail_path, thumbnail_data)
    url = _media_url(user_id, image_path.name)
    thumbnail_url = _media_url(user_id, thumbnail_path.name)
    result = await db.execute(
        text("""INSERT INTO user_media (user_id, media_type, file_url, storage_key, thumbnail_url,
                   mime_type, file_size, sort_order, is_primary, review_status)
                   VALUES (:user_id, 'photo', :url, :storage_key, :thumbnail_url,
                   'image/webp', :file_size, :sort_order, :is_primary, 0)"""),
        {"user_id": user_id, "url": url, "storage_key": str(image_path), "thumbnail_url": thumbnail_url, "file_size": len(image_data), "sort_order": sort_order, "is_primary": int(is_primary)},
    )
    if is_primary:
        await db.execute(text("UPDATE users SET avatar = :avatar, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"avatar": url, "id": user_id})
    await recalculate_completion(db, user_id)
    await db.commit()
    media = await db.execute(text("SELECT id, media_type, file_url, thumbnail_url, sort_order, is_primary, duration_seconds FROM user_media WHERE id = :id"), {"id": result.lastrowid})
    return _media_response(media.mappings().one())


async def _probe_video(path: Path) -> int:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise HTTPException(503, detail="视频处理服务未配置，请安装ffprobe")
    process = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-show_entries", "format=format_name,duration", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
    except asyncio.TimeoutError as exc:
        process.kill()
        raise HTTPException(422, detail="视频校验超时") from exc
    if process.returncode != 0:
        raise HTTPException(415, detail="视频文件无法识别")
    try:
        payload = json.loads(stdout.decode("utf-8"))
        format_name = str(payload["format"]["format_name"])
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(415, detail="视频元数据无效") from exc
    if "mp4" not in format_name.split(","):
        raise HTTPException(415, detail="仅支持MP4视频")
    if duration <= 0 or duration > VIDEO_MAX_SECONDS:
        raise HTTPException(422, detail="视频时长不能超过30秒")
    return math.ceil(duration)


async def upload_video(db: AsyncSession, user_id: int, file: UploadFile) -> dict[str, Any]:
    result = await db.execute(text("SELECT COUNT(*) FROM user_media WHERE user_id = :user_id AND media_type = 'video' AND deleted_at IS NULL"), {"user_id": user_id})
    if result.scalar():
        raise HTTPException(409, detail="每个用户最多上传一个视频")
    directory = _user_media_dir(user_id)
    directory.mkdir(parents=True, exist_ok=True)
    temp_path = directory / f"video-{uuid.uuid4().hex}.upload"
    try:
        total = 0
        async with aiofiles.open(temp_path, "wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > VIDEO_MAX_BYTES:
                    raise HTTPException(413, detail="视频大小不能超过50MB")
                await output.write(chunk)
        duration = await _probe_video(temp_path)
        final_path = directory / f"video-{uuid.uuid4().hex}.mp4"
        os.replace(temp_path, final_path)
        url = _media_url(user_id, final_path.name)
        result = await db.execute(
            text("""INSERT INTO user_media (user_id, media_type, file_url, storage_key, mime_type,
                       file_size, duration_seconds, sort_order, is_primary, review_status)
                       VALUES (:user_id, 'video', :url, :storage_key, 'video/mp4',
                       :file_size, :duration, 0, 0, 0)"""),
            {"user_id": user_id, "url": url, "storage_key": str(final_path), "file_size": total, "duration": duration},
        )
        await db.commit()
        media = await db.execute(text("SELECT id, media_type, file_url, thumbnail_url, sort_order, is_primary, duration_seconds FROM user_media WHERE id = :id"), {"id": result.lastrowid})
        return _media_response(media.mappings().one())
    finally:
        if temp_path.exists():
            temp_path.unlink()


async def delete_photo(db: AsyncSession, user_id: int, media_id: int) -> None:
    result = await db.execute(text("SELECT file_url, is_primary FROM user_media WHERE id = :id AND user_id = :user_id AND media_type = 'photo' AND deleted_at IS NULL FOR UPDATE"), {"id": media_id, "user_id": user_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="相册图片不存在")
    await db.execute(text("UPDATE user_media SET deleted_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": media_id})
    if row["is_primary"]:
        next_media = await db.execute(text("SELECT id, file_url FROM user_media WHERE user_id = :user_id AND media_type = 'photo' AND deleted_at IS NULL ORDER BY sort_order, id LIMIT 1"), {"user_id": user_id})
        next_row = next_media.mappings().first()
        if next_row:
            await db.execute(text("UPDATE user_media SET is_primary = 1 WHERE id = :id"), {"id": next_row["id"]})
            await db.execute(text("UPDATE users SET avatar = :avatar WHERE id = :id"), {"avatar": next_row["file_url"], "id": user_id})
        else:
            avatar = await db.execute(text("SELECT file_url FROM user_media WHERE user_id = :user_id AND media_type = 'avatar' AND deleted_at IS NULL ORDER BY id DESC LIMIT 1"), {"user_id": user_id})
            avatar_row = avatar.mappings().first()
            await db.execute(text("UPDATE users SET avatar = :avatar WHERE id = :id"), {"avatar": avatar_row["file_url"] if avatar_row else None, "id": user_id})
    await recalculate_completion(db, user_id)
    await db.commit()


async def reorder_photos(db: AsyncSession, user_id: int, request: PhotoOrderRequest) -> None:
    result = await db.execute(text("SELECT id FROM user_media WHERE user_id = :user_id AND media_type = 'photo' AND deleted_at IS NULL"), {"user_id": user_id})
    active_ids = {int(row[0]) for row in result}
    if active_ids != set(request.media_ids):
        raise HTTPException(422, detail="排序列表必须包含当前全部相册图片")
    for order, media_id in enumerate(request.media_ids):
        await db.execute(text("UPDATE user_media SET sort_order = :sort_order WHERE id = :id AND user_id = :user_id"), {"sort_order": order, "id": media_id, "user_id": user_id})
    await db.commit()


async def set_primary_photo(db: AsyncSession, user_id: int, media_id: int) -> dict[str, Any]:
    result = await db.execute(text("SELECT file_url FROM user_media WHERE id = :id AND user_id = :user_id AND media_type = 'photo' AND deleted_at IS NULL"), {"id": media_id, "user_id": user_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="相册图片不存在")
    await db.execute(text("UPDATE user_media SET is_primary = 0 WHERE user_id = :user_id AND media_type = 'photo' AND deleted_at IS NULL"), {"user_id": user_id})
    await db.execute(text("UPDATE user_media SET is_primary = 1 WHERE id = :id"), {"id": media_id})
    await db.execute(text("UPDATE users SET avatar = :avatar, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"avatar": row["file_url"], "id": user_id})
    await db.commit()
    result = await db.execute(text("SELECT id, media_type, file_url, thumbnail_url, sort_order, is_primary, duration_seconds FROM user_media WHERE id = :id"), {"id": media_id})
    return _media_response(result.mappings().one())


async def get_intro_templates() -> list[IntroTemplateResponse]:
    return [IntroTemplateResponse(**template) for template in INTRO_TEMPLATES]


async def get_tag_options() -> TagOptionsResponse:
    return TagOptionsResponse(
        version="v1",
        categories=[
            TagCategoryResponse(key=key, label=label, options=list(options))
            for key, label, options in TAG_CATEGORIES
        ],
    )


async def get_profile_preview(db: AsyncSession, user_id: int) -> ProfilePreviewResponse:
    return ProfilePreviewResponse(preview_notice="这是别人看到你的样子", profile=await get_profile(db, user_id, public=True))


async def review_media(db: AsyncSession, media_id: int, request: MediaReviewRequest) -> MediaReviewResponse:
    result = await db.execute(text("SELECT id, user_id FROM user_media WHERE id = :media_id AND deleted_at IS NULL FOR UPDATE"), {"media_id": media_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="媒体不存在")
    await db.execute(text("""UPDATE user_media SET review_status = :status, review_reason = :reason,
        reviewed_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() WHERE id = :media_id"""), {"media_id": media_id, "status": request.status, "reason": request.reason})
    await recalculate_completion(db, int(row["user_id"]))
    await db.commit()
    return MediaReviewResponse(media_id=media_id, user_id=int(row["user_id"]), status=request.status, reason=request.reason)
