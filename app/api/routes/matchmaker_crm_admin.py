"""Member CRM endpoints protected by the independent matchmaker admin session."""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.matchmaker_crm_admin import MemberDetail, MemberListItem, MemberPage, MemberStatistics, MemberStatusResponse, MemberStatusUpdate

router = APIRouter(prefix="/admin/matchmaker")


async def _member_query(db: AsyncSession, where: str, params: dict, page: int, page_size: int) -> MemberPage:
    base = """FROM users u LEFT JOIN user_profile p ON p.user_id = u.id
        LEFT JOIN user_auth ua ON ua.user_id = u.id
        LEFT JOIN (SELECT user_id, MAX(end_at) AS vip_end_at FROM user_membership WHERE status = 1 GROUP BY user_id) v ON v.user_id = u.id
        LEFT JOIN (SELECT user_id, matchmaker_id FROM resource_assignment WHERE status = 1) a ON a.user_id = u.id
        LEFT JOIN (SELECT user_id, MAX(created_at) last_follow_at, MAX(next_follow_at) next_follow_at FROM member_follow_up GROUP BY user_id) f ON f.user_id = u.id"""
    params = {**params, "limit": page_size, "offset": (page - 1) * page_size}
    rows = await db.execute(text(f"""SELECT u.id, u.nickname, u.phone, u.gender, u.status, u.created_at,
        COALESCE(u.avatar, JSON_UNQUOTE(JSON_EXTRACT(p.photos, '$[0]'))) AS avatar,
        u.birthday, u.is_married, p.height, p.income, p.hometown, p.residence,
        ua.education, ua.job, ua.auth_status, f.last_follow_at, f.next_follow_at,
        v.vip_end_at, a.matchmaker_id, CASE WHEN v.user_id IS NULL OR (v.vip_end_at IS NOT NULL AND v.vip_end_at <= UTC_TIMESTAMP()) THEN 0 ELSE 1 END AS is_vip
        {base} WHERE {where} ORDER BY u.id DESC LIMIT :limit OFFSET :offset"""), params)
    count = await db.execute(text(f"SELECT COUNT(*) {base} WHERE {where}"), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = int(count.scalar() or 0)
    items = [MemberListItem(**dict(row)) for row in rows.mappings().all()]
    return MemberPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.get("/members", response_model=MemberPage, summary="查询会员 CRM 列表")
async def members(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), gender: int | None = Query(None, ge=1, le=2), status: int | None = Query(None, ge=1, le=3), vip: bool | None = Query(None), auth_status: int | None = Query(None, ge=0, le=3), assigned: bool | None = Query(None), follow_state: str | None = Query(None, pattern="^(never|due_today|overdue)$"), search: str | None = Query(None, max_length=64), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MemberPage:
    where = "1=1"
    params: dict = {}
    if gender is not None:
        where += " AND u.gender = :gender"
        params["gender"] = gender
    if status is not None:
        where += " AND u.status = :status"
        params["status"] = status
    if search:
        where += " AND (u.nickname LIKE CONCAT('%', :search, '%') OR u.phone LIKE CONCAT('%', :search, '%'))"
        params["search"] = search
    if vip is True:
        where += " AND v.user_id IS NOT NULL AND (v.vip_end_at IS NULL OR v.vip_end_at > UTC_TIMESTAMP())"
    if vip is False:
        where += " AND (v.user_id IS NULL OR (v.vip_end_at IS NOT NULL AND v.vip_end_at <= UTC_TIMESTAMP()))"
    if auth_status is not None:
        where += " AND COALESCE(ua.auth_status, 0) = :auth_status"
        params["auth_status"] = auth_status
    if assigned is True:
        where += " AND a.matchmaker_id IS NOT NULL"
    if assigned is False:
        where += " AND a.matchmaker_id IS NULL"
    if follow_state == "never":
        where += " AND f.last_follow_at IS NULL"
    if follow_state == "due_today":
        where += " AND f.next_follow_at >= CURDATE() AND f.next_follow_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)"
    if follow_state == "overdue":
        where += " AND f.next_follow_at < CURDATE()"
    return await _member_query(db, where, params, page, page_size)


@router.get("/members/statistics", response_model=MemberStatistics, summary="查询会员统计")
async def member_statistics(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MemberStatistics:
    row = (await db.execute(text("""SELECT COUNT(*) total, SUM(u.gender = 1) male, SUM(u.gender = 2) female,
        SUM(u.status = 1) active,
        SUM(a.matchmaker_id IS NULL) unassigned,
        SUM(f.last_follow_at IS NULL) never_followed,
        SUM(f.next_follow_at >= CURDATE() AND f.next_follow_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)) follow_due_today,
        (SELECT COUNT(DISTINCT user_id) FROM user_membership WHERE status = 1 AND (end_at IS NULL OR end_at > UTC_TIMESTAMP())) vip
        FROM users u
        LEFT JOIN (SELECT user_id, matchmaker_id FROM resource_assignment WHERE status = 1) a ON a.user_id = u.id
        LEFT JOIN (SELECT user_id, MAX(created_at) last_follow_at, MAX(next_follow_at) next_follow_at FROM member_follow_up GROUP BY user_id) f ON f.user_id = u.id"""))).mappings().one()
    return MemberStatistics(**{key: int(row[key] or 0) for key in ("total", "male", "female", "vip", "active", "unassigned", "never_followed", "follow_due_today")})


@router.get("/members/{member_id}", response_model=MemberDetail, summary="查询会员详情")
async def member_detail(member_id: int = Path(..., ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MemberDetail:
    row = (await db.execute(text("""SELECT u.id, u.nickname, u.phone, u.gender, u.status, u.avatar, u.birthday, u.is_married, u.created_at,
        p.residence_city_code, v.vip_end_at, a.matchmaker_id,
        CASE WHEN v.user_id IS NULL OR (v.vip_end_at IS NOT NULL AND v.vip_end_at <= UTC_TIMESTAMP()) THEN 0 ELSE 1 END AS is_vip
        FROM users u LEFT JOIN user_profile p ON p.user_id = u.id
        LEFT JOIN (SELECT user_id, MAX(end_at) vip_end_at FROM user_membership WHERE status = 1 GROUP BY user_id) v ON v.user_id = u.id
        LEFT JOIN (SELECT user_id, matchmaker_id FROM resource_assignment WHERE status = 1) a ON a.user_id = u.id WHERE u.id = :id"""), {"id": member_id})).mappings().first()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, detail="会员不存在")
    return MemberDetail(**dict(row))


@router.patch("/members/{member_id}/status", response_model=MemberStatusResponse, summary="修改会员状态")
async def member_status(member_id: int = Path(..., ge=1), body: MemberStatusUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MemberStatusResponse:
    result = await db.execute(text("SELECT id FROM users WHERE id = :id FOR UPDATE"), {"id": member_id})
    if not result.scalar():
        from fastapi import HTTPException
        raise HTTPException(404, detail="会员不存在")
    await db.execute(text("UPDATE users SET status = :status, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"status": body.status, "id": member_id})
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id, reason) VALUES (:actor, 'member.status.update', 'user', :id, :reason)"), {"actor": current.account.id, "id": member_id, "reason": body.reason})
    await db.commit()
    return MemberStatusResponse(id=member_id, status=body.status, reason=body.reason)
