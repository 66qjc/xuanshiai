"""Alibaba Cloud Marketplace text-moderation adapter."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderDecision:
    """Normalized moderation result returned by an external provider."""

    action: str
    matched_words: tuple[str, ...] = ()
    risk_level: int = 0
    provider: str = "aliyun_market"


class ProviderProtocolError(ValueError):
    """Raised when the configured provider response cannot be classified."""


def _failure_decision() -> ProviderDecision:
    if settings.aliyun_content_moderation_fail_mode == "reject":
        return ProviderDecision(action="reject", risk_level=3)
    return ProviderDecision(action="manual_review", risk_level=2)


def _as_words(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _extract_matched_words(payload: dict[str, Any]) -> tuple[str, ...]:
    data = payload.get("data")
    candidates = [
        payload.get("matched_words"),
        payload.get("matchedWords"),
        payload.get("words"),
        payload.get("sensitiveWords"),
        data.get("matched_words") if isinstance(data, dict) else None,
        data.get("matchedWords") if isinstance(data, dict) else None,
        data.get("words") if isinstance(data, dict) else None,
        data.get("sensitiveWords") if isinstance(data, dict) else None,
    ]
    for candidate in candidates:
        words = _as_words(candidate)
        if words:
            return words
    return ()


def _classify_payload(payload: Any) -> bool:
    """Return whether the provider explicitly reports a hit.

    The cloud-market product's exact response schema is supplied after
    purchase. Unknown responses fail closed to the configured fallback rather
    than being treated as clean content.
    """
    if not isinstance(payload, dict):
        raise ProviderProtocolError("阿里云敏感词服务返回非 JSON 对象")

    candidates: list[Any] = [payload]
    if isinstance(payload.get("data"), dict):
        candidates.append(payload["data"])

    for item in candidates:
        for key in ("blocked", "hit", "isSensitive", "is_sensitive"):
            if key in item and isinstance(item[key], bool):
                return item[key]

        for key in ("status", "result", "riskLevel", "risk_level"):
            value = item.get(key)
            if isinstance(value, str):
                normalized = value.casefold()
                if normalized in {"blocked", "sensitive", "illegal", "reject", "hit"}:
                    return True
                if normalized in {"allow", "allowed", "clean", "pass", "ok", "normal"}:
                    return False

    raise ProviderProtocolError("无法识别阿里云敏感词服务返回结果")


async def moderate_with_aliyun(content: str) -> ProviderDecision:
    """Moderate text through the configured Marketplace API."""
    if not settings.aliyun_content_moderation_enabled or not content.strip():
        return ProviderDecision(action="allow")

    app_code = settings.aliyun_content_moderation_app_code
    path = settings.aliyun_content_moderation_path.strip()
    if not app_code or not path or path == "/YOUR_API_PATH":
        logger.error("阿里云敏感词服务已启用但接口配置不完整")
        return _failure_decision()

    url = (
        settings.aliyun_content_moderation_base_url.rstrip("/")
        + "/"
        + path.lstrip("/")
    )
    headers = {
        "Authorization": "APPCODE " + app_code.get_secret_value(),
        "Accept": "application/json",
    }
    payload = {settings.aliyun_content_moderation_text_field: content}

    try:
        async with httpx.AsyncClient(
            timeout=settings.aliyun_content_moderation_timeout_seconds
        ) as client:
            if settings.aliyun_content_moderation_request_mode == "form":
                response = await client.post(url, headers=headers, data=payload)
            else:
                response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            response_payload = response.json()
            hit = _classify_payload(response_payload)
    except (httpx.HTTPError, ValueError, ProviderProtocolError) as exc:
        logger.warning(
            "阿里云敏感词服务调用失败: error_type=%s",
            type(exc).__name__,
        )
        return _failure_decision()

    if not hit:
        return ProviderDecision(action="allow")

    return ProviderDecision(
        action=settings.aliyun_content_moderation_default_action,
        matched_words=_extract_matched_words(response_payload),
        risk_level=2,
    )
