from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.message_admin import AdminAnnouncementCreate, AdminAnnouncementItem, AdminMessageModerationRequest, AdminMessagePage, AdminMessageItem
from app.services.message_admin import list_admin_messages, moderate_admin_message, create_admin_announcement

router=APIRouter(prefix="/admin/messages")

@router.get("", response_model=AdminMessagePage)
async def messages(page:int=Query(1,ge=1), page_size:int=Query(20,ge=1,le=100), user_id:int|None=Query(None,ge=1), session_id:int|None=Query(None,ge=1), message_type:int|None=Query(None,ge=1,le=6), current:CurrentMatchmakerAdmin=Depends(get_current_matchmaker_admin), db:AsyncSession=Depends(get_db)):
    return await list_admin_messages(db,page,page_size,user_id,session_id,message_type)

@router.patch("/{message_id}/moderation", response_model=AdminMessageItem)
async def moderate(message_id:int=Path(...,ge=1), body:AdminMessageModerationRequest=..., current:CurrentMatchmakerAdmin=Depends(get_current_matchmaker_admin), db:AsyncSession=Depends(get_db)):
    return await moderate_admin_message(db,current.account.id,message_id,body.action,body.reason)

@router.post("/announcements", response_model=AdminAnnouncementItem, status_code=201)
async def announcement(body:AdminAnnouncementCreate, current:CurrentMatchmakerAdmin=Depends(get_current_matchmaker_admin), db:AsyncSession=Depends(get_db)):
    return await create_admin_announcement(db,current.account.id,body)
