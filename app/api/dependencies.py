"""Common request dependencies and authenticated-user guards."""

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class CurrentUser:
    id: int
    session_id: int
    phone: str | None
    status: int
    realname_status: int
    face_verified: int | None = None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
        session_id = int(payload["sid"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="无效或已过期的访问令牌") from exc

    result = await db.execute(
        text(
            """SELECT u.id, u.phone, u.status, COALESCE(ua.realname_status, 0) AS realname_status,
                      COALESCE(ua.face_verified, 0) AS face_verified
               FROM users u LEFT JOIN user_auth ua ON ua.user_id = u.id
               JOIN user_session s ON s.user_id = u.id
               WHERE u.id = :user_id AND s.id = :session_id AND s.status = 1
                 AND s.revoked_at IS NULL AND s.access_expire_at > UTC_TIMESTAMP()"""
        ),
        {"user_id": user_id, "session_id": session_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=401, detail="登录状态已失效")
    if int(row["status"]) != 1:
        raise HTTPException(status_code=403, detail="账号当前不可用")
    await db.execute(
        text("UPDATE user_session SET last_used_at = UTC_TIMESTAMP() WHERE id = :id"),
        {"id": session_id},
    )
    await db.commit()
    values = dict(row)
    values.setdefault("face_verified", None)
    return CurrentUser(**values, session_id=session_id)


async def get_verified_user(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Require a verified phone before social discovery and interaction actions."""
    if not current.phone:
        raise HTTPException(status_code=403, detail="请先绑定手机号")
    return current


async def get_realname_verified_user(
    current: CurrentUser = Depends(get_verified_user),
) -> CurrentUser:
    if current.realname_status != 2:
        raise HTTPException(status_code=403, detail="请先完成实名认证")
    if current.face_verified is not None and current.face_verified != 1:
        raise HTTPException(status_code=403, detail="璇峰厛瀹屾垚浜鸿劯璁よ瘉")
    return current


async def get_browsable_user(
    current: CurrentUser = Depends(get_verified_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """Require the single server-side gate shared by homepage discovery APIs."""
    result = await db.execute(
        text("SELECT COALESCE(score, 0) FROM user_profile_completion WHERE user_id = :user_id"),
        {"user_id": current.id},
    )
    if float(result.scalar() or 0) < 100:
        raise HTTPException(status_code=403, detail="请先完善资料后再进入首页")
    return current


async def get_current_admin(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    """校验当前用户拥有有效管理员角色。"""
    result = await db.execute(
        text("""SELECT 1 FROM user_role
                WHERE user_id = :user_id AND role_code = 'admin' AND status = 1
                LIMIT 1"""),
        {"user_id": current.id},
    )
    if not result.scalar():
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current
