"""约见申请、安排和私有反馈服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser
from app.schemas.meeting import (
    MeetingFeedbackCreate,
    MeetingRecordResponse,
    MeetingRequestCreate,
    MatchmakerMeetingRequestCreate,
    MeetingRequestResponse,
    MeetingScheduleCreate,
    MeetingStatusUpdate,
)


def _dt(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _request_response(row: Any) -> MeetingRequestResponse:
    return MeetingRequestResponse(**{**dict(row), "created_at": _dt(row["created_at"]), "updated_at": _dt(row["updated_at"])})


def _record_response(row: Any) -> MeetingRecordResponse:
    return MeetingRecordResponse(**{**dict(row), "scheduled_at": _dt(row["scheduled_at"]), "created_at": _dt(row["created_at"]), "updated_at": _dt(row["updated_at"])})


async def create_meeting_request(db: AsyncSession, current: CurrentUser, request: MeetingRequestCreate) -> MeetingRequestResponse:
    raise HTTPException(403, detail="普通用户不能直接发起约见，请通过已购买的红娘服务联系红娘")


async def create_matchmaker_meeting_request(
    db: AsyncSession, current: CurrentUser, request: MatchmakerMeetingRequestCreate
) -> MeetingRequestResponse:
    service_result = await db.execute(text("""SELECT id, user_id, status FROM matchmaker_service
        WHERE id = :service_id AND matchmaker_id = :matchmaker_id FOR UPDATE"""), {
        "service_id": request.service_id, "matchmaker_id": current.id,
    })
    service = service_result.mappings().first()
    if not service:
        raise HTTPException(404, detail="服务单不存在或不属于当前红娘")
    if service["status"] not in (1, 2):
        raise HTTPException(409, detail="只有服务中或服务完成的红娘服务才能发起约见")
    if request.target_user_id == service["user_id"]:
        raise HTTPException(422, detail="不能将服务用户作为约见对象")
    target = await db.execute(text("SELECT id FROM users WHERE id = :id AND status = 1"), {"id": request.target_user_id})
    if not target.scalar():
        raise HTTPException(404, detail="约见对象不存在或不可用")
    duplicate = await db.execute(text("""SELECT id FROM meeting_request
        WHERE user_id = :user_id AND target_user_id = :target_id
          AND status IN ('SUBMITTED', 'CONTACTED', 'ACCEPTED') LIMIT 1"""), {
        "user_id": service["user_id"], "target_id": request.target_user_id,
    })
    if duplicate.scalar():
        raise HTTPException(409, detail="已有处理中约见申请")
    result = await db.execute(text("""INSERT INTO meeting_request
        (user_id, target_user_id, matchmaker_id, service_id, note)
        VALUES (:user_id, :target_id, :matchmaker_id, :service_id, :note)"""), {
        "user_id": service["user_id"], "target_id": request.target_user_id,
        "matchmaker_id": current.id, "service_id": request.service_id, "note": request.note,
    })
    request_id = int(result.lastrowid)
    await db.commit()
    result = await db.execute(text("""SELECT id, user_id, target_user_id, matchmaker_id,
        service_id, organization_id, status, note, created_at, updated_at
        FROM meeting_request WHERE id = :id"""), {"id": request_id})
    return _request_response(result.mappings().one())


async def list_my_meeting_requests(db: AsyncSession, current: CurrentUser) -> list[MeetingRequestResponse]:
    result = await db.execute(text("""SELECT id, user_id, target_user_id, matchmaker_id,
        service_id, organization_id, status, note, created_at, updated_at FROM meeting_request
        WHERE user_id = :user_id OR target_user_id = :user_id ORDER BY created_at DESC, id DESC"""), {"user_id": current.id})
    return [_request_response(row) for row in result.mappings().all()]


async def update_meeting_request(db: AsyncSession, current: CurrentUser, request_id: int, request: MeetingStatusUpdate) -> MeetingRequestResponse:
    result = await db.execute(text("""SELECT id, user_id, target_user_id, matchmaker_id,
        service_id, organization_id, status, note, created_at, updated_at FROM meeting_request
        WHERE id = :id FOR UPDATE"""), {"id": request_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="约见申请不存在")
    if current.id not in (row["user_id"], row["target_user_id"]):
        raise HTTPException(403, detail="无权处理该约见申请")
    if row["status"] not in ("SUBMITTED", "CONTACTED", "ACCEPTED"):
        raise HTTPException(409, detail="当前约见申请状态不能修改")
    if request.status in ("DECLINED", "CLOSED") and not request.reason:
        raise HTTPException(422, detail="拒绝或关闭约见申请必须填写原因")
    await db.execute(text("UPDATE meeting_request SET status = :status, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {
        "status": request.status, "id": request_id,
    })
    await db.commit()
    result = await db.execute(text("""SELECT id, user_id, target_user_id, matchmaker_id,
        service_id, organization_id, status, note, created_at, updated_at FROM meeting_request WHERE id = :id"""), {"id": request_id})
    return _request_response(result.mappings().one())


async def schedule_meeting(db: AsyncSession, admin: CurrentUser, request_id: int, request: MeetingScheduleCreate) -> MeetingRecordResponse:
    result = await db.execute(text("""SELECT id, user_id, target_user_id, status FROM meeting_request
        WHERE id = :id FOR UPDATE"""), {"id": request_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="约见申请不存在")
    if row["status"] != "ACCEPTED":
        raise HTTPException(409, detail="只有双方接受的约见申请才能安排约会")
    result = await db.execute(text("""INSERT INTO meeting_record
        (request_id, organizer_id, organization_id, scheduled_at, location)
        VALUES (:request_id, :organizer_id, :organization_id, :scheduled_at, :location)"""), {
        "request_id": request_id, "organizer_id": request.organizer_id,
        "organization_id": request.organization_id, "scheduled_at": request.scheduled_at,
        "location": request.location,
    })
    meeting_id = int(result.lastrowid)
    await db.execute(text("UPDATE meeting_request SET status = 'ACCEPTED', updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": request_id})
    await db.commit()
    result = await db.execute(text("""SELECT id, request_id, organizer_id, organization_id,
        scheduled_at, location, status, cancel_reason, created_at, updated_at
        FROM meeting_record WHERE id = :id"""), {"id": meeting_id})
    return _record_response(result.mappings().one())


async def create_feedback(db: AsyncSession, current: CurrentUser, meeting_id: int, request: MeetingFeedbackCreate) -> None:
    result = await db.execute(text("""SELECT mr.id, rq.user_id, rq.target_user_id, mr.status
        FROM meeting_record mr JOIN meeting_request rq ON rq.id = mr.request_id
        WHERE mr.id = :id"""), {"id": meeting_id})
    row = result.mappings().first()
    if not row or current.id not in (row["user_id"], row["target_user_id"]):
        raise HTTPException(404, detail="约会记录不存在或无权反馈")
    if row["status"] not in ("COMPLETED", "CHECKED_IN"):
        raise HTTPException(409, detail="约会尚未完成，暂不能反馈")
    await db.execute(text("""INSERT INTO meeting_feedback
        (meeting_id, user_id, target_rating, matchmaker_rating, continue_intent, private_feedback)
        VALUES (:meeting_id, :user_id, :target_rating, :matchmaker_rating, :continue_intent, :private_feedback)"""), {
        "meeting_id": meeting_id, "user_id": current.id, "target_rating": request.target_rating,
        "matchmaker_rating": request.matchmaker_rating, "continue_intent": request.continue_intent,
        "private_feedback": request.private_feedback,
    })
    await db.commit()
