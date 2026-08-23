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
    MeetingRecordAdminPage,
    MeetingRequestAdminPage,
    MeetingRecordAdminUpdate,
    MeetingFeedbackAdminItem,
)
from app.services.social import ensure_users_can_interact


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
    await ensure_users_can_interact(db, int(service["user_id"]), request.target_user_id)
    await ensure_users_can_interact(db, current.id, request.target_user_id)
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


async def admin_list_requests(db: AsyncSession, page: int, page_size: int, status: str | None = None) -> MeetingRequestAdminPage:
    where = ["1 = 1"]
    params: dict[str, object] = {"limit": page_size, "offset": (page - 1) * page_size}
    if status:
        where.append("status = :status")
        params["status"] = status
    clause = " AND ".join(where)
    rows = await db.execute(text(f"""SELECT id, user_id, target_user_id, matchmaker_id,
        service_id, organization_id, status, note, created_at, updated_at
        FROM meeting_request WHERE {clause} ORDER BY id DESC LIMIT :limit OFFSET :offset"""), params)
    total = int((await db.execute(text(f"SELECT COUNT(*) FROM meeting_request WHERE {clause}"),
        {key: value for key, value in params.items() if key not in ("limit", "offset")})).scalar() or 0)
    return MeetingRequestAdminPage(items=[_request_response(row) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def admin_list_meetings(db: AsyncSession, page: int, page_size: int, status: str | None = None) -> MeetingRecordAdminPage:
    where = ["1 = 1"]
    params: dict[str, object] = {"limit": page_size, "offset": (page - 1) * page_size}
    if status:
        where.append("status = :status")
        params["status"] = status
    clause = " AND ".join(where)
    rows = await db.execute(text(f"""SELECT id, request_id, organizer_id, organization_id,
        scheduled_at, location, status, cancel_reason, created_at, updated_at
        FROM meeting_record WHERE {clause} ORDER BY scheduled_at DESC, id DESC
        LIMIT :limit OFFSET :offset"""), params)
    total = int((await db.execute(text(f"SELECT COUNT(*) FROM meeting_record WHERE {clause}"),
        {key: value for key, value in params.items() if key not in ("limit", "offset")})).scalar() or 0)
    return MeetingRecordAdminPage(items=[_record_response(row) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


async def admin_get_meeting(db: AsyncSession, meeting_id: int) -> MeetingRecordResponse:
    row = (await db.execute(text("""SELECT id, request_id, organizer_id, organization_id,
        scheduled_at, location, status, cancel_reason, created_at, updated_at
        FROM meeting_record WHERE id = :id"""), {"id": meeting_id})).mappings().first()
    if not row:
        raise HTTPException(404, detail="约见记录不存在")
    return _record_response(row)


async def admin_update_meeting(db: AsyncSession, meeting_id: int, body: MeetingRecordAdminUpdate, actor_id: int) -> MeetingRecordResponse:
    current = await admin_get_meeting(db, meeting_id)
    values = body.model_dump(exclude_unset=True)
    if current.status == "CANCELLED" and values.get("status") not in (None, "CANCELLED"):
        raise HTTPException(409, detail="已取消的约见不能恢复")
    if values.get("status") == "CANCELLED" and not values.get("cancel_reason") and not current.cancel_reason:
        raise HTTPException(422, detail="取消约见必须填写原因")
    if values:
        updates = ", ".join(f"{key} = :{key}" for key in values)
        await db.execute(text(f"UPDATE meeting_record SET {updates}, updated_at = UTC_TIMESTAMP() WHERE id = :id"),
            {**values, "id": meeting_id})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id)
        VALUES (:actor, 'meeting.update', 'meeting_record', :id)"""), {"actor": actor_id, "id": meeting_id})
    await db.commit()
    return await admin_get_meeting(db, meeting_id)


async def admin_feedback(db: AsyncSession, meeting_id: int) -> list[MeetingFeedbackAdminItem]:
    await admin_get_meeting(db, meeting_id)
    rows = await db.execute(text("""SELECT id, meeting_id, user_id, target_rating,
        matchmaker_rating, continue_intent, private_feedback, created_at
        FROM meeting_feedback WHERE meeting_id = :id ORDER BY id ASC"""), {"id": meeting_id})
    return [MeetingFeedbackAdminItem(**dict(row)) for row in rows.mappings().all()]
