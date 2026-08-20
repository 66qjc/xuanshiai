"""Common request dependencies and authenticated-user guards."""

from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.services.matchmaker_admin_auth import decode_matchmaker_admin_token, get_session_account
from app.schemas.matchmaker_admin import MatchmakerAdminAccount
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
        raise HTTPException(status_code=403, detail="请先完成人脸认证")
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


@dataclass(frozen=True)
class CurrentMatchmakerAdmin:
    account: MatchmakerAdminAccount
    session_id: int
    permissions: frozenset[str] = field(default_factory=frozenset)

    def require(self, permission: str) -> None:
        aliases = {
            "finance.read": {"finance.read", "finance.write"},
            "meeting.read": {"meeting.read", "meeting.write"},
            "community.read": {"community.read", "community.moderate"},
            "matchmaker.read": {"matchmaker.read", "matchmaker.manage"},
            "matchmaker.product.read": {"matchmaker.product.read", "matchmaker.product.manage"},
            "matchmaker.service.read": {"matchmaker.service.read", "matchmaker.service.manage"},
            "matchmaker.organization.read": {"matchmaker.organization.read", "matchmaker.organization.manage"},
            "matchmaker.member.read": {"matchmaker.member.read", "matchmaker.member.manage"},
            "community.activity.read": {"community.activity.read", "community.activity.manage"},
            "community.moderate": {"community.moderate", "admin.moderate"},
            "meeting.read": {"meeting.read", "meeting.write"},
            "finance.read": {"finance.read", "finance.write"},
            "reward.read": {"reward.read", "reward.write", "matchmaker.reward.read", "matchmaker.reward.manage"},
        }
        allowed = aliases.get(permission, {permission})
        if "*" not in self.permissions and not (allowed & self.permissions):
            raise HTTPException(status_code=403, detail="????????")


    def scope_condition(
        self,
        *,
        organization_column: str,
        params: dict[str, object],
        user_column: str | None = None,
    ) -> str:
        """Return a SQL predicate for the account's declared data scope."""
        scope = self.account.data_scope
        if scope == "ALL" or "*" in self.permissions:
            return "1 = 1"
        if scope == "SELF":
            if user_column is None:
                raise HTTPException(status_code=403, detail="?????????????")
            params["scope_user_id"] = self.account.matchmaker_user_id or self.account.id
            return f"{user_column} = :scope_user_id"
        if not self.account.organization_id:
            raise HTTPException(status_code=403, detail="?????????????")
        params["scope_organization_id"] = self.account.organization_id
        return f"{organization_column} = :scope_organization_id"


def _matchmaker_admin_permission(request: Request) -> str | None:
    path = request.url.path
    method = request.method.upper()
    if path.endswith("/auth/me") or path.endswith("/auth/logout") or "/auth/" in path:
        return None
    if "/accounts" in path:
        return "matchmaker.account.manage"
    if "/members" in path:
        return "matchmaker.member.manage" if method != "GET" else "matchmaker.member.read"
    if "/matchmakers" in path:
        return "matchmaker.manage" if method != "GET" else "matchmaker.read"
    if "/service-products" in path:
        return "matchmaker.product.manage" if method != "GET" else "matchmaker.product.read"
    if "/service-requests" in path:
        return "matchmaker.service.manage" if method != "GET" else "matchmaker.service.read"
    if "/branches" in path or "/stores" in path or "/assignments" in path:
        return "matchmaker.organization.manage" if method != "GET" else "matchmaker.organization.read"
    if "/meetings" in path:
        return "meeting.write" if method != "GET" else "meeting.read"
    if "/finance" in path:
        return "finance.write" if method != "GET" else "finance.read"
    if "/activities" in path:
        return "community.activity.manage" if method != "GET" else "community.activity.read"
    if "/community" in path or "/reports" in path or "/media/" in path:
        return "community.moderate" if method != "GET" else "community.read"
    if "/reward-rules" in path:
        return "reward.write" if method != "GET" else "reward.read"
    return None


async def get_current_matchmaker_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> CurrentMatchmakerAdmin:
    """Require the independent red-matchmaker back-office session."""
    if not credentials:
        raise HTTPException(status_code=401, detail="请先登录红娘后台")
    try:
        payload = decode_matchmaker_admin_token(credentials.credentials)
        account_id = int(payload["sub"])
        session_id = int(payload["sid"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=401, detail="无效或已过期的红娘后台访问令牌") from exc
    account = await get_session_account(db, account_id, session_id)
    result = await db.execute(
        text("SELECT permission FROM matchmaker_admin_permission WHERE account_id = :id"),
        {"id": account_id},
    )
    permissions = frozenset(str(row[0]) for row in result.all())
    current = CurrentMatchmakerAdmin(
        account=account,
        session_id=session_id,
        permissions=permissions,
    )
    permission = _matchmaker_admin_permission(request)
    if permission:
        current.require(permission)
    return current
