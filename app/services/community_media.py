"""Community media upload, ownership and binding helpers."""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
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
from app.schemas.community import CommunityMediaResponse

IMAGE_MAX_BYTES = 5 * 1024 * 1024
VIDEO_MAX_BYTES = 50 * 1024 * 1024
VIDEO_MAX_SECONDS = 30
IMAGE_MAX_PIXELS = 25_000_000
UNBOUND_TTL_HOURS = 24
ALLOWED_PURPOSES = {"post", "paper_plane"}


def _media_url(user_id: int, filename: str) -> str:
    return f"/storage/uploads/{user_id}/community/{filename}"


def _dir(user_id: int) -> Path:
    path = Path(settings.upload_dir) / str(user_id) / "community"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    if total == 0:
        raise HTTPException(422, detail="文件内容为空")
    return b"".join(chunks)


def _image_outputs(data: bytes) -> tuple[bytes, bytes]:
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(415, detail="仅支持JPG、JPEG、PNG或WEBP图片")
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


async def _probe_video(path: Path) -> int:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise HTTPException(503, detail="视频处理服务未配置，请安装ffprobe")
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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


def _row_response(row: Any) -> CommunityMediaResponse:
    return CommunityMediaResponse(
        id=int(row["id"]),
        purpose=row["purpose"],
        media_type=row["media_type"],
        url=row["file_url"],
        thumbnail_url=row.get("thumbnail_url"),
        file_size=row.get("file_size"),
        duration_seconds=row.get("duration_seconds"),
        status=row["status"],
    )


def _thumb_storage_path(thumbnail_url: str | None) -> Path | None:
    if not thumbnail_url:
        return None
    marker = "/storage/uploads/"
    if marker not in thumbnail_url:
        return None
    relative = thumbnail_url.split(marker, 1)[1]
    return Path(settings.upload_dir) / relative


async def upload_community_media(
    db: AsyncSession,
    user_id: int,
    file: UploadFile,
    purpose: str,
) -> CommunityMediaResponse:
    purpose_norm = (purpose or "").strip()
    if purpose_norm not in ALLOWED_PURPOSES:
        raise HTTPException(422, detail="purpose 仅支持 post 或 paper_plane")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    name = file.filename or ""
    looks_video = content_type.startswith("video/") or name.lower().endswith(".mp4")
    if purpose_norm == "paper_plane" and looks_video:
        raise HTTPException(422, detail="纸飞机不支持视频")

    directory = _dir(user_id)
    expire_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=UNBOUND_TTL_HOURS)

    if looks_video:
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
            if total == 0:
                raise HTTPException(422, detail="文件内容为空")
            duration = await _probe_video(temp_path)
            final_path = directory / f"{uuid.uuid4().hex}.mp4"
            os.replace(temp_path, final_path)
            url = _media_url(user_id, final_path.name)
            result = await db.execute(
                text(
                    """INSERT INTO community_media
                    (user_id, purpose, media_type, file_url, storage_key, thumbnail_url,
                     mime_type, file_size, duration_seconds, status, expire_at)
                    VALUES
                    (:user_id, :purpose, 'video', :url, :storage_key, NULL,
                     'video/mp4', :file_size, :duration, 'ready', :expire_at)"""
                ),
                {
                    "user_id": user_id,
                    "purpose": purpose_norm,
                    "url": url,
                    "storage_key": str(final_path),
                    "file_size": total,
                    "duration": duration,
                    "expire_at": expire_at,
                },
            )
            await db.commit()
            row = await db.execute(
                text("SELECT * FROM community_media WHERE id = :id"),
                {"id": result.lastrowid},
            )
            return _row_response(row.mappings().one())
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    data = await _read_limited(file, IMAGE_MAX_BYTES)
    image_data, thumb_data = _image_outputs(data)
    token = uuid.uuid4().hex
    image_path = directory / f"{token}.webp"
    thumb_path = directory / f"{token}-thumb.webp"
    async with aiofiles.open(image_path, "wb") as out:
        await out.write(image_data)
    async with aiofiles.open(thumb_path, "wb") as out:
        await out.write(thumb_data)
    url = _media_url(user_id, image_path.name)
    thumb_url = _media_url(user_id, thumb_path.name)
    result = await db.execute(
        text(
            """INSERT INTO community_media
            (user_id, purpose, media_type, file_url, storage_key, thumbnail_url,
             mime_type, file_size, duration_seconds, status, expire_at)
            VALUES
            (:user_id, :purpose, 'image', :url, :storage_key, :thumb,
             'image/webp', :file_size, NULL, 'ready', :expire_at)"""
        ),
        {
            "user_id": user_id,
            "purpose": purpose_norm,
            "url": url,
            "storage_key": str(image_path),
            "thumb": thumb_url,
            "file_size": len(image_data),
            "expire_at": expire_at,
        },
    )
    await db.commit()
    row = await db.execute(
        text("SELECT * FROM community_media WHERE id = :id"),
        {"id": result.lastrowid},
    )
    return _row_response(row.mappings().one())


async def delete_community_media(db: AsyncSession, user_id: int, media_id: int) -> None:
    result = await db.execute(
        text(
            """SELECT id, status, storage_key, thumbnail_url
            FROM community_media
            WHERE id = :id AND user_id = :user_id AND deleted_at IS NULL
            FOR UPDATE"""
        ),
        {"id": media_id, "user_id": user_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="媒体不存在")
    if row["status"] == "bound":
        raise HTTPException(409, detail="媒体已绑定内容，无法删除")
    await db.execute(
        text(
            """UPDATE community_media
            SET status = 'deleted', deleted_at = UTC_TIMESTAMP()
            WHERE id = :id"""
        ),
        {"id": media_id},
    )
    await db.commit()
    # best-effort file cleanup; failures ignored for expire cleanup later
    for key in (row.get("storage_key"),):
        if key and Path(str(key)).exists():
            try:
                Path(str(key)).unlink()
            except OSError:
                pass
    thumb_path = _thumb_storage_path(row.get("thumbnail_url"))
    if thumb_path is not None and thumb_path.exists():
        try:
            thumb_path.unlink()
        except OSError:
            pass


FORBIDDEN_URL_PREFIXES = (
    "wxfile://",
    "file://",
    "temp://",
    "http://",
    "https://",
)
ALLOWED_STORAGE_PREFIX = "/storage/uploads/"


def _is_forbidden_media_url(url: str) -> bool:
    value = (url or "").strip()
    if not value:
        return True
    lowered = value.casefold()
    if any(lowered.startswith(prefix) for prefix in FORBIDDEN_URL_PREFIXES):
        return True
    # Windows absolute paths: C:\... or C:/...
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        return True
    # UNC / absolute disk-ish paths without controlled storage prefix
    if value.startswith("\\\\") or value.startswith("//"):
        return True
    if value.startswith("/") and not value.startswith(ALLOWED_STORAGE_PREFIX):
        return True
    if not value.startswith(ALLOWED_STORAGE_PREFIX):
        return True
    return False


async def assert_owned_media_urls(
    db: AsyncSession,
    user_id: int,
    urls: list[str] | None,
    *,
    purpose: str,
    media_type: str | None = None,
) -> list[dict[str, Any]]:
    """Validate legacy media URLs are owned community_media rows for purpose.

    Returns matching media rows (ready or bound) in input order. Ready rows
    should be bound by the caller after the target insert.
    """
    ordered_urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
    if not ordered_urls:
        return []

    rows_out: list[dict[str, Any]] = []
    for url in ordered_urls:
        if _is_forbidden_media_url(url):
            raise HTTPException(422, detail="仅允许使用已上传的社区媒体地址")
        params: dict[str, Any] = {
            "user_id": user_id,
            "purpose": purpose,
            "url": url,
        }
        type_clause = ""
        if media_type is not None:
            type_clause = " AND media_type = :media_type"
            params["media_type"] = media_type
        result = await db.execute(
            text(
                f"""SELECT *
                FROM community_media
                WHERE file_url = :url
                  AND user_id = :user_id
                  AND purpose = :purpose
                  AND deleted_at IS NULL
                  AND status IN ('ready', 'bound')
                  {type_clause}
                LIMIT 1"""
            ),
            params,
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(422, detail="仅允许使用已上传的社区媒体地址")
        rows_out.append(dict(row))
    return rows_out


async def resolve_owned_ready_media(
    db: AsyncSession,
    user_id: int,
    media_ids: list[int],
    *,
    purpose: str,
    media_type: str | None = None,
) -> list[dict[str, Any]]:
    if not media_ids:
        return []
    unique_ids = list(dict.fromkeys(media_ids))
    placeholders = ", ".join(f":id{i}" for i in range(len(unique_ids)))
    params: dict[str, Any] = {
        "user_id": user_id,
        "purpose": purpose,
        **{f"id{i}": media_id for i, media_id in enumerate(unique_ids)},
    }
    type_clause = ""
    if media_type is not None:
        type_clause = " AND media_type = :media_type"
        params["media_type"] = media_type
    result = await db.execute(
        text(
            f"""SELECT *
            FROM community_media
            WHERE id IN ({placeholders})
              AND user_id = :user_id
              AND purpose = :purpose
              AND status = 'ready'
              AND deleted_at IS NULL
              {type_clause}"""
        ),
        params,
    )
    rows = {int(row["id"]): dict(row) for row in result.mappings().all()}
    ordered: list[dict[str, Any]] = []
    # unique_ids preserves first-seen order; never re-attach duplicate IDs
    for media_id in unique_ids:
        row = rows.get(int(media_id))
        if row is None:
            raise HTTPException(422, detail=f"媒体不可用: {media_id}")
        ordered.append(row)
    return ordered


async def bind_media(
    db: AsyncSession,
    *,
    media_ids: list[int],
    target_type: str,
    target_id: int,
) -> None:
    """Attach ready media to a target and mark them bound.

    Each media row is locked with FOR UPDATE, then attachment INSERT and
    ready→bound UPDATE run in the caller's transaction. Non-ready / missing
    media fails the whole bind so no orphan attachments remain.
    """
    for order, media_id in enumerate(media_ids):
        locked = await db.execute(
            text(
                """SELECT id, status
                FROM community_media
                WHERE id = :id AND deleted_at IS NULL
                FOR UPDATE"""
            ),
            {"id": media_id},
        )
        row = locked.mappings().first()
        if not row:
            raise HTTPException(422, detail=f"媒体不存在: {media_id}")
        if row["status"] != "ready":
            raise HTTPException(
                409,
                detail=f"媒体状态不可绑定: {media_id} (status={row['status']})",
            )

        await db.execute(
            text(
                """INSERT INTO community_media_attachment
                (media_id, target_type, target_id, sort_order)
                VALUES (:media_id, :target_type, :target_id, :sort_order)"""
            ),
            {
                "media_id": media_id,
                "target_type": target_type,
                "target_id": target_id,
                "sort_order": order,
            },
        )
        update_result = await db.execute(
            text(
                """UPDATE community_media
                SET status = 'bound', expire_at = NULL
                WHERE id = :id AND status = 'ready'"""
            ),
            {"id": media_id},
        )
        if getattr(update_result, "rowcount", 0) == 0:
            raise HTTPException(409, detail=f"媒体绑定失败: {media_id}")


async def cleanup_expired_unbound_media(db: AsyncSession, *, limit: int = 100) -> int:
    """删除 status=ready 且 expire_at < now 的未绑定媒体。"""
    result = await db.execute(
        text(
            """SELECT id, storage_key, thumbnail_url
            FROM community_media
            WHERE status = 'ready'
              AND deleted_at IS NULL
              AND expire_at IS NOT NULL
              AND expire_at < UTC_TIMESTAMP()
            ORDER BY expire_at ASC
            LIMIT :limit
            FOR UPDATE"""
        ),
        {"limit": limit},
    )
    rows = list(result.mappings().all())
    if not rows:
        return 0
    ids = [int(row["id"]) for row in rows]
    placeholders = ", ".join(f":id{i}" for i in range(len(ids)))
    params = {f"id{i}": media_id for i, media_id in enumerate(ids)}
    await db.execute(
        text(
            f"""UPDATE community_media
            SET status = 'deleted', deleted_at = UTC_TIMESTAMP()
            WHERE id IN ({placeholders})"""
        ),
        params,
    )
    await db.commit()
    for row in rows:
        storage_key = row.get("storage_key")
        if storage_key and Path(str(storage_key)).exists():
            try:
                Path(str(storage_key)).unlink()
            except OSError:
                pass
        thumb_path = _thumb_storage_path(row.get("thumbnail_url"))
        if thumb_path is not None and thumb_path.exists():
            try:
                thumb_path.unlink()
            except OSError:
                pass
    return len(rows)
