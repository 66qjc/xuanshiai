"""线下约见接口。"""

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, CurrentUser, get_current_matchmaker_admin, get_verified_user
from app.db.session import get_db
from app.schemas.meeting import (
    MeetingFeedbackCreate,
    MeetingRecordResponse,
    MeetingRequestCreate,
    MatchmakerMeetingRequestCreate,
    MeetingRequestResponse,
    MeetingScheduleCreate,
    MeetingStatusUpdate,
    MeetingRecordAdminPage,
    MeetingRequestAdminPage,
    MeetingRecordAdminUpdate,
    MeetingFeedbackAdminItem,
)
from app.services.meeting import (
    create_feedback,
    create_meeting_request,
    create_matchmaker_meeting_request,
    list_my_meeting_requests,
    schedule_meeting,
    update_meeting_request,
    admin_feedback,
    admin_get_meeting,
    admin_list_meetings,
    admin_list_requests,
    admin_update_meeting,
)

router = APIRouter(prefix="/matchmaker/meetings")
admin_router = APIRouter(prefix="/admin/matchmaker/meetings")


@router.post("/requests", response_model=MeetingRequestResponse, status_code=201, summary="提交约见申请")
async def create_request(body: MeetingRequestCreate = Body(...), current: CurrentUser = Depends(get_verified_user), db: AsyncSession = Depends(get_db)) -> MeetingRequestResponse:
    return await create_meeting_request(db, current, body)


@router.post("/requests/from-service", response_model=MeetingRequestResponse, status_code=201, summary="红娘基于服务单发起约见")
async def create_request_from_service(
    body: MatchmakerMeetingRequestCreate = Body(...),
    current: CurrentUser = Depends(get_verified_user),
    db: AsyncSession = Depends(get_db),
) -> MeetingRequestResponse:
    return await create_matchmaker_meeting_request(db, current, body)


@router.get("/requests/mine", response_model=list[MeetingRequestResponse], summary="查询我的约见申请")
async def mine_requests(current: CurrentUser = Depends(get_verified_user), db: AsyncSession = Depends(get_db)) -> list[MeetingRequestResponse]:
    return await list_my_meeting_requests(db, current)


@router.patch("/requests/{request_id}", response_model=MeetingRequestResponse, summary="处理约见申请")
async def update_request(request_id: int = Path(..., ge=1), body: MeetingStatusUpdate = Body(...), current: CurrentUser = Depends(get_verified_user), db: AsyncSession = Depends(get_db)) -> MeetingRequestResponse:
    return await update_meeting_request(db, current, request_id, body)


@router.post("/{meeting_id}/feedback", status_code=204, summary="提交约见反馈")
async def feedback(meeting_id: int = Path(..., ge=1), body: MeetingFeedbackCreate = Body(...), current: CurrentUser = Depends(get_verified_user), db: AsyncSession = Depends(get_db)) -> None:
    await create_feedback(db, current, meeting_id, body)


@admin_router.post("/requests/{request_id}/schedule", response_model=MeetingRecordResponse, status_code=201, summary="安排约会")
async def schedule(request_id: int = Path(..., ge=1), body: MeetingScheduleCreate = Body(...), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MeetingRecordResponse:
    admin.require("meeting.write")
    if admin.account.matchmaker_user_id is None:
        raise HTTPException(status_code=409, detail="当前后台账号未绑定红娘用户，不能安排约见")
    actor = CurrentUser(
        id=admin.account.matchmaker_user_id, session_id=admin.session_id, phone=None,
        status=1, realname_status=2,
    )
    return await schedule_meeting(db, actor, request_id, body)


@admin_router.get("/requests", response_model=MeetingRequestAdminPage)
async def admin_requests(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), status: str | None = Query(None, max_length=32), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MeetingRequestAdminPage:
    admin.require("meeting.read")
    return await admin_list_requests(db, page, page_size, status)


@admin_router.get("", response_model=MeetingRecordAdminPage)
async def admin_meetings(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), status: str | None = Query(None, max_length=32), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MeetingRecordAdminPage:
    admin.require("meeting.read")
    return await admin_list_meetings(db, page, page_size, status)


@admin_router.get("/{meeting_id}", response_model=MeetingRecordResponse)
async def admin_meeting_detail(meeting_id: int = Path(..., ge=1), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MeetingRecordResponse:
    admin.require("meeting.read")
    return await admin_get_meeting(db, meeting_id)


@admin_router.patch("/{meeting_id}", response_model=MeetingRecordResponse)
async def admin_meeting_update(meeting_id: int = Path(..., ge=1), body: MeetingRecordAdminUpdate = Body(...), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MeetingRecordResponse:
    current.require("meeting.write")
    return await admin_update_meeting(db, meeting_id, body, current.account.id)


@admin_router.get("/{meeting_id}/feedback", response_model=list[MeetingFeedbackAdminItem])
async def admin_meeting_feedback(meeting_id: int = Path(..., ge=1), admin: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[MeetingFeedbackAdminItem]:
    admin.require("meeting.feedback.read")
    return await admin_feedback(db, meeting_id)
