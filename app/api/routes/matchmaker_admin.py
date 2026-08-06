"""Independent matchmaker back-office authentication routes."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.matchmaker_admin import (
    MatchmakerAdminLoginRequest, MatchmakerAdminMeResponse, MatchmakerAdminRefreshRequest,
    MatchmakerAdminTokenResponse, MatchmakerStatistics, MatchmakerStatusResponse,
    MatchmakerStatusUpdate, ResourceAssignmentPage,
)
from app.schemas.matchmaker import (
    MatchmakerPage, MatchmakerServiceProductCreate,
    MatchmakerServiceProductResponse, MatchmakerServiceProductUpdate, MatchmakerServiceRequestPage,
    MatchmakerServiceRequestResponse, MatchmakerCard, MatchmakerAdminServiceRequestUpdate,
)
from app.services.matchmaker_admin_auth import login, logout, refresh
from app.services.matchmaker import (
    admin_create_service_product, admin_list_service_requests, admin_update_service_product,
    admin_update_service_request, get_matchmaker, get_service_product, list_matchmakers,
    list_service_products,
)
from app.schemas.organization import ResourceAssignmentCreate, ResourceAssignmentResponse, StoreCreate, StoreMemberCreate, StoreMemberResponse, StoreResponse
from app.schemas.meeting import MeetingRecordResponse, MeetingScheduleCreate
from app.services.organization import add_store_member, assign_resource, create_store, get_store, list_stores
from app.services.meeting import schedule_meeting
from app.api.dependencies import CurrentUser
from sqlalchemy import text

router = APIRouter(prefix="/admin/matchmaker")


@router.post("/auth/login", response_model=MatchmakerAdminTokenResponse, summary="红娘后台账号密码登录")
async def admin_login(request: Request, body: MatchmakerAdminLoginRequest, db: AsyncSession = Depends(get_db)) -> MatchmakerAdminTokenResponse:
    return await login(db, body, request.client.host if request.client else None, request.headers.get("user-agent"))


@router.post("/auth/refresh", response_model=MatchmakerAdminTokenResponse, summary="刷新红娘后台令牌")
async def admin_refresh(request: Request, body: MatchmakerAdminRefreshRequest, db: AsyncSession = Depends(get_db)) -> MatchmakerAdminTokenResponse:
    return await refresh(db, body, request.client.host if request.client else None, request.headers.get("user-agent"))


@router.get("/auth/me", response_model=MatchmakerAdminMeResponse, summary="查询当前红娘后台账号")
async def admin_me(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin)) -> MatchmakerAdminMeResponse:
    return MatchmakerAdminMeResponse(account=current.account, permissions=["matchmaker:read", "matchmaker:service:write"])


@router.post("/auth/logout", status_code=204, summary="退出红娘后台")
async def admin_logout(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> None:
    await logout(db, current.session_id)


@router.get("/matchmakers", response_model=MatchmakerPage, summary="查询红娘列表")
async def matchmakers(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerPage:
    return await list_matchmakers(db, page, page_size)


@router.patch("/matchmakers/{matchmaker_id}/status", response_model=MatchmakerStatusResponse, summary="停用或恢复红娘接单")
async def update_matchmaker_status(matchmaker_id: int = Path(..., ge=1), body: MatchmakerStatusUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerStatusResponse:
    result = await db.execute(text("SELECT id, status FROM user_matchmaker_apply WHERE user_id = :id AND application_type = 'service_matchmaker' FOR UPDATE"), {"id": matchmaker_id})
    row = result.mappings().first()
    if not row or int(row["status"]) != 1:
        raise HTTPException(404, detail="有效服务红娘不存在")
    await db.execute(text("UPDATE user_role SET status = :status WHERE user_id = :id AND role_code = 'service_matchmaker'"), {"status": body.status, "id": matchmaker_id})
    await db.execute(text("UPDATE user_matchmaker_apply SET suspended_at = CASE WHEN :status = 2 THEN UTC_TIMESTAMP() ELSE NULL END, suspension_reason = :reason, updated_at = UTC_TIMESTAMP() WHERE id = :apply_id"), {"status": body.status, "reason": body.reason, "apply_id": row["id"]})
    await db.commit()
    return MatchmakerStatusResponse(matchmaker_id=matchmaker_id, status=body.status, reason=body.reason)


@router.get("/matchmakers/{matchmaker_id}", response_model=MatchmakerCard, summary="查询红娘详情")
async def matchmaker_detail(matchmaker_id: int = Path(..., ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerCard:
    return await get_matchmaker(db, matchmaker_id)


@router.get("/service-products", response_model=list[MatchmakerServiceProductResponse], summary="查询红娘服务商品")
async def products(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[MatchmakerServiceProductResponse]:
    return await list_service_products(db)


@router.post("/service-products", response_model=MatchmakerServiceProductResponse, status_code=201, summary="创建红娘服务商品")
async def create_product(body: MatchmakerServiceProductCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerServiceProductResponse:
    return await admin_create_service_product(db, current.account.id, body)


@router.get("/service-products/{product_id}", response_model=MatchmakerServiceProductResponse, summary="查询红娘服务商品详情")
async def product_detail(product_id: int = Path(..., ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerServiceProductResponse:
    return await get_service_product(db, product_id)


@router.patch("/service-products/{product_id}", response_model=MatchmakerServiceProductResponse, summary="修改或下架红娘服务商品")
async def update_product(product_id: int = Path(..., ge=1), body: MatchmakerServiceProductUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerServiceProductResponse:
    return await admin_update_service_product(db, product_id, body)


@router.get("/service-requests", response_model=MatchmakerServiceRequestPage, summary="查询红娘服务申请")
async def service_requests(status: int | None = Query(None, ge=0, le=3), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerServiceRequestPage:
    return await admin_list_service_requests(db, page, page_size, status)


@router.patch("/service-requests/{service_id}", response_model=MatchmakerServiceRequestResponse, summary="分配或处理红娘服务申请")
async def update_service_request(service_id: int = Path(..., ge=1), body: MatchmakerAdminServiceRequestUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerServiceRequestResponse:
    return await admin_update_service_request(db, current.account.id, service_id, body)


def _legacy_actor(current: CurrentMatchmakerAdmin) -> CurrentUser:
    """Adapt the existing actor-only service signatures without authenticating as a user."""
    return CurrentUser(id=current.account.id, session_id=current.session_id, phone=None, status=1, realname_status=2)


@router.get("/statistics", response_model=MatchmakerStatistics, summary="查询红娘后台统计")
async def statistics(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MatchmakerStatistics:
    result = await db.execute(text("""SELECT
        (SELECT COUNT(*) FROM user_matchmaker_apply WHERE application_type = 'service_matchmaker' AND status IN (1, 2, 3)) AS total,
        (SELECT COUNT(*) FROM user_matchmaker_apply a JOIN user_role r ON r.user_id = a.user_id AND r.role_code = 'service_matchmaker' AND r.status = 1 WHERE a.application_type = 'service_matchmaker' AND a.status = 1) AS available,
        (SELECT COUNT(*) FROM matchmaker_service WHERE status = 0) AS pending_services,
        (SELECT COUNT(*) FROM matchmaker_service WHERE status = 1) AS active_services,
        (SELECT COUNT(*) FROM matchmaker_service WHERE status = 2) AS completed_services,
        (SELECT COUNT(*) FROM matchmaker_service WHERE status = 3) AS cancelled_services"""))
    return MatchmakerStatistics(**dict(result.mappings().one()))


@router.get("/branches", response_model=list[StoreResponse], summary="查询门店")
async def branches(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[StoreResponse]:
    return await list_stores(db)


@router.post("/branches", response_model=StoreResponse, status_code=201, summary="创建门店")
async def create_branch(body: StoreCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> StoreResponse:
    return await create_store(db, _legacy_actor(current), body)


@router.get("/branches/{branch_id}", response_model=StoreResponse, summary="查询门店详情")
async def branch_detail(branch_id: int = Path(..., ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> StoreResponse:
    return await get_store(db, branch_id)


@router.post("/branches/{branch_id}/members", response_model=StoreMemberResponse, status_code=201, summary="添加门店成员")
async def branch_member(branch_id: int = Path(..., ge=1), body: StoreMemberCreate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> StoreMemberResponse:
    return await add_store_member(db, _legacy_actor(current), branch_id, body)


@router.post("/assignments", response_model=ResourceAssignmentResponse, status_code=201, summary="分配会员资源")
async def assignment(body: ResourceAssignmentCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ResourceAssignmentResponse:
    return await assign_resource(db, _legacy_actor(current), body)


@router.get("/assignments", response_model=ResourceAssignmentPage, summary="查询资源分配记录")
async def assignments(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), user_id: int | None = Query(None, ge=1), matchmaker_id: int | None = Query(None, ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> ResourceAssignmentPage:
    conditions = ["1=1"]
    params: dict[str, int] = {"limit": page_size, "offset": (page - 1) * page_size}
    if user_id is not None:
        conditions.append("user_id = :user_id"); params["user_id"] = user_id
    if matchmaker_id is not None:
        conditions.append("matchmaker_id = :matchmaker_id"); params["matchmaker_id"] = matchmaker_id
    where = " AND ".join(conditions)
    rows = await db.execute(text(f"SELECT id, user_id, organization_id, matchmaker_id, source, status, effective_at, ended_at FROM resource_assignment WHERE {where} ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)
    count = await db.execute(text(f"SELECT COUNT(*) FROM resource_assignment WHERE {where}"), {k: v for k, v in params.items() if k not in ("limit", "offset")})
    total = int(count.scalar() or 0)
    return ResourceAssignmentPage(items=[ResourceAssignmentResponse(**dict(row)) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.post("/meetings/requests/{request_id}/schedule", response_model=MeetingRecordResponse, status_code=201, summary="安排约见")
async def schedule_matchmaker_meeting(request_id: int = Path(..., ge=1), body: MeetingScheduleCreate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MeetingRecordResponse:
    return await schedule_meeting(db, _legacy_actor(current), request_id, body)
