"""Relationships, chat, notifications and safety routes."""

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    CurrentUser,
    get_current_user,
    get_realname_verified_user,
    get_verified_user,
)
from app.db.session import get_db
from app.schemas.social import (
    BlockRequest,
    ChatMessageCreate,
    ChatMessageResponse,
    ChatMessagePage,
    ChatSessionRequestAction,
    ChatSessionRequestCreate,
    ChatSessionRequestResponse,
    NotificationUnreadSummary,
    ChatSessionPage,
    NotificationPage,
    PrivacyResponse,
    PrivacyUpdateRequest,
    RelationPage,
    RelationResponse,
    ReportRequest,
    ReportResponse,
    SocialUser,
)
from app.services.social import (
    create_report,
    get_privacy,
    list_blocks,
    list_chat_sessions,
    list_messages,
    list_messages_cursor,
    set_chat_session_visibility,
    create_chat_session_request,
    list_chat_session_requests,
    respond_chat_session_request,
    notification_unread_summary,
    list_notifications,
    list_relation,
    mark_messages_read,
    mark_notification_read,
    revoke_message,
    recall_message,
    send_message,
    set_block,
    set_follow,
    set_like,
    unmatch,
    update_privacy,
)

router = APIRouter(dependencies=[Depends(get_verified_user)])


@router.put("/users/{target_id}/like", response_model=RelationResponse, summary="喜欢用户")
async def like(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_realname_verified_user), db: AsyncSession = Depends(get_db)) -> RelationResponse:
    return await set_like(db, current.id, target_id, True)


@router.delete("/users/{target_id}/like", response_model=RelationResponse, summary="取消喜欢")
async def unlike(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_realname_verified_user), db: AsyncSession = Depends(get_db)) -> RelationResponse:
    return await set_like(db, current.id, target_id, False)


@router.put("/users/{target_id}/follow", response_model=RelationResponse, summary="关注用户")
async def follow(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_realname_verified_user), db: AsyncSession = Depends(get_db)) -> RelationResponse:
    return await set_follow(db, current.id, target_id, True)


@router.delete("/users/{target_id}/follow", response_model=RelationResponse, summary="取消关注")
async def unfollow(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_realname_verified_user), db: AsyncSession = Depends(get_db)) -> RelationResponse:
    return await set_follow(db, current.id, target_id, False)


@router.get("/relations/likes", response_model=RelationPage, summary="查看我的喜欢")
async def my_likes(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> RelationPage:
    return await list_relation(db, current.id, "like", False, page, page_size)


@router.get("/relations/liked-by", response_model=RelationPage, summary="查看喜欢我的人", deprecated=True)
async def liked_by(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> RelationPage:
    raise HTTPException(status_code=410, detail="鍠滄鍒楄〃浠呮湰浜哄彲瑙?")


@router.get("/relations/following", response_model=RelationPage, summary="查看我的关注")
async def following(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> RelationPage:
    return await list_relation(db, current.id, "follow", False, page, page_size)


@router.get("/relations/followers", response_model=RelationPage, summary="查看我的粉丝")
async def followers(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> RelationPage:
    return await list_relation(db, current.id, "follow", True, page, page_size)


@router.get("/relations/matches", response_model=RelationPage, summary="查看我的匹配")
async def matches(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> RelationPage:
    return await list_relation(db, current.id, "match", False, page, page_size)


@router.delete("/relations/matches/{target_id}", status_code=204, summary="取消匹配")
async def cancel_match(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await unmatch(db, current.id, target_id)


@router.get("/chat/sessions", response_model=ChatSessionPage, summary="查看聊天会话")
async def sessions(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ChatSessionPage:
    return await list_chat_sessions(db, current.id, page, page_size)


@router.get("/chat/sessions/{session_id}/messages/cursor", response_model=ChatMessagePage, summary="游标分页查看聊天记录")
async def messages_cursor(session_id: int = Path(..., ge=1), cursor: int | None = Query(None, ge=1), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ChatMessagePage:
    return await list_messages_cursor(db, current.id, session_id, cursor, page_size)


@router.put("/chat/sessions/{session_id}/pin", status_code=204, summary="置顶聊天会话")
async def pin_session(session_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await set_chat_session_visibility(db, current.id, session_id, pinned=True)


@router.delete("/chat/sessions/{session_id}/pin", status_code=204, summary="取消置顶聊天会话")
async def unpin_session(session_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await set_chat_session_visibility(db, current.id, session_id, pinned=False)


@router.delete("/chat/sessions/{session_id}", status_code=204, summary="隐藏聊天会话")
async def hide_session(session_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await set_chat_session_visibility(db, current.id, session_id, hidden=True)


@router.post("/chat/sessions/{session_id}/restore", status_code=204, summary="恢复聊天会话")
async def restore_session(session_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await set_chat_session_visibility(db, current.id, session_id, hidden=False)


@router.post("/chat/sessions/{session_id}/requests", response_model=ChatSessionRequestResponse, status_code=201, summary="发起会话结构化请求")
async def create_session_request(session_id: int = Path(..., ge=1), body: ChatSessionRequestCreate = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ChatSessionRequestResponse:
    return await create_chat_session_request(db, current.id, session_id, body)


@router.get("/chat/sessions/{session_id}/requests", response_model=list[ChatSessionRequestResponse], summary="查询会话结构化请求")
async def session_requests(session_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[ChatSessionRequestResponse]:
    return await list_chat_session_requests(db, current.id, session_id)


@router.patch("/chat/session-requests/{request_id}", response_model=ChatSessionRequestResponse, summary="处理会话结构化请求")
async def handle_session_request(request_id: int = Path(..., ge=1), body: ChatSessionRequestAction = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ChatSessionRequestResponse:
    return await respond_chat_session_request(db, current.id, request_id, body.action)


@router.get("/chat/sessions/{session_id}/messages", response_model=list[ChatMessageResponse], summary="查看聊天记录")
async def messages(session_id: int = Path(..., ge=1), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[ChatMessageResponse]:
    return await list_messages(db, current.id, session_id, page, page_size)


@router.post("/chat/sessions/{session_id}/messages", response_model=ChatMessageResponse, status_code=201, summary="发送聊天消息")
async def send(session_id: int = Path(..., ge=1), body: ChatMessageCreate = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ChatMessageResponse:
    return await send_message(db, current.id, session_id, body)


@router.post("/chat/sessions/{session_id}/read", status_code=204, summary="标记聊天已读")
async def read_messages(session_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await mark_messages_read(db, current.id, session_id)


@router.delete("/chat/messages/{message_id}", status_code=204, summary="撤回聊天消息")
async def revoke(message_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await revoke_message(db, current.id, message_id)


@router.post("/chat/messages/{message_id}/recall", response_model=ChatMessageResponse, summary="撤回聊天消息并返回最新状态")
async def recall(message_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ChatMessageResponse:
    return await recall_message(db, current.id, message_id)


@router.get("/notifications", response_model=NotificationPage, summary="查看消息通知")
async def notifications(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50), notification_type: str | None = Query(None, max_length=64), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> NotificationPage:
    return await list_notifications(db, current.id, page, page_size, notification_type)


@router.get("/notifications/unread-summary", response_model=NotificationUnreadSummary, summary="查询分类未读汇总")
async def unread_summary(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> NotificationUnreadSummary:
    return await notification_unread_summary(db, current.id)


@router.post("/notifications/{notification_id}/read", status_code=204, summary="标记通知已读")
async def read_notification(notification_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await mark_notification_read(db, current.id, notification_id)


@router.post("/notifications/read-all", status_code=204, summary="全部通知已读")
async def read_all_notifications(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await mark_notification_read(db, current.id, None)


@router.get("/users/me/privacy", response_model=PrivacyResponse, summary="查看隐私设置")
async def privacy(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> PrivacyResponse:
    return await get_privacy(db, current.id)


@router.put("/users/me/privacy", response_model=PrivacyResponse, summary="更新隐私设置")
async def update_privacy_settings(body: PrivacyUpdateRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> PrivacyResponse:
    return await update_privacy(db, current.id, body)


@router.get("/security/blocks", response_model=list[SocialUser], summary="查看黑名单")
async def blocks(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[SocialUser]:
    return await list_blocks(db, current.id)


@router.put("/security/blocks/{target_id}", status_code=204, summary="拉黑用户")
async def block(target_id: int = Path(..., ge=1), body: BlockRequest = Body(default=BlockRequest()), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await set_block(db, current.id, target_id, body, True)


@router.delete("/security/blocks/{target_id}", status_code=204, summary="解除拉黑")
async def unblock(target_id: int = Path(..., ge=1), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    await set_block(db, current.id, target_id, BlockRequest(), False)


@router.post("/security/reports/{target_id}", response_model=ReportResponse, status_code=201, summary="举报用户")
async def report(target_id: int = Path(..., ge=1), body: ReportRequest = Body(...), current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> ReportResponse:
    return await create_report(db, current.id, target_id, body)
