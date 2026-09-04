"""Paper-plane bilateral contact-exchange endpoints."""

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.paper_plane_contact_exchange import (
    PaperPlaneContactExchangeCreate,
    PaperPlaneContactExchangeRespond,
    PaperPlaneContactExchangeResponse,
)
from app.services.paper_plane_contact_exchange import (
    ContactExchangeConflict,
    ContactExchangeForbidden,
    ContactExchangeNotFound,
    ContactExchangeService,
    SqlAlchemyPaperPlaneContactExchangeStore,
)

router = APIRouter()


def _key(value: str | None) -> str:
    if not value or not 8 <= len(value) <= 128:
        raise HTTPException(422, detail="请提供 8-128 个字符的 Idempotency-Key")
    return value


def _service(db: AsyncSession) -> ContactExchangeService:
    return ContactExchangeService(SqlAlchemyPaperPlaneContactExchangeStore(db))


@router.post(
    "/paper-plane-conversations/{conversation_id}/contact-exchanges",
    response_model=PaperPlaneContactExchangeResponse,
    status_code=201,
    summary="发起纸飞机联系方式交换申请",
)
async def create_contact_exchange(
    conversation_id: int = Path(..., ge=1),
    body: PaperPlaneContactExchangeCreate = Body(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneContactExchangeResponse:
    try:
        result = await _service(db).request(conversation_id, current.id, 0, body.kind, _key(idempotency_key))
    except ContactExchangeNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ContactExchangeForbidden as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except ContactExchangeConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return PaperPlaneContactExchangeResponse(**result)


@router.post(
    "/paper-plane-contact-exchanges/{exchange_id}/respond",
    response_model=PaperPlaneContactExchangeResponse,
    summary="处理纸飞机联系方式交换申请",
)
async def respond_contact_exchange(
    exchange_id: int = Path(..., ge=1),
    body: PaperPlaneContactExchangeRespond = Body(...),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PaperPlaneContactExchangeResponse:
    try:
        result = await _service(db).respond(current.id, exchange_id, body.decision, _key(idempotency_key))
    except ContactExchangeNotFound as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ContactExchangeForbidden as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except ContactExchangeConflict as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return PaperPlaneContactExchangeResponse(**result)


respond_paper_plane_contact_exchange = respond_contact_exchange
