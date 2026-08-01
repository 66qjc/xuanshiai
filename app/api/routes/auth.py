"""用户认证和账号安全接口。"""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_current_user
from app.core.config import settings
from app.core.security import decode_access_token
from app.db.session import get_db
from app.schemas.auth import (
    AgreementAcceptRequest,
    BindPhoneRequest,
    PhoneLoginRequest,
    RefreshRequest,
    RealNameRequest,
    SmsSendRequest,
    SmsSendResponse,
    TokenResponse,
    ExistingAccountLoginRequest,
    UserResponse,
    WechatLoginRequest,
)
from app.services import auth
from app.services.presence import mark_session_offline, mark_user_offline
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

router = APIRouter(prefix="/auth")
bearer = HTTPBearer(auto_error=False)


def request_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/sms/send", response_model=SmsSendResponse, status_code=status.HTTP_202_ACCEPTED, summary="发送短信验证码")
async def send_sms(request: Request, body: SmsSendRequest) -> SmsSendResponse:
    """发送登录或绑定手机号所需的短信验证码。"""
    expires = await auth.sms_store.issue(body.phone, body.purpose, request_ip(request), None)
    return SmsSendResponse(message="验证码已发送", expires_in=expires, retry_after=settings.sms_send_interval_seconds)


@router.post("/phone/login", response_model=TokenResponse, summary="手机号验证码登录")
async def phone_login(request: Request, body: PhoneLoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """使用手机号和短信验证码登录，未注册手机号会自动创建账号。"""
    return await auth.login_phone(db, body, request_ip(request), request.headers.get("user-agent"))


@router.post("/wechat/login", response_model=TokenResponse, summary="微信登录")
async def wechat_login(request: Request, body: WechatLoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """使用微信登录凭证换取用户身份并创建登录会话。"""
    return await auth.login_wechat(db, body, request_ip(request), request.headers.get("user-agent"))


@router.post("/test-login", response_model=TokenResponse, summary="使用已有账号测试登录")
async def test_login(request: Request, body: ExistingAccountLoginRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Development/testing only: issue tokens for an existing active account."""
    return await auth.login_existing_account(db, body, request_ip(request), request.headers.get("user-agent"))


@router.post("/bind-phone", status_code=status.HTTP_204_NO_CONTENT, summary="绑定手机号")
async def bind_phone(request: Request, body: BindPhoneRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    """为当前微信账号绑定未被其他账号使用的手机号。"""
    await auth.bind_phone(db, current.id, body, request_ip(request))
    await db.commit()


@router.post("/refresh", response_model=TokenResponse, summary="刷新登录令牌")
async def refresh(request: Request, body: RefreshRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """轮换 Refresh Token 并签发新的访问令牌。"""
    return await auth.refresh_session(db, body, request_ip(request), request.headers.get("user-agent"))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="退出当前设备")
async def logout(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: AsyncSession = Depends(get_db)) -> None:
    """撤销当前设备的登录会话。"""
    if credentials:
        try:
            payload = decode_access_token(credentials.credentials)
            await auth.revoke_session(db, int(payload["sid"]))
            await mark_session_offline(db, int(payload["sub"]), int(payload["sid"]))
            await db.commit()
        except (ValueError, KeyError):
            pass


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT, summary="退出所有设备")
async def logout_all(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    """撤销当前账号的全部有效登录会话。"""
    await db.execute(text("UPDATE user_session SET status = 2, revoked_at = UTC_TIMESTAMP(), revoke_reason = 'logout_all' WHERE user_id = :id AND status = 1"), {"id": current.id})
    await mark_user_offline(db, current.id)
    await db.commit()


@router.get("/me", response_model=UserResponse, summary="获取当前用户信息")
async def me(current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """返回当前用户基本信息、手机号脱敏结果和认证状态。"""
    result = await db.execute(text("SELECT id, phone, nickname, avatar, status, phone_verified_at FROM users WHERE id = :id"), {"id": current.id})
    row = result.mappings().first()
    phone = row["phone"] if row else None
    return {"id": current.id, "phone_masked": f"{phone[:3]}****{phone[-4:]}" if phone else None,
            "nickname": row["nickname"] if row else None, "avatar": row["avatar"] if row else None,
            "status": row["status"] if row else current.status, "phone_verified": bool(row and row["phone_verified_at"]),
            "realname_status": current.realname_status, "need_bind_phone": not bool(phone)}


@router.post("/agreements/accept", status_code=status.HTTP_204_NO_CONTENT, summary="签署用户协议")
async def accept_agreement(request: Request, body: AgreementAcceptRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> None:
    """记录当前用户对指定协议版本的确认结果。"""
    await auth.accept_agreement(db, current.id, body.agreement_type, body.version, body.content_hash, body.scene, request_ip(request), None)
    await db.commit()


@router.get("/agreements", summary="查询当前协议版本")
async def agreements() -> dict[str, dict[str, str]]:
    """返回当前发布的协议版本，不暴露协议正文。"""
    return {agreement_type: {"type": agreement_type, "version": version}
            for agreement_type, version in settings.agreement_versions.items()}


@router.post("/realname", status_code=status.HTTP_200_OK, summary="提交实名认证")
async def realname(body: RealNameRequest, current: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict:
    """提交姓名和身份证信息，进入实名认证审核流程。"""
    return await auth.submit_realname(db, current.id, body)
