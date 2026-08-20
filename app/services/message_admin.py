from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.message_admin import AdminMessageItem, AdminMessagePage, AdminAnnouncementCreate, AdminAnnouncementItem

async def list_admin_messages(db: AsyncSession, page: int, page_size: int, user_id: int | None = None, session_id: int | None = None, message_type: int | None = None) -> AdminMessagePage:
    where = ["1=1"]; params = {"limit": page_size, "offset": (page-1)*page_size}
    if user_id is not None: where.append("(from_user_id=:user_id OR to_user_id=:user_id)"); params["user_id"] = user_id
    if session_id is not None: where.append("session_id=:session_id"); params["session_id"] = session_id
    if message_type is not None: where.append("type=:message_type"); params["message_type"] = message_type
    clause = " AND ".join(where)
    rows = (await db.execute(text(f"SELECT id, session_id, from_user_id, to_user_id, type, content, media_url, is_read, revoked_at, created_at FROM chat_message WHERE {clause} ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)).mappings().all()
    total = int((await db.execute(text(f"SELECT COUNT(*) FROM chat_message WHERE {clause}"), {k:v for k,v in params.items() if k not in ("limit","offset")})).scalar() or 0)
    return AdminMessagePage(items=[AdminMessageItem(**dict(row)) for row in rows], page=page, page_size=page_size, total=total, has_more=page*page_size<total)

async def moderate_admin_message(db: AsyncSession, admin_id: int, message_id: int, action: str, reason: str) -> AdminMessageItem:
    row = (await db.execute(text("SELECT id, session_id, from_user_id, to_user_id, type, content, media_url, is_read, revoked_at, created_at FROM chat_message WHERE id=:id FOR UPDATE"), {"id": message_id})).mappings().first()
    if not row: raise HTTPException(404, detail="?????")
    if action == "recall":
        await db.execute(text("UPDATE chat_message SET revoked_at=COALESCE(revoked_at, UTC_TIMESTAMP()) WHERE id=:id"), {"id": message_id})
    elif action == "restore":
        await db.execute(text("UPDATE chat_message SET revoked_at=NULL WHERE id=:id"), {"id": message_id})
    else: raise HTTPException(422, detail="??????????")
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id, reason) VALUES (:actor, :action, 'chat_message', :id, :reason)"), {"actor":admin_id,"action":"message."+action,"id":message_id,"reason":reason})
    await db.commit()
    updated=(await db.execute(text("SELECT id, session_id, from_user_id, to_user_id, type, content, media_url, is_read, revoked_at, created_at FROM chat_message WHERE id=:id"), {"id":message_id})).mappings().one()
    return AdminMessageItem(**dict(updated))

async def create_admin_announcement(db: AsyncSession, admin_id: int, body: AdminAnnouncementCreate) -> AdminAnnouncementItem:
    result=await db.execute(text("INSERT INTO admin_announcement (category,title,link_to,published_at) VALUES (:category,:title,:link_to,:published_at)"), {**body.model_dump()})
    await db.execute(text("INSERT INTO business_audit_log (actor_user_id, action, resource_type, resource_id) VALUES (:actor,'announcement.create','admin_announcement',:id)"), {"actor":admin_id,"id":result.lastrowid})
    await db.commit()
    row=(await db.execute(text("SELECT id, category, title, link_to, published_at, created_at FROM admin_announcement WHERE id=:id"), {"id":result.lastrowid})).mappings().one()
    return AdminAnnouncementItem(**dict(row))
