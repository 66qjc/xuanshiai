"""Independent back-office store and assignment routes."""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.organization_admin import (
    AssignmentAdminPage,
    AssignmentAdminItem,
    StoreAdminItem,
    StoreAdminUpdate,
    StoreMemberAdminItem,
    StoreMemberAdminPage,
    StoreReport,
    StoreStatusUpdate,
)
from app.schemas.organization import StoreMemberResponse
from app.services.organization import add_store_member
from app.services.organization_admin import (
    end_assignment,
    get_store_admin,
    list_assignments,
    list_store_members,
    remove_store_member,
    store_report,
    update_store,
    update_store_status,
)
from app.schemas.organization import StoreMemberCreate

router = APIRouter(prefix="/admin/matchmaker")


@router.get("/stores/{store_id}", response_model=StoreAdminItem)
async def store_detail(store_id: int = Path(..., ge=1), _: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await get_store_admin(db, store_id)


@router.patch("/stores/{store_id}", response_model=StoreAdminItem)
async def store_update(store_id: int = Path(..., ge=1), body: StoreAdminUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await update_store(db, store_id, body, current.account.id)


@router.patch("/stores/{store_id}/status", response_model=StoreAdminItem)
async def store_status(store_id: int = Path(..., ge=1), body: StoreStatusUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await update_store_status(db, store_id, body.status, body.reason, current.account.id)


@router.get("/stores/{store_id}/members", response_model=StoreMemberAdminPage)
async def store_members(store_id: int = Path(..., ge=1), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), _: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    items, total = await list_store_members(db, store_id, page, page_size)
    return StoreMemberAdminPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.post("/stores/{store_id}/members", response_model=StoreMemberResponse, status_code=201)
async def store_member_add(store_id: int = Path(..., ge=1), body: StoreMemberCreate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await add_store_member(db, current_user(current), store_id, body)


@router.delete("/store-members/{member_id}", response_model=StoreMemberAdminItem)
async def store_member_remove(member_id: int = Path(..., ge=1), reason: str = Query(..., min_length=1, max_length=255), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await remove_store_member(db, member_id, reason, current.account.id)


@router.get("/stores/{store_id}/report", response_model=StoreReport)
async def store_report_route(store_id: int = Path(..., ge=1), _: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await store_report(db, store_id)


@router.get("/assignments", response_model=AssignmentAdminPage, operation_id="matchmaker_admin_assignments_page")
async def assignments(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), search: str | None = Query(None, max_length=128), _: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    items, total = await list_assignments(db, page, page_size, search)
    return AssignmentAdminPage(items=items, page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.post("/assignments/{assignment_id}/end", response_model=AssignmentAdminItem)
async def assignment_end(assignment_id: int = Path(..., ge=1), reason: str = Query(..., min_length=1, max_length=255), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)):
    return await end_assignment(db, assignment_id, reason, current.account.id)


def current_user(current: CurrentMatchmakerAdmin):
    from app.api.dependencies import CurrentUser
    return CurrentUser(id=current.account.id, session_id=current.session_id, phone=None, status=1, realname_status=2)
