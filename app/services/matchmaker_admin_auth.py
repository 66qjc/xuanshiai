"""Authentication for the independent matchmaker back office accounts."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_token, random_token, verify_password
from app.schemas.matchmaker_admin import (
    MatchmakerAdminAccount,
    MatchmakerAdminLoginRequest,
    MatchmakerAdminRefreshRequest,
    MatchmakerAdminTokenResponse,
)


def _account(row: Any) -> MatchmakerAdminAccount:
    return MatchmakerAdminAccount(
        id=int(row["id"]), username=row["username"], display_name=row["display_name"],
        matchmaker_user_id=int(row["matchmaker_user_id"]) if row["matchmaker_user_id"] else None,
        data_scope=str(row.get("data_scope") or "SELF"),
        organization_id=int(row["organization_id"]) if row.get("organization_id") else None,
        status=int(row["status"]), last_login_at=row["last_login_at"],
    )


def decode_matchmaker_admin_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("无效或已过期的红娘后台访问令牌") from exc
    if payload.get("typ") != "matchmaker_admin_access" or not payload.get("sub") or not payload.get("sid"):
        raise ValueError("无效的红娘后台访问令牌")
    return payload


async def _issue_session(db: AsyncSession, account: MatchmakerAdminAccount, ip: str | None, user_agent: str | None) -> MatchmakerAdminTokenResponse:
    now = datetime.now(UTC).replace(tzinfo=None)
    access_expire = now + timedelta(minutes=settings.access_token_expire_minutes)
    refresh_expire = now + timedelta(days=settings.refresh_token_expire_days)
    refresh = random_token()
    session = await db.execute(text("""INSERT INTO matchmaker_admin_session
        (account_id, refresh_token_hash, ip, user_agent, access_expire_at, refresh_expire_at, last_used_at)
        VALUES (:account_id, :refresh_hash, :ip, :user_agent, :access_expire, :refresh_expire, :now)"""), {
        "account_id": account.id, "refresh_hash": hash_token(refresh), "ip": ip,
        "user_agent": user_agent[:255] if user_agent else None,
        "access_expire": access_expire, "refresh_expire": refresh_expire, "now": now,
    })
    session_id = int(session.lastrowid)
    access = jwt.encode({"sub": str(account.id), "sid": str(session_id), "typ": "matchmaker_admin_access", "iat": now, "exp": access_expire}, settings.secret_key, algorithm=settings.jwt_algorithm)
    await db.execute(text("UPDATE matchmaker_admin_session SET access_token_hash = :hash WHERE id = :id"), {"hash": hash_token(access), "id": session_id})
    await db.commit()
    return MatchmakerAdminTokenResponse(access_token=access, refresh_token=refresh, expires_in=settings.access_token_expire_minutes * 60, account=account)


async def login(db: AsyncSession, request: MatchmakerAdminLoginRequest, ip: str | None, user_agent: str | None) -> MatchmakerAdminTokenResponse:
    result = await db.execute(text("SELECT * FROM matchmaker_admin_account WHERE username = :username"), {"username": request.username})
    row = result.mappings().first()
    now = datetime.now(UTC).replace(tzinfo=None)
    if not row or int(row["status"]) != 1 or (row["locked_until"] and row["locked_until"] > now) or not verify_password(request.password, row["password_hash"]):
        await db.execute(text("""INSERT INTO matchmaker_admin_login_log
            (account_id, username, login_status, ip, user_agent, failure_reason)
            VALUES (:account_id, :username, 0, :ip, :user_agent, :failure_reason)"""), {
            "account_id": row["id"] if row else None,
            "username": request.username,
            "ip": ip,
            "user_agent": user_agent[:255] if user_agent else None,
            "failure_reason": "invalid_credentials_or_locked",
        })
        if row:
            await db.execute(text("UPDATE matchmaker_admin_account SET failed_count = failed_count + 1, locked_until = CASE WHEN failed_count + 1 >= 5 THEN DATE_ADD(UTC_TIMESTAMP(), INTERVAL 15 MINUTE) ELSE locked_until END WHERE id = :id"), {"id": row["id"]})
            await db.commit()
        raise HTTPException(401, detail="账号或密码错误")
    await db.execute(text("UPDATE matchmaker_admin_account SET failed_count = 0, locked_until = NULL, last_login_at = UTC_TIMESTAMP(), last_login_ip = :ip WHERE id = :id"), {"id": row["id"], "ip": ip})
    await db.execute(text("""INSERT INTO matchmaker_admin_login_log
        (account_id, username, login_status, ip, user_agent)
        VALUES (:account_id, :username, 1, :ip, :user_agent)"""), {
        "account_id": row["id"], "username": request.username, "ip": ip,
        "user_agent": user_agent[:255] if user_agent else None,
    })
    await db.commit()
    row = dict(row)
    row["last_login_at"] = now
    return await _issue_session(db, _account(row), ip, user_agent)


async def get_session_account(db: AsyncSession, account_id: int, session_id: int) -> MatchmakerAdminAccount:
    result = await db.execute(text("""SELECT a.* FROM matchmaker_admin_account a JOIN matchmaker_admin_session s ON s.account_id = a.id
        WHERE a.id = :account_id AND s.id = :session_id AND a.status = 1 AND s.status = 1
          AND s.revoked_at IS NULL AND s.access_expire_at > UTC_TIMESTAMP()"""), {"account_id": account_id, "session_id": session_id})
    row = result.mappings().first()
    if not row:
        raise HTTPException(401, detail="红娘后台登录状态已失效")
    await db.execute(text("UPDATE matchmaker_admin_session SET last_used_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": session_id})
    await db.commit()
    return _account(row)


async def refresh(db: AsyncSession, request: MatchmakerAdminRefreshRequest, ip: str | None, user_agent: str | None) -> MatchmakerAdminTokenResponse:
    result = await db.execute(text("""SELECT a.*, s.id AS session_id FROM matchmaker_admin_account a JOIN matchmaker_admin_session s ON s.account_id = a.id
        WHERE s.refresh_token_hash = :token_hash AND s.status = 1 AND s.refresh_expire_at > UTC_TIMESTAMP() AND a.status = 1 FOR UPDATE"""), {"token_hash": hash_token(request.refresh_token)})
    row = result.mappings().first()
    if not row:
        raise HTTPException(401, detail="refresh_token 无效或已过期")
    await db.execute(text("UPDATE matchmaker_admin_session SET status = 3, revoked_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": row["session_id"]})
    await db.commit()
    return await _issue_session(db, _account(row), ip, user_agent)


async def logout(db: AsyncSession, session_id: int) -> None:
    await db.execute(text("UPDATE matchmaker_admin_session SET status = 2, revoked_at = UTC_TIMESTAMP() WHERE id = :id AND status = 1"), {"id": session_id})
    await db.commit()
