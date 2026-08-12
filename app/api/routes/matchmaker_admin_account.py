"""Independent matchmaker back-office account administration routes."""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.matchmaker_admin_account import (
    MatchmakerAdminAccountCreate,
    MatchmakerAdminAccountItem,
    MatchmakerAdminAccountPage,
    MatchmakerAdminAccountStatusUpdate,
    MatchmakerAdminAccountUpdate,
    MatchmakerAdminLoginLogPage,
    MatchmakerAdminPasswordReset,
    MatchmakerAdminSessionPage,
)
from app.services.matchmaker_admin_account import (
    create_account,
    get_account,
    list_accounts,
    list_login_logs,
    list_sessions,
    reset_password,
    revoke_all_sessions,
    update_account,
    update_account_status,
)

router = APIRouter(prefix="/admin/matchmaker")


@router.get("/accounts", response_model=MatchmakerAdminAccountPage)
async def accounts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    username: str | None = Query(None, max_length=64),
    display_name: str | None = Query(None, max_length=128),
    status: int | None = Query(None, ge=1, le=3),
    matchmaker_user_id: int | None = Query(None, ge=1),
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerAdminAccountPage:
    current.require("matchmaker.account.manage")
    return await list_accounts(db, page, page_size, username, display_name, status, matchmaker_user_id)


@router.post("/accounts", response_model=MatchmakerAdminAccountItem, status_code=201)
async def create_admin_account(
    body: MatchmakerAdminAccountCreate,
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerAdminAccountItem:
    current.require("matchmaker.account.manage")
    return await create_account(db, body, current.account.id)


@router.get("/accounts/login-logs", response_model=MatchmakerAdminLoginLogPage)
async def login_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    account_id: int | None = Query(None, ge=1),
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerAdminLoginLogPage:
    current.require("matchmaker.account.manage")
    return await list_login_logs(db, page, page_size, account_id)


@router.get("/accounts/{account_id}", response_model=MatchmakerAdminAccountItem)
async def account_detail(
    account_id: int = Path(..., ge=1),
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
):
    current.require("matchmaker.account.manage")
    return await get_account(db, account_id)


@router.patch("/accounts/{account_id}", response_model=MatchmakerAdminAccountItem)
async def edit_account(
    account_id: int,
    body: MatchmakerAdminAccountUpdate,
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerAdminAccountItem:
    current.require("matchmaker.account.manage")
    return await update_account(db, account_id, body, current.account.id)


@router.patch("/accounts/{account_id}/status", response_model=MatchmakerAdminAccountItem)
async def change_account_status(
    account_id: int,
    body: MatchmakerAdminAccountStatusUpdate,
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerAdminAccountItem:
    current.require("matchmaker.account.manage")
    return await update_account_status(db, account_id, body, current.account.id)


@router.post("/accounts/{account_id}/reset-password", response_model=MatchmakerAdminAccountItem)
async def reset_account_password(
    account_id: int,
    body: MatchmakerAdminPasswordReset,
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerAdminAccountItem:
    current.require("matchmaker.account.manage")
    return await reset_password(db, account_id, body, current.account.id)


@router.get("/accounts/{account_id}/sessions", response_model=MatchmakerAdminSessionPage)
async def account_sessions(
    account_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> MatchmakerAdminSessionPage:
    current.require("matchmaker.account.manage")
    return await list_sessions(db, account_id, page, page_size)


@router.post("/accounts/{account_id}/sessions/revoke-all", status_code=204)
async def revoke_account_sessions(
    account_id: int,
    current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    await revoke_all_sessions(db, account_id, current.account.id)
