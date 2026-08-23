"""Member creation, editing and certification details for the back office."""

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.matchmaker_member_admin import (
    CertificationDetail,
    CertificationsAdminResponse,
    MatchmakerMemberAdminItem,
    MatchmakerMemberCreate,
    MatchmakerMemberUpdate,
    MemberAuditLogItem,
)
from app.services.matchmaker_member_admin import (
    certification_detail,
    create_member,
    member_audit_logs,
    update_member,
)

router = APIRouter(prefix="/admin/matchmaker/members")


@router.post("", response_model=MatchmakerMemberAdminItem, status_code=201)
async def create(
    body: MatchmakerMemberCreate,
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerMemberAdminItem:
    return await create_member(db, body, current.account.id)


@router.patch("/{member_id}", response_model=MatchmakerMemberAdminItem)
async def update(
    member_id: int = Path(..., ge=1),
    body: MatchmakerMemberUpdate = ...,
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerMemberAdminItem:
    return await update_member(db, member_id, body, current.account.id)


@router.get("/{member_id}/certifications", response_model=CertificationsAdminResponse)
async def certifications(
    member_id: int = Path(..., ge=1),
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> CertificationsAdminResponse:
    return CertificationsAdminResponse(
        education=await certification_detail(db, member_id, "education"),
        house=await certification_detail(db, member_id, "house"),
        marriage=await certification_detail(db, member_id, "marriage"),
    )


@router.get("/{member_id}/certifications/{kind}", response_model=CertificationDetail)
async def certification(
    member_id: int = Path(..., ge=1),
    kind: str = Path(..., pattern="^(education|house|marriage)$"),
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> CertificationDetail:
    return await certification_detail(db, member_id, kind)


@router.get("/{member_id}/audit-logs", response_model=list[MemberAuditLogItem])
async def audit_logs(
    member_id: int = Path(..., ge=1),
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> list[MemberAuditLogItem]:
    return await member_audit_logs(db, member_id)
