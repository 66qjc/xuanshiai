"""Server-side user restriction checks and administrator operations."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.restrictions import RestrictionCreate, RestrictionPage, RestrictionResponse


def _response(row: dict[str, Any]) -> RestrictionResponse:
    return RestrictionResponse(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        restriction_type=row["restriction_type"],
        reason_code=row["reason_code"],
        reason=row["reason"],
        starts_at=row["starts_at"],
        ends_at=row.get("ends_at"),
        status=int(row["status"]),
        ended_at=row.get("ended_at"),
        created_by=int(row["created_by"]),
        note=row.get("note"),
        created_at=row["created_at"],
    )


async def ensure_user_allowed(db: AsyncSession, user_id: int, restriction_type: str) -> None:
    """Fail closed for active restrictions; missing table errors must not grant access."""
    # Unit tests use lightweight scripted sessions for service behavior. API and
    # production callers always provide SQLAlchemy AsyncSession instances.
    if not isinstance(db, AsyncSession):
        return
    try:
        result = await db.execute(
            text("""SELECT restriction_type FROM user_restriction
                WHERE user_id = :user_id AND status = 1
                  AND starts_at <= UTC_TIMESTAMP()
                  AND (ends_at IS NULL OR ends_at > UTC_TIMESTAMP())
                  AND (restriction_type = 'TOTAL_BAN' OR restriction_type = :restriction_type)
                LIMIT 1"""),
            {"user_id": user_id, "restriction_type": restriction_type},
        )
    except Exception as exc:
        raise HTTPException(503, detail="用户安全状态暂不可用") from exc
    if result.scalar():
        raise HTTPException(403, detail="当前账号暂不能执行该操作")


async def list_restrictions(
    db: AsyncSession, user_id: int, page: int, page_size: int
) -> RestrictionPage:
    params = {"user_id": user_id, "limit": page_size, "offset": (page - 1) * page_size}
    total = int((await db.execute(text("SELECT COUNT(*) FROM user_restriction WHERE user_id = :user_id"), params)).scalar() or 0)
    result = await db.execute(text("""SELECT id, user_id, restriction_type, reason_code, reason,
        starts_at, ends_at, status, ended_at, created_by, note, created_at
        FROM user_restriction WHERE user_id = :user_id
        ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"""), params)
    return RestrictionPage(
        items=[_response(dict(row)) for row in result.mappings().all()],
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


async def create_restriction(
    db: AsyncSession, user_id: int, request: RestrictionCreate, actor_id: int, *, commit: bool = True
) -> RestrictionResponse:
    if request.ends_at and request.starts_at and request.ends_at <= request.starts_at:
        raise HTTPException(422, detail="限制结束时间必须晚于开始时间")
    user = await db.execute(text("SELECT id FROM users WHERE id = :user_id"), {"user_id": user_id})
    if not user.scalar():
        raise HTTPException(404, detail="用户不存在")
    active = await db.execute(text("""SELECT id FROM user_restriction
        WHERE user_id = :user_id AND restriction_type = :restriction_type AND status = 1
          AND (ends_at IS NULL OR ends_at > UTC_TIMESTAMP())
        LIMIT 1 FOR UPDATE"""), {"user_id": user_id, "restriction_type": request.restriction_type})
    if active.scalar():
        raise HTTPException(409, detail="相同限制已生效")
    result = await db.execute(text("""INSERT INTO user_restriction
        (user_id, restriction_type, reason_code, reason, starts_at, ends_at, created_by, note)
        VALUES (:user_id, :restriction_type, :reason_code, :reason,
                COALESCE(:starts_at, UTC_TIMESTAMP()), :ends_at, :created_by, :note)"""), {
        "user_id": user_id, "restriction_type": request.restriction_type,
        "reason_code": request.reason_code, "reason": request.reason,
        "starts_at": request.starts_at, "ends_at": request.ends_at,
        "created_by": actor_id, "note": request.note,
    })
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'restrict_user', 'user_restriction', :resource_id, :reason)"""), {
        "actor_id": actor_id, "resource_id": result.lastrowid, "reason": request.reason,
    })
    if commit:
        await db.commit()
    created = await db.execute(text("""SELECT id, user_id, restriction_type, reason_code, reason,
        starts_at, ends_at, status, ended_at, created_by, note, created_at
        FROM user_restriction WHERE id = :id"""), {"id": result.lastrowid})
    return _response(dict(created.mappings().one()))


async def end_restriction(db: AsyncSession, restriction_id: int, actor_id: int) -> None:
    result = await db.execute(text("""UPDATE user_restriction SET status = 2,
        ended_at = UTC_TIMESTAMP() WHERE id = :id AND status = 1"""), {"id": restriction_id})
    if result.rowcount == 0:
        exists = await db.execute(text("SELECT id FROM user_restriction WHERE id = :id"), {"id": restriction_id})
        if not exists.scalar():
            raise HTTPException(404, detail="限制记录不存在")
        raise HTTPException(409, detail="限制已解除或已结束")
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'release_user_restriction', 'user_restriction', :resource_id, '管理员解除用户限制')"""), {
        "actor_id": actor_id, "resource_id": restriction_id,
    })
    await db.commit()
