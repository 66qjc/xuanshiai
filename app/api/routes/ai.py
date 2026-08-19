"""Phase-one AI assistant, profile polish, semantic search and match routes."""

from typing import Literal

from fastapi import APIRouter, Body, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.ai import (
    AIAssistantMessageCreate,
    AIAssistantMessageResponse,
    AIAssistantSessionCreate,
    AIAssistantSessionPage,
    AIAssistantSessionResponse,
    AIMatchPage,
    AIProfilePolishRequest,
    AIProfilePolishResponse,
    AISearchRequest,
    AISearchResponse,
)
from app.services.ai import (
    assistant_message,
    create_assistant_session,
    list_assistant_sessions,
    match_page,
    parse_search,
    polish_profile,
)

router = APIRouter(prefix="/ai")


@router.post("/assistant/sessions", response_model=AIAssistantSessionResponse, status_code=201, summary="创建 AI 助手会话")
async def create_session(body: AIAssistantSessionCreate = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AIAssistantSessionResponse:
    return await create_assistant_session(db, current.id, body.title)


@router.get("/assistant/sessions", response_model=AIAssistantSessionPage, summary="查询 AI 助手会话")
async def sessions(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AIAssistantSessionPage:
    return await list_assistant_sessions(db, current.id, page, page_size)


@router.post("/assistant/sessions/{session_id}/messages", response_model=AIAssistantMessageResponse, status_code=201, summary="向 AI 助手提问")
async def message(session_id: int = Path(..., ge=1), body: AIAssistantMessageCreate = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AIAssistantMessageResponse:
    return await assistant_message(db, current.id, session_id, body.content)


@router.post("/profile/polish", response_model=AIProfilePolishResponse, summary="AI 润色文字资料")
async def polish(body: AIProfilePolishRequest = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AIProfilePolishResponse:
    return await polish_profile(db, current.id, body)


@router.post("/search", response_model=AISearchResponse, summary="AI 自然语言搜索")
async def search(body: AISearchRequest = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AISearchResponse:
    return await parse_search(db, current.id, body)


@router.get("/matches/{match_type}", response_model=AIMatchPage, summary="查询 AI 匹配结果")
async def matches(match_type: Literal["who_likes_me", "i_like", "material", "soul"] = Path(...), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=20), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AIMatchPage:
    return await match_page(db, current.id, match_type, page, page_size)

