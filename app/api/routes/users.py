"""当前用户资料、媒体和择偶要求接口。"""

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.auth import (
    CompletionResponse,
    IntroTemplateResponse,
    PhotoOrderRequest,
    PreferenceResponse,
    PreferenceUpdateRequest,
    ProfileMediaResponse,
    ProfilePreviewResponse,
    ProfileResponse,
    ProfileUpdateRequest,
    ProfileOverviewResponse,
    NicknameUpdateRequest,
    NicknameUpdateResponse,
)
from app.services.profile import (
    delete_photo,
    get_completion,
    get_intro_templates,
    get_preferences,
    get_profile,
    get_profile_preview,
    reorder_photos,
    set_primary_photo,
    update_preferences,
    update_profile,
    upload_avatar,
    upload_background,
    upload_photo,
    upload_video,
    get_profile_overview,
    update_nickname,
)

router = APIRouter(prefix="/users/me")


@router.get("/overview", response_model=ProfileOverviewResponse, summary="获取我的页面聚合信息")
async def overview(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ProfileOverviewResponse:
    return await get_profile_overview(db, current.id)


@router.get("/profile", response_model=ProfileResponse, summary="获取个人资料")
async def profile(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """返回当前用户的基础资料、媒体和资料完整度。"""
    return await get_profile(db, current.id)


@router.patch("/profile", response_model=ProfileResponse, summary="修改个人资料")
async def edit_profile(body: ProfileUpdateRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """更新当前用户资料并重新计算资料完整度。"""
    return await update_profile(db, current.id, body)


@router.patch("/nickname", response_model=NicknameUpdateResponse, summary="修改个人昵称")
async def edit_nickname(
    body: NicknameUpdateRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NicknameUpdateResponse:
    """Update only the nickname without changing other profile fields."""
    return await update_nickname(db, current.id, body.nickname)


@router.get("/completion", response_model=CompletionResponse, summary="查询资料完整度")
async def completion(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> CompletionResponse:
    """返回逐项资料完成状态以及功能权限。"""
    return await get_completion(db, current.id)


@router.get("/profile/preview", response_model=ProfilePreviewResponse, summary="预览个人主页")
async def profile_preview(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ProfilePreviewResponse:
    """返回对外展示的个人主页预览。"""
    return await get_profile_preview(db, current.id)


@router.get("/intro-templates", response_model=list[IntroTemplateResponse], summary="查询自我介绍模板")
async def intro_templates() -> list[IntroTemplateResponse]:
    """返回一期内置的自我介绍模板。"""
    return await get_intro_templates()


@router.get("/preferences", response_model=PreferenceResponse, summary="获取择偶要求")
async def preferences(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """返回当前用户的择偶要求。"""
    return await get_preferences(db, current.id)


@router.put("/preferences", response_model=PreferenceResponse, summary="更新择偶要求")
async def edit_preferences(body: PreferenceUpdateRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """更新择偶要求并立即影响后续推荐。"""
    return await update_preferences(db, current.id, body)


@router.post("/avatar", response_model=ProfileMediaResponse, status_code=201, summary="上传头像")
async def avatar(file: UploadFile = File(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """上传并压缩头像，重复上传会覆盖旧头像记录。"""
    return await upload_avatar(db, current.id, file)


@router.post("/background", response_model=ProfileMediaResponse, status_code=201, summary="上传主页背景墙")
async def background(file: UploadFile = File(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """上传主页背景墙，重复上传会覆盖旧背景。"""
    return await upload_background(db, current.id, file)


@router.post("/photos", response_model=ProfileMediaResponse, status_code=201, summary="上传相册图片")
async def photo(file: UploadFile = File(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """上传相册图片，最多保存9张。"""
    return await upload_photo(db, current.id, file)


@router.post("/video", response_model=ProfileMediaResponse, status_code=201, summary="上传个人视频")
async def video(file: UploadFile = File(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """上传MP4个人视频，最大50MB且最长30秒。"""
    return await upload_video(db, current.id, file)


@router.put("/photos/order", status_code=204, summary="调整相册顺序")
async def photo_order(body: PhotoOrderRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    """按提交的媒体 ID 顺序保存相册排序。"""
    await reorder_photos(db, current.id, body)


@router.put("/photos/{media_id}/primary", response_model=ProfileMediaResponse, summary="设置相册首图")
async def primary_photo(media_id: int, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """设置一张相册图片为首图并同步个人头像展示地址。"""
    return await set_primary_photo(db, current.id, media_id)


@router.delete("/photos/{media_id}", status_code=204, summary="删除相册图片")
async def remove_photo(media_id: int, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    """软删除当前用户的一张相册图片。"""
    await delete_photo(db, current.id, media_id)
