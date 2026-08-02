"""Authentication business rules for SMS, WeChat and session management."""

import asyncio
import hashlib
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import (
    create_token,
    encrypt_sensitive,
    hash_passwordless_code,
    hash_token,
    mask_id_card,
    random_token,
    verify_password,
)
from app.schemas.auth import (
    BindPhoneRequest,
    PhoneLoginRequest,
    RealNameRequest,
    RefreshRequest,
    WechatLoginRequest,
    ExistingAccountLoginRequest,
)
from app.services.sms.providers import get_sms_provider
from app.services.wechat.providers import get_wechat_provider
from app.services.presence import mark_session_online


def normalize_user_agent(user_agent: str | None) -> str | None:
    """Keep request metadata within the database column size used by login records."""
    return user_agent[:255] if user_agent else user_agent


class SmsStore:
    """Process-local fallback for development; production must use Redis."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        self._daily: dict[tuple[str, str], tuple[date, int]] = {}
        self._lock = asyncio.Lock()

    async def issue(self, phone: str, purpose: str, ip: str, device_id: str | None) -> int:
        if settings.sms_provider.lower() == "disabled":
            await get_sms_provider().send_code(phone, purpose, "")
        now = datetime.now(UTC)
        async with self._lock:
            key = (phone, purpose)
            old = self._items.get(key)
            if old and old["sent_at"] + timedelta(seconds=settings.sms_send_interval_seconds) > now:
                retry = int((old["sent_at"] + timedelta(seconds=settings.sms_send_interval_seconds) - now).total_seconds())
                raise HTTPException(429, detail=f"发送过于频繁，请{max(1, retry)}秒后重试")
            count_key = (phone, purpose)
            day, count = self._daily.get(count_key, (now.date(), 0))
            if day != now.date():
                count = 0
            if count >= settings.sms_daily_limit:
                raise HTTPException(429, detail="今日验证码发送次数已达上限")
            code = settings.sms_mock_code if settings.sms_provider.lower() == "mock" else f"{secrets.randbelow(1_000_000):06d}"
            await get_sms_provider().send_code(phone, purpose, code)
            self._items[key] = {
                "hash": hash_passwordless_code(code),
                "sent_at": now,
                "expires_at": now + timedelta(seconds=settings.sms_code_expire_seconds),
                "attempts": 0,
                "locked_until": None,
                "ip": ip,
                "device_id": device_id,
            }
            self._daily[count_key] = (now.date(), count + 1)
        return settings.sms_code_expire_seconds

    async def verify(self, phone: str, purpose: str, code: str) -> None:
        now = datetime.now(UTC)
        async with self._lock:
            item = self._items.get((phone, purpose))
            if not item:
                raise HTTPException(400, detail="验证码不存在或已过期")
            if item["locked_until"] and item["locked_until"] > now:
                raise HTTPException(429, detail="验证码错误次数过多，请稍后重试")
            if item["expires_at"] <= now:
                self._items.pop((phone, purpose), None)
                raise HTTPException(400, detail="验证码已过期")
            if not secrets.compare_digest(item["hash"], hash_passwordless_code(code)):
                item["attempts"] += 1
                if item["attempts"] >= 3:
                    item["locked_until"] = now + timedelta(minutes=15)
                    raise HTTPException(429, detail="验证码错误次数过多，请15分钟后重试")
                raise HTTPException(400, detail="验证码错误")
            self._items.pop((phone, purpose), None)


sms_store = SmsStore()


async def exchange_wechat_code(code: str) -> dict[str, str]:
    if settings.wechat_provider.lower() == "mock":
        return await get_wechat_provider().exchange_code(code)
    if not settings.wechat_app_id or not settings.wechat_app_secret:
        raise HTTPException(503, detail="微信登录服务未配置")
    url = "https://api.weixin.qq.com/sns/jscode2session"
    params = {
        "appid": settings.wechat_app_id,
        "secret": settings.wechat_app_secret,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, detail="微信登录服务暂时不可用") from exc
    if data.get("errcode") or not data.get("openid"):
        raise HTTPException(400, detail="微信登录凭证无效")
    return {key: data[key] for key in ("openid", "unionid") if key in data}


async def create_session(
    db: AsyncSession,
    user_id: int,
    device_id: str | None,
    platform: str | None,
    app_version: str | None,
    ip: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    await db.execute(
        text(
            """UPDATE user_session SET status = 2, revoked_at = UTC_TIMESTAMP(), revoke_reason = 'session_limit'
               WHERE user_id = :user_id AND status = 1
                 AND id NOT IN (SELECT id FROM (SELECT id FROM user_session WHERE user_id = :user_id
                   AND status = 1 ORDER BY created_at DESC LIMIT :keep) recent)"""
        ),
        {"user_id": user_id, "keep": max(0, settings.max_sessions_per_user - 1)},
    )
    refresh = random_token()
    now = datetime.now(UTC).replace(tzinfo=None)
    access_expire = now + timedelta(minutes=settings.access_token_expire_minutes)
    refresh_expire = now + timedelta(days=settings.refresh_token_expire_days)
    result = await db.execute(
        text(
            """INSERT INTO user_session
               (user_id, refresh_token_hash, device_id, platform, app_version, ip, user_agent,
                created_at, last_used_at, access_expire_at, refresh_expire_at, status)
               VALUES (:user_id, :refresh_hash, :device_id, :platform, :app_version, :ip, :ua,
                       UTC_TIMESTAMP(), UTC_TIMESTAMP(), :access_expire, :refresh_expire, 1)"""
        ),
        {"user_id": user_id, "refresh_hash": hash_token(refresh), "device_id": device_id,
         "platform": platform, "app_version": app_version, "ip": ip, "ua": user_agent,
         "access_expire": access_expire, "refresh_expire": refresh_expire},
    )
    session_id = result.lastrowid
    access = create_token(user_id, session_id, "access", timedelta(minutes=settings.access_token_expire_minutes))
    await db.execute(
        text("UPDATE user_session SET access_token_hash = :hash WHERE id = :id"),
        {"hash": hash_token(access), "id": session_id},
    )
    await mark_session_online(db, user_id, int(session_id))
    return {"access_token": access, "refresh_token": refresh, "expires_in": settings.access_token_expire_minutes * 60,
            "user_id": user_id}


async def get_or_create_user_by_phone(db: AsyncSession, phone: str, ip: str | None) -> int:
    result = await db.execute(text("SELECT id, status FROM users WHERE phone = :phone FOR UPDATE"), {"phone": phone})
    row = result.mappings().first()
    if row:
        if row["status"] != 1:
            raise HTTPException(403, detail="账号当前不可用")
        user_id = row["id"]
        await ensure_default_user_role(db, user_id)
        return user_id
    result = await db.execute(
        text("INSERT INTO users (phone, phone_verified_at, register_ip) VALUES (:phone, UTC_TIMESTAMP(), :ip)"),
        {"phone": phone, "ip": ip},
    )
    user_id = result.lastrowid
    await ensure_default_user_role(db, user_id)
    return user_id


async def ensure_default_user_role(db: AsyncSession, user_id: int) -> None:
    """为历史或新建用户补齐普通用户角色，避免角色查询为空。"""
    await db.execute(text("""INSERT INTO user_role (user_id, role_code, status)
        VALUES (:user_id, 'user', 1)
        ON DUPLICATE KEY UPDATE status = 1"""), {"user_id": user_id})


async def login_phone(db: AsyncSession, request: PhoneLoginRequest, ip: str | None, user_agent: str | None) -> dict[str, Any]:
    if request.purpose != "login":
        raise HTTPException(422, detail="手机号登录验证码用途必须为login")
    await sms_store.verify(request.phone, request.purpose, request.code)
    user_agent = normalize_user_agent(user_agent)
    async with db.begin():
        user_id = await get_or_create_user_by_phone(db, request.phone, ip)
        await db.execute(text("UPDATE users SET phone_verified_at = UTC_TIMESTAMP(), last_login_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": user_id})
        tokens = await create_session(db, user_id, request.device_id, request.platform, request.app_version, ip, user_agent)
        await db.execute(text("INSERT INTO user_login_log (user_id, login_type, ip, device) VALUES (:id, 2, :ip, :device)"), {"id": user_id, "ip": ip, "device": user_agent})
    tokens["need_bind_phone"] = False
    return tokens


async def login_wechat(db: AsyncSession, request: WechatLoginRequest, ip: str | None, user_agent: str | None) -> dict[str, Any]:
    identity = await exchange_wechat_code(request.code)
    user_agent = normalize_user_agent(user_agent)
    async with db.begin():
        result = await db.execute(text("SELECT id, phone, status FROM users WHERE openid = :openid FOR UPDATE"), {"openid": identity["openid"]})
        row = result.mappings().first()
        if row and row["status"] != 1:
            raise HTTPException(403, detail="账号当前不可用")
        if row:
            user_id = row["id"]
            await db.execute(text("UPDATE users SET nickname = COALESCE(:nickname, nickname), avatar = COALESCE(:avatar, avatar), last_login_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": user_id, "nickname": request.nickname, "avatar": request.avatar})
            phone = row["phone"]
        else:
            result = await db.execute(text("INSERT INTO users (openid, unionid, nickname, avatar, register_ip) VALUES (:openid, :unionid, :nickname, :avatar, :ip)"), {"openid": identity["openid"], "unionid": identity.get("unionid"), "nickname": request.nickname, "avatar": request.avatar, "ip": ip})
            user_id, phone = result.lastrowid, None
        await ensure_default_user_role(db, user_id)
        tokens = await create_session(db, user_id, request.device_id, request.platform, request.app_version, ip, user_agent)
        await db.execute(text("INSERT INTO user_login_log (user_id, login_type, ip, device) VALUES (:id, 1, :ip, :device)"), {"id": user_id, "ip": ip, "device": user_agent})
    tokens["need_bind_phone"] = not bool(phone)
    return tokens


async def login_existing_account(db: AsyncSession, request: ExistingAccountLoginRequest, ip: str | None, user_agent: str | None) -> dict[str, Any]:
    """Issue a normal session for an existing account without SMS/WeChat."""
    if not settings.is_test_mode:
        raise HTTPException(403, detail="已有账号测试登录仅允许在 development/testing 环境使用")
    user_agent = normalize_user_agent(user_agent)
    async with db.begin():
        result = await db.execute(text("SELECT id, phone, password, status FROM users WHERE phone = :phone FOR UPDATE"), {"phone": request.phone})
        row = result.mappings().first()
        if not row:
            raise HTTPException(404, detail="账号不存在")
        if int(row["status"]) != 1:
            raise HTTPException(403, detail="账号当前不可用")
        if not row["password"] or not verify_password(request.password, row["password"]):
            raise HTTPException(401, detail="手机号或密码错误")
        await ensure_default_user_role(db, int(row["id"]))
        await db.execute(text("UPDATE users SET last_login_at = UTC_TIMESTAMP() WHERE id = :id"), {"id": row["id"]})
        tokens = await create_session(db, int(row["id"]), request.device_id, request.platform, request.app_version, ip, user_agent)
        await db.execute(text("INSERT INTO user_login_log (user_id, login_type, ip, device) VALUES (:id, 3, :ip, :device)"), {"id": row["id"], "ip": ip, "device": user_agent})
    tokens["need_bind_phone"] = not bool(row["phone"])
    return tokens


async def bind_phone(db: AsyncSession, user_id: int, request: BindPhoneRequest, ip: str | None) -> None:
    if request.purpose != "bind_phone":
        raise HTTPException(422, detail="绑定手机号验证码用途必须为bind_phone")
    await sms_store.verify(request.phone, "bind_phone", request.code)
    async with db.begin():
        result = await db.execute(text("SELECT id, phone FROM users WHERE id = :id FOR UPDATE"), {"id": user_id})
        current = result.mappings().first()
        if not current:
            raise HTTPException(404, detail="用户不存在")
        if current["phone"] == request.phone:
            return
        result = await db.execute(text("SELECT id FROM users WHERE phone = :phone FOR UPDATE"), {"phone": request.phone})
        if result.first():
            raise HTTPException(409, detail="该手机号已绑定其他账号")
        await db.execute(text("UPDATE users SET phone = :phone, phone_verified_at = UTC_TIMESTAMP(), updated_at = UTC_TIMESTAMP() WHERE id = :id"), {"phone": request.phone, "id": user_id})


async def refresh_session(db: AsyncSession, request: RefreshRequest, ip: str | None, user_agent: str | None) -> dict[str, Any]:
    async with db.begin():
        result = await db.execute(text("SELECT id, user_id, device_id, platform, app_version, refresh_expire_at FROM user_session WHERE refresh_token_hash = :hash AND status = 1 FOR UPDATE"), {"hash": hash_token(request.refresh_token)})
        row = result.mappings().first()
        if not row or row["refresh_expire_at"] <= datetime.now(UTC).replace(tzinfo=None):
            raise HTTPException(401, detail="刷新令牌无效或已过期")
        await db.execute(text("UPDATE user_session SET status = 2, revoked_at = UTC_TIMESTAMP(), revoke_reason = 'rotated' WHERE id = :id"), {"id": row["id"]})
        tokens = await create_session(db, row["user_id"], row["device_id"], row["platform"], row["app_version"], ip, user_agent)
    result = await db.execute(text("SELECT phone IS NOT NULL AS has_phone FROM users WHERE id = :id"), {"id": row["user_id"]})
    tokens["need_bind_phone"] = not bool(result.scalar())
    return tokens


async def revoke_session(db: AsyncSession, session_id: int) -> None:
    await db.execute(text("UPDATE user_session SET status = 2, revoked_at = UTC_TIMESTAMP(), revoke_reason = 'logout' WHERE id = :id AND status = 1"), {"id": session_id})


async def accept_agreement(db: AsyncSession, user_id: int, agreement_type: str, version: str, content_hash: str | None, scene: str | None, ip: str | None, device_id: str | None) -> None:
    current = settings.agreement_versions.get(agreement_type)
    if not current or version != current:
        raise HTTPException(409, detail="协议版本不是当前发布版本")
    await db.execute(text("INSERT INTO user_agreement_acceptance (user_id, agreement_type, agreement_version, content_hash, accepted_ip, device_id, scene) VALUES (:uid, :type, :version, :content_hash, :ip, :device, :scene) ON DUPLICATE KEY UPDATE accepted_at = UTC_TIMESTAMP(), accepted_ip = VALUES(accepted_ip), device_id = VALUES(device_id), scene = VALUES(scene), status = 1"), {"uid": user_id, "type": agreement_type, "version": version, "content_hash": content_hash, "ip": ip, "device": device_id, "scene": scene})
    if agreement_type == "safety_pledge":
        await db.execute(text("UPDATE users SET is_single_pledge = 1, updated_at = UTC_TIMESTAMP() WHERE id = :uid"), {"uid": user_id})


def calculate_age(birthday: date) -> int:
    today = date.today()
    return today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))


async def submit_realname(db: AsyncSession, user_id: int, request: RealNameRequest) -> dict[str, Any]:
    birth = date(int(request.id_card[6:10]), int(request.id_card[10:12]), int(request.id_card[12:14]))
    if calculate_age(birth) < 18:
        raise HTTPException(422, detail="仅支持年满18周岁的用户认证")
    card_hash = hashlib.sha256(request.id_card.upper().encode()).hexdigest()
    async with db.begin():
        duplicate = await db.execute(text("SELECT user_id FROM user_auth WHERE id_card_hash = :hash AND user_id <> :uid AND realname_status = 2"), {"hash": card_hash, "uid": user_id})
        if duplicate.first():
            raise HTTPException(409, detail="该身份信息已被其他账号认证")
        current = await db.execute(text("SELECT realname_status FROM user_auth WHERE user_id = :uid FOR UPDATE"), {"uid": user_id})
        existing = current.scalar()
        if existing == 2:
            raise HTTPException(409, detail="实名认证通过后不能修改认证信息")
        await db.execute(text("""INSERT INTO user_auth (user_id, real_name, id_card, id_card_hash, id_card_masked, realname_status, auth_status, auth_step, submitted_at, retry_count)
             VALUES (:uid, :name, :card, :hash, :masked, 1, 1, 3, UTC_TIMESTAMP(), 1)
             ON DUPLICATE KEY UPDATE real_name = VALUES(real_name), id_card = VALUES(id_card), id_card_hash = VALUES(id_card_hash), id_card_masked = VALUES(id_card_masked), realname_status = 1, auth_status = 1, auth_step = 3, submitted_at = UTC_TIMESTAMP(), retry_count = retry_count + 1, fail_reason = NULL"""), {"uid": user_id, "name": request.real_name, "card": encrypt_sensitive(request.id_card), "hash": card_hash, "masked": mask_id_card(request.id_card)})
        await db.execute(text("UPDATE users SET birthday = COALESCE(birthday, :birthday) WHERE id = :uid"), {"uid": user_id, "birthday": birth})
    return {"status": 1, "id_card_masked": mask_id_card(request.id_card)}


