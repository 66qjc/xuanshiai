"""Generic media upload helpers for community and paper-plane voice."""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path

import aiofiles
from fastapi import HTTPException, UploadFile

from app.core.config import settings
from app.schemas.community import MediaUploadResponse

AUDIO_MAX_BYTES = 5 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/aac",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/ogg",
    "audio/x-m4a",
    "audio/m4a",
}
ALLOWED_PURPOSES = {
    "paper_plane_voice",
    "chat_voice",
    "community_image",
}
AUDIO_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
}


def _media_url(user_id: int, relative_path: str) -> str:
    return f"/storage/uploads/{user_id}/{relative_path}"


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


def _guess_content_type(file: UploadFile) -> str:
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type:
        return content_type
    guessed, _ = mimetypes.guess_type(file.filename or "")
    return (guessed or "application/octet-stream").lower()


async def upload_media(user_id: int, file: UploadFile, purpose: str) -> MediaUploadResponse:
    purpose_norm = (purpose or "paper_plane_voice").strip()
    if purpose_norm not in ALLOWED_PURPOSES:
        raise HTTPException(422, detail="不支持的上传用途")
    if purpose_norm not in {"paper_plane_voice", "chat_voice"}:
        raise HTTPException(422, detail="本期仅支持语音上传")

    content_type = _guess_content_type(file)
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(415, detail="仅支持 mp3/aac/wav/m4a/webm/ogg 语音")

    data = await _read_limited(file, AUDIO_MAX_BYTES)
    extension = AUDIO_EXTENSIONS.get(content_type, ".bin")
    relative = f"audio/{uuid.uuid4().hex}{extension}"
    directory = Path(settings.upload_dir) / str(user_id) / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / Path(relative).name
    async with aiofiles.open(path, "wb") as output:
        await output.write(data)
    return MediaUploadResponse(
        url=_media_url(user_id, relative.replace("\\", "/")),
        content_type=content_type,
        size=len(data),
        purpose=purpose_norm,
    )
