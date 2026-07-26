"""Community, topic, activity and paper-plane routes."""

from collections.abc import Awaitable, Callable
import logging
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Body, Depends, File, Form, Header, Path, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    CurrentUser,
    get_current_user,
    get_realname_verified_user,
    get_verified_user,
)
from app.core.redis import daily_quota_key, refund_daily
from app.db.session import get_db
from app.schemas.community import (
    ActivityPage,
    ActivityResponse,
    ActivitySignupCreate,
    ActivitySignupResponse,
    CommunityBannerResponse,
    CITY_CODE_PATTERN,
    CommunityCityResponse,
    CommunityCityUpdateRequest,
    CommunityCollectResponse,
    CommunityCommentCreate,
    CommunityCommentResponse,
    CommunityMediaResponse,
    CommunityPostCreate,
    CommunityPostPage,
    CommunityPostResponse,
    CommunityQuotasResponse,
    CommunityReportCreate,
    CommunityReportReason,
    CommunityReportResponse,
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
from app.services.community_media import delete_community_media, upload_community_media
from app.services.community import (
    collect_post,
    create_comment,
    create_paper_plane,
    create_post,
    delete_comment,
    delete_post,
    end_paper_plane_conversation,
    get_activity,
    get_community_quotas,
    get_current_city,
    get_post,
    get_topic,
    get_topic_detail,
    join_topic,
    leave_topic,
    like_comment,
    like_post,
    list_activities,
    list_banners,
    list_comments,
    list_my_activities,
    list_paper_plane_conversations,
    list_paper_plane_messages,
    list_paper_planes,
    list_posts,
    list_report_reasons,
    list_topics,
    read_paper_plane_conversation,
    reply_paper_plane,
    send_paper_plane_message,
    set_current_city,
    signup_activity,
)
from app.services.idempotency import abort, complete, reserve_or_replay
from app.services.social import create_content_report

router = APIRouter(dependencies=[Depends(get_verified_user)])
ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)
logger = logging.getLogger(__name__)


async def _create_idempotently(
    db: AsyncSession,
    user_id: int,
    operation: str,
    idempotency_key: str | None,
    payload: dict[str, Any],
    response_model: type[ResponseModelT],
    creator: Callable[[bool], Awaitable[ResponseModelT]],
    on_completion_failure: Callable[[], Awaitable[None]] | None = None,
) -> ResponseModelT:
    if idempotency_key is None:
        return await creator(True)

    reservation = await reserve_or_replay(
        db,
        user_id,
        operation,
        idempotency_key,
        payload,
    )
    if reservation.response is not None:
        return response_model.model_validate(reservation.response)
    created = False
    try:
        response = await creator(False)
        created = True
        await complete(db, reservation, response.model_dump(mode="json"))
        return response
    except Exception:
        try:
            await db.rollback()
        except Exception:
            logger.exception("Failed to roll back idempotent create transaction")
        if created and on_completion_failure is not None:
            try:
                await on_completion_failure()
            except Exception:
                logger.exception("Failed to compensate after idempotent completion failure")
        try:
            await abort(db, reservation)
        except Exception:
            logger.exception("Failed to abort idempotency reservation")
        raise


@router.post(
    "/community/media/uploads",
    response_model=CommunityMediaResponse,
    status_code=201,
    summary="上传社区媒体",
)
async def upload_media(
    file: UploadFile = File(...),
    purpose: str = Form(...),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityMediaResponse:
    return await upload_community_media(db, current.id, file, purpose)


@router.delete(
    "/community/media/{media_id}",
    status_code=204,
    summary="删除未绑定社区媒体",
)
async def remove_media(
    media_id: int,
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await delete_community_media(db, current.id, media_id)


@router.post("/community/posts", response_model=CommunityPostResponse, status_code=201, summary="发布动态")
async def post(
    body: CommunityPostCreate,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityPostResponse:
    return await _create_idempotently(
        db,
        current.id,
        "community.post.create",
        idempotency_key,
        body.model_dump(mode="json"),
        CommunityPostResponse,
        lambda commit: create_post(db, current.id, body, commit=commit),
    )


@router.get("/community/posts", response_model=CommunityPostPage, summary="查看动态流")
async def feed(
    mode: Literal[
        "latest", "following", "city", "liked_users", "following_and_liked"
    ] = Query("latest"),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    city: str | None = Query(default=None, max_length=64),
    city_code: str | None = Query(
        default=None,
        max_length=6,
        pattern=CITY_CODE_PATTERN,
        description="市一级 city_code，只接受 4 或 6 位 ASCII 数字；4 位短码自动补 00",
    ),
    filter: Literal["all", "mbti", "alumni", "hometown", "hot", "latest"] | None = Query(default=None),
    sort: Literal["latest", "hot"] = Query("latest"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityPostPage:
    filter_key = None if filter in (None, "all", "hot", "latest") else filter
    sort_key: Literal["latest", "hot"] = "hot" if filter == "hot" or sort == "hot" else "latest"
    return await list_posts(
        db,
        current.id,
        mode=mode,
        page=page,
        page_size=page_size,
        city=city,
        city_code=city_code,
        filter_key=filter_key,
        sort=sort_key,
    )


@router.get("/community/posts/{post_id}", response_model=CommunityPostResponse, summary="查看动态详情")
async def post_detail(
    post_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityPostResponse:
    return await get_post(db, current.id, post_id)


@router.delete("/community/posts/{post_id}", status_code=204, summary="删除动态")
async def remove_post(
    post_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await delete_post(db, current.id, post_id)


@router.put("/community/posts/{post_id}/like", response_model=CommunityPostResponse, summary="点赞动态")
async def like(
    post_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityPostResponse:
    return await like_post(db, current.id, post_id, True)


@router.delete("/community/posts/{post_id}/like", response_model=CommunityPostResponse, summary="取消动态点赞")
async def unlike(
    post_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityPostResponse:
    return await like_post(db, current.id, post_id, False)


@router.put("/community/posts/{post_id}/collect", response_model=CommunityCollectResponse, summary="收藏动态")
async def collect(
    post_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityCollectResponse:
    return await collect_post(db, current.id, post_id, True)


@router.delete("/community/posts/{post_id}/collect", response_model=CommunityCollectResponse, summary="取消收藏动态")
async def uncollect(
    post_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityCollectResponse:
    return await collect_post(db, current.id, post_id, False)


@router.get("/community/posts/{post_id}/comments", response_model=list[CommunityCommentResponse], summary="查看动态评论")
async def comments(
    post_id: int = Path(..., ge=1),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CommunityCommentResponse]:
    return await list_comments(db, current.id, post_id, page, page_size)


@router.post(
    "/community/posts/{post_id}/comments",
    response_model=CommunityCommentResponse,
    status_code=201,
    summary="发表评论",
)
async def comment(
    post_id: int = Path(..., ge=1),
    body: CommunityCommentCreate = Body(...),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityCommentResponse:
    return await _create_idempotently(
        db,
        current.id,
        "community.comment.create",
        idempotency_key,
        {"post_id": post_id, "body": body.model_dump(mode="json")},
        CommunityCommentResponse,
        lambda commit: create_comment(db, current.id, post_id, body, commit=commit),
    )


@router.delete("/community/comments/{comment_id}", status_code=204, summary="删除评论")
async def remove_comment(
    comment_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await delete_comment(db, current.id, comment_id)


@router.put(
    "/community/comments/{comment_id}/like",
    response_model=CommunityCommentResponse,
    summary="点赞评论",
)
async def like_comment_route(
    comment_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityCommentResponse:
    return await like_comment(db, current.id, comment_id, True)


@router.delete(
    "/community/comments/{comment_id}/like",
    response_model=CommunityCommentResponse,
    summary="取消评论点赞",
)
async def unlike_comment_route(
    comment_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityCommentResponse:
    return await like_comment(db, current.id, comment_id, False)


@router.get("/community/topics", response_model=list[CommunityTopicResponse], summary="话题列表")
async def topics(
    sort: Literal["hot", "latest"] = Query("hot"),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=100),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CommunityTopicResponse]:
    page_data = await list_topics(db, current.id, sort=sort, page=page, page_size=page_size)
    return page_data.items


@router.get("/community/topics/page", response_model=CommunityTopicPage, summary="分页话题列表")
async def topics_page(
    sort: Literal["hot", "latest"] = Query("hot"),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    exclude_ids: list[int] | None = Query(default=None),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityTopicPage:
    return await list_topics(
        db,
        current.id,
        sort=sort,
        page=page,
        page_size=page_size,
        exclude_ids=exclude_ids,
    )


@router.get("/community/topics/{topic_id}", response_model=CommunityTopicResponse, summary="话题详情元信息")
async def topic_meta(
    topic_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityTopicResponse:
    return await get_topic(db, current.id, topic_id)


@router.get(
    "/community/topics/{topic_id}/detail",
    response_model=CommunityTopicDetailResponse,
    summary="话题详情与动态",
)
async def topic_detail(
    topic_id: int = Path(..., ge=1),
    sort: Literal["hot", "latest"] = Query("hot"),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityTopicDetailResponse:
    return await get_topic_detail(db, current.id, topic_id, sort=sort, page=page, page_size=page_size)


@router.post(
    "/community/topics/{topic_id}/join",
    response_model=CommunityTopicJoinResponse,
    summary="参与话题",
)
async def topic_join(
    topic_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityTopicJoinResponse:
    return await join_topic(db, current.id, topic_id)


@router.delete(
    "/community/topics/{topic_id}/leave",
    response_model=CommunityTopicJoinResponse,
    summary="取消参与话题",
)
async def topic_leave(
    topic_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityTopicJoinResponse:
    return await leave_topic(db, current.id, topic_id)


@router.get("/community/activities", response_model=ActivityPage, summary="线下活动列表")
async def activities(
    filter: Literal["all", "recruiting", "mine"] = Query("all"),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ActivityPage:
    return await list_activities(db, current.id, filter_key=filter, page=page, page_size=page_size)


@router.get("/community/activities/mine", response_model=ActivityPage, summary="我的活动")
async def my_activities(
    filter: Literal["all", "pending", "joined", "ended"] = Query("all"),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ActivityPage:
    return await list_my_activities(db, current.id, filter_key=filter, page=page, page_size=page_size)


@router.get("/community/activities/{activity_id}", response_model=ActivityResponse, summary="活动详情")
async def activity_detail(
    activity_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ActivityResponse:
    return await get_activity(db, current.id, activity_id)


@router.post(
    "/community/activities/{activity_id}/signup",
    response_model=ActivitySignupResponse,
    status_code=201,
    summary="报名活动",
)
async def activity_signup(
    activity_id: int = Path(..., ge=1),
    body: ActivitySignupCreate = Body(default=ActivitySignupCreate()),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> ActivitySignupResponse:
    return await signup_activity(db, current.id, activity_id, body)


@router.get("/community/banners", response_model=list[CommunityBannerResponse], summary="社区 Banner")
async def banners(
    position: str = Query("community", max_length=32),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CommunityBannerResponse]:
    return await list_banners(db, position=position)


@router.get("/community/quotas", response_model=CommunityQuotasResponse, summary="社区相关日额度")
async def quotas(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityQuotasResponse:
    return await get_community_quotas(db, current.id)


@router.get("/community/city", response_model=CommunityCityResponse, summary="当前同城城市")
async def city(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityCityResponse:
    return await get_current_city(db, current.id)


@router.put("/community/city", response_model=CommunityCityResponse, summary="设置同城城市")
async def update_city(
    body: CommunityCityUpdateRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityCityResponse:
    return await set_current_city(db, current.id, name=body.name, code=body.code)


@router.get("/community/report-reasons", response_model=list[CommunityReportReason], summary="举报原因列表")
async def report_reasons(current: CurrentUser = Depends(get_current_user)) -> list[CommunityReportReason]:
    return [CommunityReportReason(**item) for item in list_report_reasons()]


@router.post("/community/reports", response_model=CommunityReportResponse, status_code=201, summary="举报社区内容")
async def create_community_report(
    body: CommunityReportCreate,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CommunityReportResponse:
    report = await create_content_report(
        db,
        current.id,
        target_type=body.target_type,
        target_id=body.target_id,
        reason_id=body.reason_id,
        description=body.description,
        images=body.images,
    )
    return CommunityReportResponse(
        id=report.id,
        target_type=report.target_type,
        target_id=report.target_id,
        target_user_id=report.target_user_id,
        type=report.type,
        status=report.status,
        created_at=report.created_at,
    )


@router.post("/paper-planes", response_model=PaperPlaneResponse, status_code=201, summary="发送纸飞机")
async def send_plane(
    body: PaperPlaneCreate,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneResponse:
    quota_key = daily_quota_key("paper-plane", current.id)
    return await _create_idempotently(
        db,
        current.id,
        "community.paper_plane.create",
        idempotency_key,
        body.model_dump(mode="json"),
        PaperPlaneResponse,
        lambda commit: create_paper_plane(
            db,
            current.id,
            body,
            commit=commit,
            quota_key=quota_key,
        ),
        on_completion_failure=lambda: refund_daily(quota_key),
    )


@router.get("/paper-planes", response_model=list[PaperPlaneResponse], summary="捡纸飞机")
async def planes(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PaperPlaneResponse]:
    return await list_paper_planes(db, current.id, page, page_size)


@router.get("/paper-planes/mine", response_model=list[PaperPlaneResponse], summary="查看我的纸飞机")
async def my_planes(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PaperPlaneResponse]:
    return await list_paper_planes(db, current.id, page, page_size, own=True)


@router.post(
    "/paper-planes/{plane_id}/replies",
    response_model=PaperPlaneReplyResponse,
    status_code=201,
    summary="回复纸飞机",
)
async def reply(
    plane_id: int = Path(..., ge=1),
    body: PaperPlaneReplyCreate = Body(...),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneReplyResponse:
    return await _create_idempotently(
        db,
        current.id,
        "community.paper_plane_reply.create",
        idempotency_key,
        {"plane_id": plane_id, "body": body.model_dump(mode="json")},
        PaperPlaneReplyResponse,
        lambda commit: reply_paper_plane(db, current.id, plane_id, body, commit=commit),
    )


@router.get(
    "/paper-plane-conversations",
    response_model=list[PaperPlaneConversationResponse],
    summary="纸飞机匿名会话列表",
)
async def plane_conversations(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PaperPlaneConversationResponse]:
    return await list_paper_plane_conversations(db, current.id, page, page_size)


@router.get(
    "/paper-plane-conversations/{conversation_id}/messages",
    response_model=list[PaperPlaneMessageResponse],
    summary="纸飞机匿名会话消息",
)
async def plane_conversation_messages(
    conversation_id: int = Path(..., ge=1),
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(50, ge=1, le=100),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PaperPlaneMessageResponse]:
    return await list_paper_plane_messages(db, current.id, conversation_id, page, page_size)


@router.post(
    "/paper-plane-conversations/{conversation_id}/messages",
    response_model=PaperPlaneMessageResponse,
    status_code=201,
    summary="发送纸飞机匿名会话消息",
)
async def plane_conversation_send(
    conversation_id: int = Path(..., ge=1),
    body: PaperPlaneMessageCreate = Body(...),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneMessageResponse:
    return await _create_idempotently(
        db,
        current.id,
        "community.paper_plane_message.create",
        idempotency_key,
        {"conversation_id": conversation_id, "body": body.model_dump(mode="json")},
        PaperPlaneMessageResponse,
        lambda commit: send_paper_plane_message(
            db, current.id, conversation_id, body, commit=commit
        ),
    )


@router.post(
    "/paper-plane-conversations/{conversation_id}/read",
    response_model=PaperPlaneConversationResponse,
    summary="标记纸飞机会话已读",
)
async def plane_conversation_read(
    conversation_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneConversationResponse:
    return await read_paper_plane_conversation(db, current.id, conversation_id)


@router.post(
    "/paper-plane-conversations/{conversation_id}/end",
    response_model=PaperPlaneConversationResponse,
    summary="结束纸飞机匿名会话",
)
async def plane_conversation_end(
    conversation_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_realname_verified_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneConversationResponse:
    return await end_paper_plane_conversation(db, current.id, conversation_id)
