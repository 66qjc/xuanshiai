"""Customer lead management routes."""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.customer_lead_admin import CustomerLead, CustomerLeadAbandonment, CustomerLeadAbandonRequest, CustomerLeadAssignment, CustomerLeadCreate, CustomerLeadFollowUp, CustomerLeadFollowUpCreate, CustomerLeadPage, CustomerLeadRestoreRequest, CustomerLeadStatistics, CustomerLeadUpdate
from app.services.customer_lead_admin import abandon_lead, add_follow_up, assign_lead, create_lead, get_lead, lead_statistics, list_abandonments, list_follow_ups, list_leads, restore_lead, update_lead

router = APIRouter(prefix="/admin/customer-leads")


@router.get("", response_model=CustomerLeadPage, summary="查询客源线索")
async def lead_list(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), status: str | None = Query(None, pattern="^(NEW|CONTACTED|INTENDED|CONVERTED|LOST|CLOSED)$"), source: str | None = Query(None, max_length=64), matchmaker_id: int | None = Query(None, ge=1), search: str | None = Query(None, max_length=64), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLeadPage:
    current.require("customer_lead.manage")
    return await list_leads(db, page, page_size, status, source, matchmaker_id, search)


@router.post("", response_model=CustomerLead, status_code=201, summary="录入客源线索")
async def lead_create(body: CustomerLeadCreate, current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLead:
    current.require("customer_lead.manage")
    return await create_lead(db, current.account.id, body)


@router.get("/statistics", response_model=CustomerLeadStatistics, summary="查询客源线索统计")
async def lead_stats(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLeadStatistics:
    current.require("customer_lead.manage")
    return await lead_statistics(db)


@router.get("/abandoned", response_model=list[CustomerLeadAbandonment], summary="查询弃海客源")
async def abandoned_list(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[CustomerLeadAbandonment]:
    current.require("customer_lead.manage")
    return await list_abandonments(db, True)


@router.get("/abandonments", response_model=list[CustomerLeadAbandonment], summary="查询弃海记录")
async def abandonment_list(current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[CustomerLeadAbandonment]:
    current.require("customer_lead.manage")
    return await list_abandonments(db, False)


@router.get("/{lead_id}", response_model=CustomerLead, summary="查询客源线索详情")
async def lead_detail(lead_id: int = Path(..., ge=1), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLead:
    current.require("customer_lead.manage")
    return await get_lead(db, lead_id)


@router.patch("/{lead_id}", response_model=CustomerLead, summary="修改客源线索")
async def lead_update(lead_id: int = Path(..., ge=1), body: CustomerLeadUpdate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLead:
    current.require("customer_lead.manage")
    return await update_lead(db, current.account.id, lead_id, body)


@router.get("/{lead_id}/follow-ups", response_model=list[CustomerLeadFollowUp], summary="查询线索跟进记录")
async def follow_up_list(lead_id: int = Path(..., ge=1), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> list[CustomerLeadFollowUp]:
    current.require("customer_lead.manage")
    return await list_follow_ups(db, lead_id, page, page_size)


@router.post("/{lead_id}/follow-ups", response_model=CustomerLeadFollowUp, status_code=201, summary="新增线索跟进记录")
async def follow_up_create(lead_id: int = Path(..., ge=1), body: CustomerLeadFollowUpCreate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLeadFollowUp:
    current.require("customer_lead.manage")
    return await add_follow_up(db, current.account.id, lead_id, body)


@router.patch("/{lead_id}/assignment", response_model=CustomerLead, summary="分配客源线索")
async def lead_assignment(lead_id: int = Path(..., ge=1), body: CustomerLeadAssignment = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLead:
    current.require("customer_lead.manage")
    return await assign_lead(db, current.account.id, lead_id, body)


@router.post("/{lead_id}/abandon", response_model=CustomerLeadAbandonment, status_code=201, summary="放入弃海池")
async def lead_abandon(lead_id: int = Path(..., ge=1), body: CustomerLeadAbandonRequest = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLeadAbandonment:
    current.require("customer_lead.manage")
    return await abandon_lead(db, current.account.id, lead_id, body.reason)


@router.post("/{lead_id}/restore", response_model=CustomerLead, summary="从弃海池恢复")
async def lead_restore(lead_id: int = Path(..., ge=1), body: CustomerLeadRestoreRequest = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> CustomerLead:
    current.require("customer_lead.manage")
    return await restore_lead(db, current.account.id, lead_id, body.reason)
