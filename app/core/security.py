"""Token and sensitive-value helpers used by authentication services."""

import hashlib
import secrets
import base64
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from cryptography.fernet import Fernet
import bcrypt

from app.core.config import settings




def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_token(user_id: int, session_id: int, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, str]:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("无效或已过期的访问令牌") from exc
    if payload.get("typ") != "access" or not payload.get("sub") or not payload.get("sid"):
        raise ValueError("无效的访问令牌")
    return payload


def random_token() -> str:
    return secrets.token_urlsafe(48)


def hash_passwordless_code(code: str) -> str:
    """Hash SMS codes before persistence; the raw code never enters the database."""
    return hash_token(code)


def hash_password(password: str) -> str:
    """Hash an account password; plaintext passwords must never be persisted."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a bcrypt hash without exposing comparison details."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        return False


def encrypt_sensitive(value: str) -> str:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key).encrypt(value.encode()).decode()


def mask_id_card(value: str) -> str:
    return f"{value[:4]}{'*' * max(0, len(value) - 8)}{value[-4:]}"
