"""Activity and signup management for the independent back office."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.activity_admin import ActivityAdminCreate, ActivityAdminItem, ActivityAdminPage, ActivityAdminUpdate, ActivitySignupAdminItem, ActivitySignupAdminPage, ActivitySignupStatusUpdate, ActivityStatusUpdate

router = APIRouter(prefix="/admin/activities")

SELECT_ACTIVITY = "SELECT id, title, cover, type, city, address, start_time, end_time, signup_deadline, max_people, current_people, price, status, description, created_by, created_at FROM offline_activity"


async def _get(db: AsyncSession, activity_id: int) -> ActivityAdminItem:
    row = (await db.execute(text(f"{SELECT_ACTIVITY} WHERE id = :id"), {"id": activity_id})).mappings().first()
    if not row:
        raise HTTPException(404, detail="活动不存在")
    return ActivityAdminItem(**dict(row))


@router.get("", response_model=ActivityAdminPage, summary="查询活动列表")
async def activities(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), status: int | None = Query(None, ge=1, le=5), city: str | None = Query(None, max_length=64), search: str | None = Query(None, max_length=128), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivityAdminPage:
    where = ["1=1"]
    params: dict = {"limit": page_size, "offset": (page - 1) * page_size}
    if status is not None:
        where.append("status = :status")
        params["status"] = status
    if city:
        where.append("city = :city")
        params["city"] = city
    if search:
        where.append("title LIKE CONCAT('%', :search, '%')")
        params["search"] = search
    clause = " AND ".join(where)
    rows = await db.execute(text(f"{SELECT_ACTIVITY} WHERE {clause} ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)
    count = await db.execute(text(f"SELECT COUNT(*) FROM offline_activity WHERE {clause}"), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = int(count.scalar() or 0)
    return ActivityAdminPage(items=[ActivityAdminItem(**dict(row)) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.post("", response_model=ActivityAdminItem, status_code=201, summary="创建活动")
async def create_activity(body: ActivityAdminCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivityAdminItem:
    result = await db.execute(text("""INSERT INTO offline_activity (title, cover, type, city, address, start_time, end_time, signup_deadline, max_people, price, description, status, created_by)
        VALUES (:title, :cover, :type, :city, :address, :start_time, :end_time, :signup_deadline, :max_people, :price, :description, 1, :created_by)"""), {**body.model_dump(), "created_by": current.account.id})
    await db.commit()
    return await _get(db, int(result.lastrowid))


@router.patch("/{activity_id}/status", response_model=ActivityAdminItem, summary="修改活动状态")
async def update_activity_status(activity_id: int = Path(..., ge=1), body: ActivityStatusUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivityAdminItem:
    await _get(db, activity_id)
    await db.execute(text("UPDATE offline_activity SET status = :status, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"status": body.status, "id": activity_id})
    await db.commit()
    return await _get(db, activity_id)


@router.get("/{activity_id}/signups", response_model=ActivitySignupAdminPage, summary="查询活动报名")
async def signups(activity_id: int = Path(..., ge=1), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), status: int | None = Query(None, ge=0, le=3), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivitySignupAdminPage:
    await _get(db, activity_id)
    where = ["s.activity_id = :activity_id"]
    params: dict = {"activity_id": activity_id, "limit": page_size, "offset": (page - 1) * page_size}
    if status is not None:
        where.append("s.status = :status")
        params["status"] = status
    clause = " AND ".join(where)
    base = "FROM activity_signup s LEFT JOIN users u ON u.id = s.user_id"
    rows = await db.execute(text(f"SELECT s.id, s.activity_id, s.user_id, u.nickname, s.real_name, s.phone, s.remark, s.status, s.cancel_reason, s.created_at, s.updated_at {base} WHERE {clause} ORDER BY s.id DESC LIMIT :limit OFFSET :offset"), params)
    count = await db.execute(text(f"SELECT COUNT(*) {base} WHERE {clause}"), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = int(count.scalar() or 0)
    return ActivitySignupAdminPage(items=[ActivitySignupAdminItem(**dict(row)) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.patch("/{activity_id}", response_model=ActivityAdminItem, summary="修改活动")
async def update_activity(activity_id: int = Path(..., ge=1), body: ActivityAdminUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivityAdminItem:
    await _get(db, activity_id)
    values = body.model_dump(exclude_unset=True)
    updates = ", ".join(f"{key} = :{key}" for key in values)
    await db.execute(text(f"UPDATE offline_activity SET {updates}, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {**values, "id": activity_id})
    await db.commit()
    return await _get(db, activity_id)


@router.get("/{activity_id}", response_model=ActivityAdminItem, summary="查询活动详情")
async def activity_detail(activity_id: int = Path(..., ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivityAdminItem:
    return await _get(db, activity_id)


signup_router = APIRouter(prefix="/admin/activity-signups")


@signup_router.get("/{signup_id}", response_model=ActivitySignupAdminItem, summary="查询活动报名详情")
async def signup_detail(signup_id: int = Path(..., ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivitySignupAdminItem:
    row = (await db.execute(text("SELECT s.id, s.activity_id, s.user_id, u.nickname, s.real_name, s.phone, s.remark, s.status, s.cancel_reason, s.created_at, s.updated_at FROM activity_signup s LEFT JOIN users u ON u.id = s.user_id WHERE s.id = :id"), {"id": signup_id})).mappings().first()
    if not row:
        raise HTTPException(404, detail="活动报名不存在")
    return ActivitySignupAdminItem(**dict(row))


@signup_router.patch("/{signup_id}", response_model=ActivitySignupAdminItem, summary="审核活动报名")
async def update_signup(signup_id: int = Path(..., ge=1), body: ActivitySignupStatusUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ActivitySignupAdminItem:
    current_row = (await db.execute(text("SELECT activity_id, status FROM activity_signup WHERE id = :id FOR UPDATE"), {"id": signup_id})).mappings().first()
    if not current_row:
        raise HTTPException(404, detail="活动报名不存在")
    if int(current_row["status"]) in (2, 3):
        raise HTTPException(409, detail="已取消或已拒绝的报名不能再次审核")
    await db.execute(text("UPDATE activity_signup SET status = :status, cancel_reason = :reason, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"status": body.status, "reason": body.reason, "id": signup_id})
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id, reason) VALUES (:actor, 'activity_signup.review', 'activity_signup', :id, :reason)"), {"actor": current.account.id, "id": signup_id, "reason": body.reason})
    await db.commit()
    row = (await db.execute(text("SELECT s.id, s.activity_id, s.user_id, u.nickname, s.real_name, s.phone, s.remark, s.status, s.cancel_reason, s.created_at, s.updated_at FROM activity_signup s LEFT JOIN users u ON u.id = s.user_id WHERE s.id = :id"), {"id": signup_id})).mappings().one()
    return ActivitySignupAdminItem(**dict(row))
