"""Independent back-office queries and state changes for organizations."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.organization_admin import (
    AssignmentAdminItem,
    StoreAdminItem,
    StoreAdminUpdate,
    StoreMemberAdminItem,
    StoreReport,
)


def _mask(phone: str | None) -> str | None:
    return f"{phone[:3]}****{phone[-4:]}" if phone and len(phone) >= 7 else None


async def update_store(db: AsyncSession, store_id: int, body: StoreAdminUpdate, actor_id: int):
    row = (await db.execute(text(
        "SELECT id FROM organization WHERE id = :id AND org_type = 'store' FOR UPDATE"
    ), {"id": store_id})).scalar()
    if not row:
        raise HTTPException(404, detail="门店不存在")
    values = body.model_dump(exclude_unset=True)
    if values:
        assignments = ", ".join(f"{key} = :{key}" for key in values)
        await db.execute(text(
            f"UPDATE organization SET {assignments}, updated_at = UTC_TIMESTAMP() WHERE id = :id"
        ), {**values, "id": store_id})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id)
        VALUES (:actor, 'organization.update', 'organization', :id)"""),
        {"actor": actor_id, "id": store_id})
    await db.commit()
    return await get_store_admin(db, store_id)


async def get_store_admin(db: AsyncSession, store_id: int) -> StoreAdminItem:
    row = (await db.execute(text("""SELECT id, code, name, display_name, region_code,
        status, auto_redirect, created_at, updated_at
        FROM organization WHERE id = :id AND org_type = 'store'"""),
        {"id": store_id})).mappings().first()
    if not row:
        raise HTTPException(404, detail="门店不存在")
    return StoreAdminItem(**dict(row), auto_redirect=bool(row["auto_redirect"]))


async def update_store_status(db: AsyncSession, store_id: int, status: int, reason: str | None, actor_id: int):
    await get_store_admin(db, store_id)
    await db.execute(text(
        "UPDATE organization SET status = :status, updated_at = UTC_TIMESTAMP() WHERE id = :id"
    ), {"status": status, "id": store_id})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor, 'organization.status', 'organization', :id, :reason)"""),
        {"actor": actor_id, "id": store_id, "reason": reason})
    await db.commit()
    return await get_store_admin(db, store_id)


async def list_store_members(db: AsyncSession, store_id: int, page: int, page_size: int) -> tuple[list[StoreMemberAdminItem], int]:
    await get_store_admin(db, store_id)
    params = {"store_id": store_id, "limit": page_size, "offset": (page - 1) * page_size}
    rows = await db.execute(text("""SELECT om.id, om.organization_id, om.user_id,
        u.nickname, u.phone, om.role_code, om.status, om.started_at, om.ended_at
        FROM organization_member om LEFT JOIN users u ON u.id = om.user_id
        WHERE om.organization_id = :store_id ORDER BY om.id DESC
        LIMIT :limit OFFSET :offset"""), params)
    total = int((await db.execute(text(
        "SELECT COUNT(*) FROM organization_member WHERE organization_id = :store_id"
    ), {"store_id": store_id})).scalar() or 0)
    items = [StoreMemberAdminItem(**{**dict(row), "phone_masked": _mask(row["phone"])}) for row in rows.mappings().all()]
    return items, total


async def remove_store_member(db: AsyncSession, member_id: int, reason: str, actor_id: int) -> StoreMemberAdminItem:
    row = (await db.execute(text("""SELECT om.id, om.organization_id, om.user_id,
        u.nickname, u.phone, om.role_code, om.status, om.started_at, om.ended_at
        FROM organization_member om LEFT JOIN users u ON u.id = om.user_id
        WHERE om.id = :id FOR UPDATE"""), {"id": member_id})).mappings().first()
    if not row:
        raise HTTPException(404, detail="门店成员不存在")
    if int(row["status"]) != 1:
        raise HTTPException(409, detail="门店成员已结束")
    await db.execute(text("""UPDATE organization_member SET status = 3,
        ended_at = UTC_TIMESTAMP(), end_reason = :reason WHERE id = :id"""),
        {"id": member_id, "reason": reason})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor, 'organization.member.remove', 'organization_member', :id, :reason)"""),
        {"actor": actor_id, "id": member_id, "reason": reason})
    await db.commit()
    row = dict(row)
    row.update(status=3, ended_at=None, phone_masked=_mask(row.pop("phone")))
    return StoreMemberAdminItem(**row)


async def store_report(db: AsyncSession, store_id: int) -> StoreReport:
    await get_store_admin(db, store_id)
    row = (await db.execute(text("""SELECT
        (SELECT COUNT(*) FROM organization_member WHERE organization_id = :id AND status = 1) active_member_count,
        (SELECT COUNT(*) FROM resource_assignment WHERE organization_id = :id AND status = 1) active_assignment_count,
        (SELECT COUNT(*) FROM resource_assignment WHERE organization_id = :id) total_assignment_count"""),
        {"id": store_id})).mappings().one()
    return StoreReport(store_id=store_id, **dict(row))


async def list_assignments(db: AsyncSession, page: int, page_size: int, search: str | None = None):
    params = {"limit": page_size, "offset": (page - 1) * page_size}
    where = ["1 = 1"]
    if search:
        where.append("(u.nickname LIKE CONCAT('%', :search, '%') OR o.name LIKE CONCAT('%', :search, '%'))")
        params["search"] = search
    clause = " AND ".join(where)
    rows = await db.execute(text(f"""SELECT ra.id, ra.user_id, u.nickname,
        ra.organization_id, o.name organization_name, ra.matchmaker_id, mu.nickname matchmaker_name,
        ra.source, ra.status, ra.effective_at, ra.ended_at, ra.end_reason
        FROM resource_assignment ra JOIN users u ON u.id = ra.user_id
        LEFT JOIN organization o ON o.id = ra.organization_id
        LEFT JOIN users mu ON mu.id = ra.matchmaker_id
        WHERE {clause} ORDER BY ra.id DESC LIMIT :limit OFFSET :offset"""), params)
    count = await db.execute(text(f"""SELECT COUNT(*) FROM resource_assignment ra
        JOIN users u ON u.id = ra.user_id
        LEFT JOIN organization o ON o.id = ra.organization_id WHERE {clause}"""),
        {key: value for key, value in params.items() if key not in ("limit", "offset")})
    return [AssignmentAdminItem(**dict(row)) for row in rows.mappings().all()], int(count.scalar() or 0)


async def end_assignment(db: AsyncSession, assignment_id: int, reason: str, actor_id: int) -> AssignmentAdminItem:
    row = (await db.execute(text(
        "SELECT id FROM resource_assignment WHERE id = :id FOR UPDATE"
    ), {"id": assignment_id})).scalar()
    if not row:
        raise HTTPException(404, detail="资源分配不存在")
    await db.execute(text("""UPDATE resource_assignment SET status = 2,
        ended_at = UTC_TIMESTAMP(), end_reason = :reason WHERE id = :id AND status = 1"""),
        {"id": assignment_id, "reason": reason})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor, 'resource_assignment.end', 'resource_assignment', :id, :reason)"""),
        {"actor": actor_id, "id": assignment_id, "reason": reason})
    await db.commit()
    items, _ = await list_assignments(db, 1, 1)
    for item in items:
        if item.id == assignment_id:
            return item
    raise HTTPException(404, detail="资源分配不存在")
