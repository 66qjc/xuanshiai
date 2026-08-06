"""Read-only dashboard endpoints for the independent matchmaker back office."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.matchmaker_dashboard_admin import MatchmakerDashboardStats

router = APIRouter(prefix="/admin/dashboard")


@router.get("/stats", response_model=MatchmakerDashboardStats, summary="查询红娘后台首页统计")
async def dashboard_stats(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerDashboardStats:
    row = (await db.execute(text("""SELECT
        (SELECT COUNT(*) FROM users) AS member_count,
        (SELECT COUNT(DISTINCT user_id) FROM user_membership WHERE status = 1 AND (end_at IS NULL OR end_at > UTC_TIMESTAMP())) AS vip_count,
        (SELECT COUNT(*) FROM user_matchmaker_apply a JOIN user_role r ON r.user_id = a.user_id AND r.role_code = 'service_matchmaker' AND r.status = 1 WHERE a.application_type = 'service_matchmaker' AND a.status = 1) AS matchmaker_count,
        (SELECT COUNT(*) FROM matchmaker_service WHERE status = 0) AS pending_service_count,
        (SELECT COUNT(*) FROM matchmaker_service WHERE status = 1) AS active_service_count,
        (SELECT COUNT(*) FROM user_matchmaker_apply WHERE application_type = 'service_matchmaker' AND status = 0) AS pending_certification_count,
        (SELECT COUNT(*) FROM users WHERE created_at >= CURDATE()) AS today_new_member_count"""))).mappings().one()
    return MatchmakerDashboardStats(**{key: int(row[key] or 0) for key in MatchmakerDashboardStats.model_fields})

