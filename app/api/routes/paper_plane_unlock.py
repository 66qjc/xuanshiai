"""Paper-plane target-specific profile unlock endpoints."""

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.paper_plane_unlock import PaperPlaneProfileUnlockResponse
from app.services.paper_plane_unlock import (
    PaperPlaneProfileUnlockService,
    ProfileUnlockConflict,
    ProfileUnlockForbidden,
    ProfileUnlockInsufficientPoints,
    ProfileUnlockNotFound,
    SqlAlchemyPaperPlaneUnlockStore,
    get_profile_unlock,
)

router = APIRouter()


@router.get(
    "/paper-plane/profile-unlocks/{target_user_id}",
    response_model=PaperPlaneProfileUnlockResponse,
    summary="查询纸飞机目标用户资料解锁状态",
)
async def profile_unlock_status(
    target_user_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneProfileUnlockResponse:
    store = SqlAlchemyPaperPlaneUnlockStore(db)
    try:
        await store.ensure_target_visible(current.id, target_user_id)
        result = await get_profile_unlock(db, current.id, target_user_id)
    except ProfileUnlockNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProfileUnlockForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return PaperPlaneProfileUnlockResponse(**result)


@router.post(
    "/paper-plane/profile-unlocks/{target_user_id}",
    response_model=PaperPlaneProfileUnlockResponse,
    status_code=200,
    summary="以 80 积分解锁纸飞机目标用户资料",
)
async def unlock_profile(
    target_user_id: int = Path(..., ge=1),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneProfileUnlockResponse:
    if not idempotency_key or not 1 <= len(idempotency_key) <= 128:
        raise HTTPException(status_code=422, detail="请提供 1-128 个字符的 Idempotency-Key")
    service = PaperPlaneProfileUnlockService(SqlAlchemyPaperPlaneUnlockStore(db))
    try:
        result = await service.unlock(current.id, target_user_id, idempotency_key)
    except ProfileUnlockNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProfileUnlockInsufficientPoints as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc
    except ProfileUnlockConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProfileUnlockForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return PaperPlaneProfileUnlockResponse(**result)


# Contract-test compatibility alias; the registered router remains the
# plan's paper_plane_unlock module.
unlock_paper_plane_profile = unlock_profile
