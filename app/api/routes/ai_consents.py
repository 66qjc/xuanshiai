"""Public AI consent list, grant and revoke endpoints."""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.logging import request_id_context
from app.db.session import get_db
from app.schemas.ai_common import (
    AiConsentGrantRequest,
    AiConsentListResponse,
    AiConsentOperationResponse,
    AiErrorResponse,
)
from app.services.ai.consents import (
    ConsentError,
    grant_consent,
    list_consents,
    revoke_consent,
)

router = APIRouter()
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _request_id() -> str:
    value = request_id_context.get()
    return value if value and value != "-" else uuid4().hex


def _error_response(exc: ConsentError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=AiErrorResponse(
            code=exc.code,
            message=exc.message,
            request_id=_request_id(),
            retryable=False,
            retry_after_ms=0,
        ).model_dump(),
    )


def _check_idempotency_key(value: str | None) -> str:
    if not value or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AiErrorResponse(
                code="AI_INPUT_INVALID",
                message="Idempotency-Key must be 8-128 ASCII characters",
                request_id=_request_id(),
            ).model_dump(),
        )
    return value


@router.get("/consents", response_model=AiConsentListResponse, summary="List active AI consents")
async def get_ai_consents(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AiConsentListResponse:
    return await list_consents(db, current.id)


@router.put(
    "/consents/{scope}",
    response_model=AiConsentOperationResponse,
    summary="Grant an AI consent scope",
)
async def put_ai_consent(
    scope: str = Path(..., min_length=1, max_length=64),
    body: AiConsentGrantRequest = ...,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_privacy_revision: int = Header(
        ..., alias="X-Expected-Privacy-Revision", ge=0
    ),
) -> AiConsentOperationResponse:
    key = _check_idempotency_key(idempotency_key)
    try:
        response = await grant_consent(
            db,
            current.id,
            scope,
            body,
            key,
            expected_privacy_revision,
        )
        await db.commit()
        return response
    except ConsentError as exc:
        raise _error_response(exc) from exc


@router.delete(
    "/consents/{scope}",
    response_model=AiConsentOperationResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Revoke an AI consent scope and schedule cleanup",
)
async def delete_ai_consent(
    scope: str = Path(..., min_length=1, max_length=64),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_privacy_revision: int = Header(
        ..., alias="X-Expected-Privacy-Revision", ge=0
    ),
) -> AiConsentOperationResponse:
    key = _check_idempotency_key(idempotency_key)
    try:
        response = await revoke_consent(
            db,
            current.id,
            scope,
            key,
            expected_privacy_revision,
        )
        await db.commit()
        return response
    except ConsentError as exc:
        raise _error_response(exc) from exc
