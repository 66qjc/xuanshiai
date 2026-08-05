"""Administrative moderation routes."""

from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_admin
from app.db.session import get_db
from app.schemas.admin import (
    AdminReportItem,
    AdminReportPage,
    AdminReportAppealPage,
    CertificationReviewRequest,
    CertificationReviewResponse,
    ContentModerationRequest,
    ContentModerationResponse,
    MediaReviewRequest,
    MediaReviewResponse,
    ReportReviewRequest,
    ReportReviewResponse,
    AdminGrantRequest, AdminGrantResponse, ModerationItemPage, ModerationReviewRequest, ModerationReviewResponse,
    ReportAppealReviewRequest,
    ReportAppealReviewResponse,
)
from app.schemas.restrictions import RestrictionCreate, RestrictionPage, RestrictionResponse
from app.services.profile import review_media
from app.services.social import (
    get_admin_report,
    list_admin_report_appeals,
    list_admin_reports,
    moderate_content,
    review_report,
    review_report_appeal,
)
from app.services.certifications import review_certification
from app.services.moderation import grant_admin, list_moderation_items, review_moderation_item
from app.services.restrictions import create_restriction, end_restriction, list_restrictions

router = APIRouter(prefix="/admin")


@router.get("/users/{user_id}/restrictions", response_model=RestrictionPage, summary="查询用户限制记录")
async def user_restrictions(
    user_id: int = Path(..., ge=1), page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=100), admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> RestrictionPage:
    return await list_restrictions(db, user_id, page, page_size)


@router.patch("/users/{user_id}/restrictions", response_model=RestrictionResponse, summary="设置用户限制或总封禁")
async def restrict_user(
    user_id: int = Path(..., ge=1), body: RestrictionCreate = Body(...),
    admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
) -> RestrictionResponse:
    return await create_restriction(db, user_id, body, actor_id=admin.id)


@router.delete("/users/{user_id}/restrictions/{restriction_id}", status_code=204, summary="解除用户限制")
async def release_user_restriction(
    user_id: int = Path(..., ge=1), restriction_id: int = Path(..., ge=1),
    admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
) -> None:
    owner = await db.execute(text("SELECT user_id FROM user_restriction WHERE id = :id"), {"id": restriction_id})
    row = owner.mappings().first()
    if not row or int(row["user_id"]) != user_id:
        raise HTTPException(status_code=404, detail="限制记录不存在")
    await end_restriction(db, restriction_id, actor_id=admin.id)


@router.get("/community/moderation-items", response_model=ModerationItemPage, summary="查看社区待审核内容")
async def moderation_items(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    status: Literal["pending", "approved", "rejected", "replaced", "deleted", "hidden"] = Query("pending"),
    target_type: Literal["post", "comment", "paper_plane", "paper_plane_reply", "paper_plane_message", "media"] | None = Query(None),
    admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
) -> ModerationItemPage:
    return await list_moderation_items(db, page=page, page_size=page_size, status=status, target_type=target_type)


@router.patch("/community/moderation-items/{task_id}/review", response_model=ModerationReviewResponse, summary="审核社区内容")
async def review_moderation_item_route(
    task_id: int = Path(..., ge=1), body: ModerationReviewRequest = Body(...),
    admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
) -> ModerationReviewResponse:
    return await review_moderation_item(db, task_id, body, admin_id=admin.id)


@router.post("/users/grant", response_model=AdminGrantResponse, summary="授予管理员及权限")
async def grant_admin_route(
    body: AdminGrantRequest = Body(...), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db),
) -> AdminGrantResponse:
    return await grant_admin(db, body, granted_by=admin.id)


@router.patch("/media/{media_id}/review", response_model=MediaReviewResponse, summary="审核用户媒体")
async def review_user_media(media_id: int = Path(..., ge=1), body: MediaReviewRequest = Body(...), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> MediaReviewResponse:
    return await review_media(db, media_id, body)


@router.get("/reports", response_model=AdminReportPage, summary="举报列表")
async def list_reports(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Literal[0, 1, 2] | None = Query(default=None),
    target_type: Literal["user", "post", "comment", "paper_plane"] | None = Query(default=None),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminReportPage:
    return await list_admin_reports(db, page=page, page_size=page_size, status=status, target_type=target_type)


@router.get("/reports/{report_id}", response_model=AdminReportItem, summary="举报详情")
async def report_detail(
    report_id: int = Path(..., ge=1),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminReportItem:
    return await get_admin_report(db, report_id)


@router.patch("/reports/{report_id}/review", response_model=ReportReviewResponse, summary="处理用户举报")
async def review_user_report(report_id: int = Path(..., ge=1), body: ReportReviewRequest = Body(...), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> ReportReviewResponse:
    return await review_report(db, report_id, body, actor_id=admin.id)


@router.get("/report-appeals", response_model=AdminReportAppealPage, summary="举报申诉列表")
async def report_appeals(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Literal[0, 1, 2] | None = Query(default=None),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminReportAppealPage:
    return await list_admin_report_appeals(
        db, page=page, page_size=page_size, status=status
    )


@router.patch(
    "/report-appeals/{appeal_id}/review",
    response_model=ReportAppealReviewResponse,
    summary="复审举报申诉",
)
async def review_appeal(
    appeal_id: int = Path(..., ge=1),
    body: ReportAppealReviewRequest = Body(...),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> ReportAppealReviewResponse:
    return await review_report_appeal(
        db, appeal_id=appeal_id, request=body, actor_id=admin.id
    )


@router.patch(
    "/community/posts/{post_id}/moderation",
    response_model=ContentModerationResponse,
    summary="下架或恢复动态",
)
async def moderate_post(
    post_id: int = Path(..., ge=1),
    body: ContentModerationRequest = Body(...),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> ContentModerationResponse:
    await moderate_content(
        db,
        target_type="post",
        target_id=post_id,
        hide=body.status == 2,
        reason=body.reason,
        actor_id=admin.id,
    )
    await db.commit()
    return ContentModerationResponse(target_type="post", target_id=post_id, status=body.status, reason=body.reason)


@router.patch(
    "/community/comments/{comment_id}/moderation",
    response_model=ContentModerationResponse,
    summary="下架或恢复评论",
)
async def moderate_comment(
    comment_id: int = Path(..., ge=1),
    body: ContentModerationRequest = Body(...),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> ContentModerationResponse:
    await moderate_content(
        db,
        target_type="comment",
        target_id=comment_id,
        hide=body.status == 2,
        reason=body.reason,
        actor_id=admin.id,
    )
    await db.commit()
    return ContentModerationResponse(target_type="comment", target_id=comment_id, status=body.status, reason=body.reason)


@router.patch(
    "/community/paper-planes/{plane_id}/moderation",
    response_model=ContentModerationResponse,
    summary="下架或恢复纸飞机",
)
async def moderate_paper_plane(
    plane_id: int = Path(..., ge=1),
    body: ContentModerationRequest = Body(...),
    admin: CurrentUser = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
) -> ContentModerationResponse:
    await moderate_content(
        db,
        target_type="paper_plane",
        target_id=plane_id,
        hide=body.status == 2,
        reason=body.reason,
        actor_id=admin.id,
    )
    await db.commit()
    return ContentModerationResponse(target_type="paper_plane", target_id=plane_id, status=body.status, reason=body.reason)


@router.patch("/users/{user_id}/certifications/{kind}/review", response_model=CertificationReviewResponse, summary="审核用户资质认证")
async def review_user_certification(user_id: int = Path(..., ge=1), kind: str = Path(..., pattern="^(education|house|marriage)$"), body: CertificationReviewRequest = Body(...), admin: CurrentUser = Depends(get_current_admin), db: AsyncSession = Depends(get_db)) -> CertificationReviewResponse:
    return await review_certification(db, user_id, kind, body)
