"""Member CRM follow-up and behavior routes."""

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentMatchmakerAdmin, get_current_matchmaker_admin
from app.db.session import get_db
from app.schemas.member_follow_up_admin import MemberBehaviorItem, MemberBehaviorPage, MemberFollowUp, MemberFollowUpCreate, MemberFollowUpPage

router = APIRouter(prefix="/admin/members")


async def _ensure_member(db: AsyncSession, user_id: int) -> None:
    if not await db.scalar(text("SELECT 1 FROM users WHERE id = :id"), {"id": user_id}):
        raise HTTPException(404, detail="会员不存在")


@router.get("/{member_id}/follow-ups", response_model=MemberFollowUpPage, summary="查询会员跟进记录")
async def list_follow_ups(member_id: int = Path(..., ge=1), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MemberFollowUpPage:
    await _ensure_member(db, member_id)
    params = {"user_id": member_id, "limit": page_size, "offset": (page - 1) * page_size}
    rows = await db.execute(text("SELECT id, user_id, method, content, next_follow_at, created_by, created_at FROM member_follow_up WHERE user_id = :user_id ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)
    total = int((await db.scalar(text("SELECT COUNT(*) FROM member_follow_up WHERE user_id = :user_id"), {"user_id": member_id})) or 0)
    return MemberFollowUpPage(items=[MemberFollowUp(**dict(row)) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)


@router.post("/{member_id}/follow-ups", response_model=MemberFollowUp, status_code=201, summary="新增会员跟进记录")
async def create_follow_up(member_id: int = Path(..., ge=1), body: MemberFollowUpCreate = ..., current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MemberFollowUp:
    await _ensure_member(db, member_id)
    result = await db.execute(text("INSERT INTO member_follow_up (user_id, method, content, next_follow_at, created_by) VALUES (:user_id, :method, :content, :next_follow_at, :created_by)"), {**body.model_dump(), "user_id": member_id, "created_by": current.account.id})
    follow_id = int(result.lastrowid)
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id) VALUES (:actor, 'member.follow_up.create', 'member_follow_up', :id)"), {"actor": current.account.id, "id": follow_id})
    await db.commit()
    row = (await db.execute(text("SELECT id, user_id, method, content, next_follow_at, created_by, created_at FROM member_follow_up WHERE id = :id"), {"id": follow_id})).mappings().one()
    return MemberFollowUp(**dict(row))


@router.get("/{member_id}/behavior", response_model=MemberBehaviorPage, summary="查询会员行为流水")
async def behavior(member_id: int = Path(..., ge=1), page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=100), current: CurrentMatchmakerAdmin = Depends(get_current_matchmaker_admin), db: AsyncSession = Depends(get_db)) -> MemberBehaviorPage:
    await _ensure_member(db, member_id)
    offset = (page - 1) * page_size
    query = text("""SELECT event_type, event_id, target_user_id, target_nickname, detail, occurred_at FROM (
        SELECT 'login' event_type, l.id event_id, NULL target_user_id, NULL target_nickname, IF(l.login_status = 1, '登录成功', CONCAT('登录失败：', COALESCE(l.failure_reason, '未知原因'))) detail, l.created_at occurred_at FROM user_login_log l WHERE l.user_id = :id
        UNION ALL SELECT 'browse', h.id, h.target_user_id, u.nickname, '浏览资料', h.created_at FROM user_browse_history h LEFT JOIN users u ON u.id = h.target_user_id WHERE h.user_id = :id
        UNION ALL SELECT 'favorite', f.id, f.target_user_id, u.nickname, '收藏用户', f.created_at FROM user_favorite f LEFT JOIN users u ON u.id = f.target_user_id WHERE f.user_id = :id
        UNION ALL SELECT 'swipe', s.id, s.target_user_id, u.nickname, s.action, s.created_at FROM user_swipe_record s LEFT JOIN users u ON u.id = s.target_user_id WHERE s.user_id = :id
        UNION ALL SELECT 'apply', a.id, a.to_user_id, u.nickname, '提交认识申请', a.created_at FROM match_apply a LEFT JOIN users u ON u.id = a.to_user_id WHERE a.from_user_id = :id
    ) events ORDER BY occurred_at DESC, event_id DESC LIMIT :limit OFFSET :offset""")
    rows = await db.execute(query, {"id": member_id, "limit": page_size, "offset": offset})
    total = int((await db.scalar(text("""SELECT (SELECT COUNT(*) FROM user_login_log WHERE user_id = :id) + (SELECT COUNT(*) FROM user_browse_history WHERE user_id = :id) + (SELECT COUNT(*) FROM user_favorite WHERE user_id = :id) + (SELECT COUNT(*) FROM user_swipe_record WHERE user_id = :id) + (SELECT COUNT(*) FROM match_apply WHERE from_user_id = :id)"""), {"id": member_id})) or 0)
    return MemberBehaviorPage(items=[MemberBehaviorItem(**dict(row)) for row in rows.mappings().all()], page=page, page_size=page_size, total=total, has_more=page * page_size < total)

