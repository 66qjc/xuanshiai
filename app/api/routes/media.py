"""Generic media upload routes."""

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_realname_verified_user, get_verified_user
from app.db.session import get_db
from app.schemas.community import MediaUploadResponse
from app.services.media import upload_media

router = APIRouter(dependencies=[Depends(get_verified_user)])


@router.post(
    "/media/uploads",
    response_model=MediaUploadResponse,
    status_code=201,
    summary="上传媒体文件",
)
async def create_media_upload(
    file: UploadFile = File(...),
    purpose: str = Form("paper_plane_voice"),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> MediaUploadResponse:
    # db reserved for future audit/metadata writes
    _ = db
    return await upload_media(current.id, file, purpose)
