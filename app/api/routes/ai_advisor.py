"""Relationship advisor routes."""

from fastapi import APIRouter, Body, Depends, Path, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.ai_advisor import (
    AdvisorAdviceRequest,
    AdvisorAdviceResponse,
    AdvisorFeedbackRequest,
    AdvisorFeedbackResponse,
    AdvisorSessionCreate,
    AdvisorSessionPage,
    AdvisorSessionResponse,
)
from app.services.ai_advisor import (
    create_session,
    delete_session,
    get_advice,
    list_sessions,
    record_feedback,
)

router = APIRouter(prefix="/ai/advisor")


@router.post("/sessions", response_model=AdvisorSessionResponse, status_code=status.HTTP_201_CREATED, summary="创建情感军师会话")
async def create_advisor_session(
    body: AdvisorSessionCreate = Body(...),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdvisorSessionResponse:
    return await create_session(db, current.id, body)


@router.get("/sessions", response_model=AdvisorSessionPage, summary="查询情感军师会话")
async def advisor_sessions(
    page: int = Query(1, ge=1, le=1000),
    page_size: int = Query(20, ge=1, le=50),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdvisorSessionPage:
    return await list_sessions(db, current.id, page, page_size)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除情感军师会话")
async def remove_advisor_session(
    session_id: int = Path(..., ge=1),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await delete_session(db, current.id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/{session_id}/advice", response_model=AdvisorAdviceResponse, status_code=status.HTTP_201_CREATED, summary="获取情感军师建议")
async def advisor_advice(
    session_id: int = Path(..., ge=1),
    body: AdvisorAdviceRequest = Body(...),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdvisorAdviceResponse:
    return await get_advice(db, current.id, session_id, body)


@router.post("/messages/{message_id}/feedback", response_model=AdvisorFeedbackResponse, summary="反馈情感军师建议")
async def advisor_feedback(
    message_id: int = Path(..., ge=1),
    body: AdvisorFeedbackRequest = Body(...),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AdvisorFeedbackResponse:
    return await record_feedback(db, current.id, message_id, body)
