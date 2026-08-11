"""Administration services for independent matchmaker back-office accounts."""

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.schemas.matchmaker_admin_account import (
    MatchmakerAdminAccountCreate,
    MatchmakerAdminAccountItem,
    MatchmakerAdminAccountPage,
    MatchmakerAdminAccountUpdate,
    MatchmakerAdminAccountStatusUpdate,
    MatchmakerAdminLoginLogItem,
    MatchmakerAdminLoginLogPage,
    MatchmakerAdminPasswordReset,
    MatchmakerAdminSessionItem,
    MatchmakerAdminSessionPage,
)


def _permissions(row: dict) -> list[str]:
    value = row.get("permissions")
    if not value:
        return []
    return [item for item in str(value).split("\n") if item]


def _account_item(row: dict) -> MatchmakerAdminAccountItem:
    return MatchmakerAdminAccountItem(
        **{key: row[key] for key in (
            "id", "username", "display_name", "matchmaker_user_id", "status",
            "failed_count", "locked_until", "last_login_at", "last_login_ip",
            "created_at", "updated_at",
        )},
        permissions=_permissions(row),
    )


async def list_accounts(
    db: AsyncSession,
    page: int,
    page_size: int,
    username: str | None = None,
    display_name: str | None = None,
    status: int | None = None,
    matchmaker_user_id: int | None = None,
) -> MatchmakerAdminAccountPage:
    conditions = ["1 = 1"]
    params: dict[str, object] = {
        "limit": page_size,
        "offset": (page - 1) * page_size,
    }
    if username:
        conditions.append("a.username LIKE CONCAT('%', :username, '%')")
        params["username"] = username
    if display_name:
        conditions.append("a.display_name LIKE CONCAT('%', :display_name, '%')")
        params["display_name"] = display_name
    if status is not None:
        conditions.append("a.status = :status")
        params["status"] = status
    if matchmaker_user_id is not None:
        conditions.append("a.matchmaker_user_id = :matchmaker_user_id")
        params["matchmaker_user_id"] = matchmaker_user_id
    where = " AND ".join(conditions)
    select = """SELECT a.*, GROUP_CONCAT(p.permission ORDER BY p.permission SEPARATOR '\n') AS permissions
        FROM matchmaker_admin_account a
        LEFT JOIN matchmaker_admin_permission p ON p.account_id = a.id
        WHERE {where}
        GROUP BY a.id
        ORDER BY a.created_at DESC
        LIMIT :limit OFFSET :offset"""
    rows = await db.execute(text(select.format(where=where)), params)
    count = await db.execute(
        text(f"SELECT COUNT(*) FROM matchmaker_admin_account a WHERE {where}"),
        {key: value for key, value in params.items() if key not in {"limit", "offset"}},
    )
    total = int(count.scalar() or 0)
    return MatchmakerAdminAccountPage(
        items=[_account_item(dict(row)) for row in rows.mappings().all()],
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
    )


async def get_account(db: AsyncSession, account_id: int) -> MatchmakerAdminAccountItem:
    result = await db.execute(text("""SELECT a.*, GROUP_CONCAT(p.permission ORDER BY p.permission SEPARATOR '\n') AS permissions
        FROM matchmaker_admin_account a
        LEFT JOIN matchmaker_admin_permission p ON p.account_id = a.id
        WHERE a.id = :id GROUP BY a.id"""), {"id": account_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(404, detail="后台账号不存在")
    return _account_item(dict(row))


async def create_account(db: AsyncSession, body: MatchmakerAdminAccountCreate, actor_id: int) -> MatchmakerAdminAccountItem:
    duplicate = await db.execute(
        text("SELECT 1 FROM matchmaker_admin_account WHERE username = :username"),
        {"username": body.username},
    )
    if duplicate.scalar():
        raise HTTPException(409, detail="后台账号用户名已存在")
    result = await db.execute(text("""INSERT INTO matchmaker_admin_account
        (username, password_hash, matchmaker_user_id, display_name)
        VALUES (:username, :password_hash, :matchmaker_user_id, :display_name)"""), {
        "username": body.username,
        "password_hash": hash_password(body.password),
        "matchmaker_user_id": body.matchmaker_user_id,
        "display_name": body.display_name,
    })
    account_id = int(result.lastrowid)
    for permission in set(body.permissions):
        await db.execute(text("""INSERT INTO matchmaker_admin_permission (account_id, permission)
            VALUES (:account_id, :permission)"""), {"account_id": account_id, "permission": permission})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'admin_account.create', 'matchmaker_admin_account', :resource_id, NULL)"""), {
        "actor_id": actor_id, "resource_id": account_id,
    })
    await db.commit()
    return await get_account(db, account_id)


async def update_account(db: AsyncSession, account_id: int, body: MatchmakerAdminAccountUpdate, actor_id: int) -> MatchmakerAdminAccountItem:
    await get_account(db, account_id)
    updates: list[str] = []
    params: dict[str, object] = {"id": account_id}
    if body.display_name is not None:
        updates.append("display_name = :display_name")
        params["display_name"] = body.display_name
    if body.matchmaker_user_id is not None:
        updates.append("matchmaker_user_id = :matchmaker_user_id")
        params["matchmaker_user_id"] = body.matchmaker_user_id
    if updates:
        updates.append("updated_at = UTC_TIMESTAMP()")
        await db.execute(text(f"UPDATE matchmaker_admin_account SET {', '.join(updates)} WHERE id = :id"), params)
    if body.permissions is not None:
        await db.execute(text("DELETE FROM matchmaker_admin_permission WHERE account_id = :id"), {"id": account_id})
        for permission in set(body.permissions):
            await db.execute(text("""INSERT INTO matchmaker_admin_permission (account_id, permission)
                VALUES (:account_id, :permission)"""), {"account_id": account_id, "permission": permission})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'admin_account.update', 'matchmaker_admin_account', :resource_id, NULL)"""), {
        "actor_id": actor_id, "resource_id": account_id,
    })
    await db.commit()
    return await get_account(db, account_id)


async def update_account_status(db: AsyncSession, account_id: int, body: MatchmakerAdminAccountStatusUpdate, actor_id: int) -> MatchmakerAdminAccountItem:
    await get_account(db, account_id)
    await db.execute(text("UPDATE matchmaker_admin_account SET status = :status, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {
        "status": body.status, "id": account_id,
    })
    if body.status != 1:
        await db.execute(text("UPDATE matchmaker_admin_session SET status = 2, revoked_at = UTC_TIMESTAMP() WHERE account_id = :id AND status = 1"), {"id": account_id})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'admin_account.status.update', 'matchmaker_admin_account', :resource_id, :reason)"""), {
        "actor_id": actor_id, "resource_id": account_id, "reason": body.reason,
    })
    await db.commit()
    return await get_account(db, account_id)


async def reset_password(db: AsyncSession, account_id: int, body: MatchmakerAdminPasswordReset, actor_id: int) -> MatchmakerAdminAccountItem:
    await get_account(db, account_id)
    await db.execute(text("UPDATE matchmaker_admin_account SET password_hash = :password_hash, failed_count = 0, locked_until = NULL, updated_at = UTC_TIMESTAMP() WHERE id = :id"), {
        "password_hash": hash_password(body.new_password), "id": account_id,
    })
    await db.execute(text("UPDATE matchmaker_admin_session SET status = 2, revoked_at = UTC_TIMESTAMP() WHERE account_id = :id AND status = 1"), {"id": account_id})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'admin_account.password.reset', 'matchmaker_admin_account', :resource_id, :reason)"""), {
        "actor_id": actor_id, "resource_id": account_id, "reason": body.reason,
    })
    await db.commit()
    return await get_account(db, account_id)


async def list_sessions(db: AsyncSession, account_id: int, page: int, page_size: int) -> MatchmakerAdminSessionPage:
    await get_account(db, account_id)
    params = {"account_id": account_id, "limit": page_size, "offset": (page - 1) * page_size}
    rows = await db.execute(text("""SELECT id, account_id, ip, user_agent, access_expire_at,
        refresh_expire_at, last_used_at, status, revoked_at
        FROM matchmaker_admin_session WHERE account_id = :account_id
        ORDER BY id DESC LIMIT :limit OFFSET :offset"""), params)
    count = await db.execute(text("SELECT COUNT(*) FROM matchmaker_admin_session WHERE account_id = :account_id"), params)
    total = int(count.scalar() or 0)
    return MatchmakerAdminSessionPage(
        items=[MatchmakerAdminSessionItem(**dict(row)) for row in rows.mappings().all()],
        page=page, page_size=page_size, total=total, has_more=page * page_size < total,
    )


async def revoke_all_sessions(db: AsyncSession, account_id: int, actor_id: int) -> None:
    await get_account(db, account_id)
    await db.execute(text("UPDATE matchmaker_admin_session SET status = 2, revoked_at = UTC_TIMESTAMP() WHERE account_id = :id AND status = 1"), {"id": account_id})
    await db.execute(text("""INSERT INTO business_audit_log
        (actor_user_id, action, resource_type, resource_id, reason)
        VALUES (:actor_id, 'admin_account.sessions.revoke_all', 'matchmaker_admin_account', :resource_id, NULL)"""), {
        "actor_id": actor_id, "resource_id": account_id,
    })
    await db.commit()


async def list_login_logs(db: AsyncSession, page: int, page_size: int, account_id: int | None = None) -> MatchmakerAdminLoginLogPage:
    condition = "1 = 1"
    params: dict[str, object] = {"limit": page_size, "offset": (page - 1) * page_size}
    if account_id is not None:
        condition += " AND l.account_id = :account_id"
        params["account_id"] = account_id
    rows = await db.execute(text(f"""SELECT l.id, l.account_id, l.username, l.login_status, l.ip,
        l.user_agent, l.device_id, l.failure_reason, l.created_at
        FROM matchmaker_admin_login_log l WHERE {condition}
        ORDER BY l.id DESC LIMIT :limit OFFSET :offset"""), params)
    count = await db.execute(text(f"SELECT COUNT(*) FROM matchmaker_admin_login_log l WHERE {condition}"), params)
    total = int(count.scalar() or 0)
    return MatchmakerAdminLoginLogPage(
        items=[MatchmakerAdminLoginLogItem(**dict(row)) for row in rows.mappings().all()],
        page=page, page_size=page_size, total=total, has_more=page * page_size < total,
    )
