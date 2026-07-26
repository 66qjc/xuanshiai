"""Sensitive-word MVP: substring match against config_sensitive_word, reject on hit."""

from __future__ import annotations

import time
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CACHE_TTL_SEC = 60.0
_cached_words: list[str] = []
_cached_at: float = 0.0


def clear_sensitive_word_cache() -> None:
    """Test helper / admin hook after word-bank changes."""
    global _cached_words, _cached_at
    _cached_words = []
    _cached_at = 0.0


async def load_active_sensitive_words(db: AsyncSession, *, force: bool = False) -> list[str]:
    global _cached_words, _cached_at
    now = time.monotonic()
    if not force and _cached_words and (now - _cached_at) < _CACHE_TTL_SEC:
        return list(_cached_words)
    result = await db.execute(
        text("SELECT word FROM config_sensitive_word WHERE is_active = 1 AND word IS NOT NULL AND word <> ''")
    )
    rows = result.mappings().all()
    words: list[str] = []
    for row in rows:
        value = row.get("word") if hasattr(row, "get") else row["word"]
        if value is None:
            continue
        token = str(value).strip()
        if token:
            words.append(token)
    _cached_words = words
    _cached_at = now
    return list(words)


def find_sensitive_hit(content: str | None, words: Iterable[str]) -> str | None:
    text_value = (content or "").strip()
    if not text_value:
        return None
    lowered = text_value.casefold()
    for word in words:
        token = (word or "").strip()
        if not token:
            continue
        if token.casefold() in lowered:
            return token
    return None


async def assert_text_allowed(db: AsyncSession, content: str | None, *, field: str = "内容") -> None:
    """Raise 422 when content contains an active sensitive word."""
    words = await load_active_sensitive_words(db)
    hit = find_sensitive_hit(content, words)
    if hit is not None:
        raise HTTPException(422, detail=f"{field}含违规词，请修改后重试")
