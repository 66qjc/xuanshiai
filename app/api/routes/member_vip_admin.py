"""Member VIP and login history queries for the independent back office."""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.member_vip_admin import AdminLoginLogItem, AdminLoginLogPage, AdminVipItem, AdminVipPage, AdminVipUpdate, AdminVipUpdateResponse
from app.services.member_vip_admin import update_vip

router = APIRouter(prefix="/admin/members")


@router.patch("/{member_id}/vip", response_model=AdminVipUpdateResponse, summary="开通、续期或取消 VIP")
async def update_member_vip(member_id: int = Path(..., ge=1), body: AdminVipUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> AdminVipUpdateResponse:
    return await update_vip(db, current.account.id, member_id, body)


@router.get("/vip", response_model=AdminVipPage, summary="查询 VIP 会员")
async def vip_members(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), expiring_within_days: int | None = Query(None, ge=1, le=3650), search: str | None = Query(None, max_length=64), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> AdminVipPage:
    where = ["m.status = 1", "(m.end_at IS NULL OR m.end_at > UTC_TIMESTAMP())"]
    params: dict = {"limit": page_size, "offset": (page - 1) * page_size}
    if expiring_within_days is not None:
        where.append("m.end_at IS NOT NULL AND m.end_at <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL :days DAY)"); params["days"] = expiring_within_days
    if search:
        where.append("(u.nickname LIKE CONCAT('%', :search, '%') OR u.phone LIKE CONCAT('%', :search, '%'))"); params["search"] = search
    clause = " AND ".join(where)
    base = "FROM user_membership m JOIN users u ON u.id = m.user_id"
    rows = await db.execute(text(f"SELECT m.id membership_id, m.user_id, u.nickname, u.phone, m.package_type, m.amount, m.order_no, m.start_at, m.end_at, m.status {base} WHERE {clause} ORDER BY m.end_at IS NULL, m.end_at ASC, m.id DESC LIMIT :limit OFFSET :offset"), params)
    count = await db.execute(text(f"SELECT COUNT(*) {base} WHERE {clause}"), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = int(count.scalar() or 0)
    return AdminVipPage(items=[AdminVipItem(**dict(row)) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.get("/{member_id}/behavior/login-logs", response_model=AdminLoginLogPage, summary="查询会员登录日志")
async def login_logs(member_id: int = Path(..., ge=1), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> AdminLoginLogPage:
    base = "FROM user_login_log l JOIN users u ON u.id = l.user_id"
    params = {"user_id": member_id, "limit": page_size, "offset": (page - 1) * page_size}
    rows = await db.execute(text(f"SELECT l.id, l.user_id, u.nickname, l.login_status, l.ip, l.device_id, l.platform, l.failure_reason, l.created_at {base} WHERE l.user_id = :user_id ORDER BY l.id DESC LIMIT :limit OFFSET :offset"), params)
    count = await db.execute(text(f"SELECT COUNT(*) {base} WHERE l.user_id = :user_id"), {"user_id": member_id})
    total = int(count.scalar() or 0)
    return AdminLoginLogPage(items=[AdminLoginLogItem(**dict(row)) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)
