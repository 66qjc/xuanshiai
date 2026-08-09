"""Stable, exact candidate query primitives shared by discovery-style reads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


SORT_VERSION = "feed-rank-baseline-v1"
_MAX_CURSOR_LENGTH = 512


class InvalidCandidateCursor(ValueError):
    """Raised when an opaque candidate cursor is malformed or does not match."""


@dataclass(frozen=True, slots=True)
class CandidateRankRow:
    user_id: int
    is_boosted: bool
    last_active_at: datetime | None


@dataclass(frozen=True, slots=True)
class CandidateCursor:
    sort_version: str
    query_fingerprint: str
    is_boosted: bool
    last_active_at: datetime | None
    user_id: int


@dataclass(frozen=True, slots=True)
class CandidateQuerySnapshot:
    """Server-owned SQL fragments and query facts for one candidate result set."""

    select_sql: str
    count_sql: str
    where_sql: str
    params: Mapping[str, Any]
    query_fingerprint: str
    page: int
    sort_version: str = SORT_VERSION

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("candidate page must be positive")
        if not self.query_fingerprint:
            raise ValueError("candidate query fingerprint is required")
        if self.sort_version != SORT_VERSION:
            raise ValueError("unsupported candidate sort version")


@dataclass(frozen=True, slots=True)
class CandidatePage:
    items: list[dict[str, Any]]
    page: int
    page_size: int
    total: int
    has_more: bool
    next_cursor: str | None
    sort_version: str = SORT_VERSION
    total_is_estimate: bool = False


def _sortable_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def sort_candidate_rows(rows: list[CandidateRankRow]) -> list[CandidateRankRow]:
    """Sort by the database feed order, including a deterministic user-id tie-break."""
    return sorted(
        rows,
        key=lambda row: (row.is_boosted, _sortable_timestamp(row.last_active_at), row.user_id),
        reverse=True,
    )


def build_query_fingerprint(query_facts: Mapping[str, Any]) -> str:
    """Create a stable, non-secret fingerprint used to bind cursors to one query."""
    canonical = json.dumps(
        query_facts,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise InvalidCandidateCursor("invalid candidate cursor") from exc


def _cursor_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidCandidateCursor("invalid candidate cursor")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidCandidateCursor("invalid candidate cursor") from exc


class CandidateQueryService:
    """Execute exact, SQL-ordered candidate pages without client-owned SQL."""

    def __init__(self, *, secret_key: str) -> None:
        if not secret_key:
            raise ValueError("candidate cursor secret key is required")
        self._secret_key = secret_key.encode("utf-8")

    def encode_cursor(self, cursor: CandidateCursor) -> str:
        if cursor.sort_version != SORT_VERSION:
            raise InvalidCandidateCursor("unsupported candidate cursor sort version")
        payload = {
            "sort_version": cursor.sort_version,
            "query_fingerprint": cursor.query_fingerprint,
            "is_boosted": cursor.is_boosted,
            "last_active_at": (
                cursor.last_active_at.isoformat() if cursor.last_active_at is not None else None
            ),
            "user_id": cursor.user_id,
        }
        encoded_payload = _encode_bytes(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(
            self._secret_key,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_encode_bytes(signature)}"

    def decode_cursor(
        self,
        token: str,
        *,
        expected_fingerprint: str | None = None,
    ) -> CandidateCursor:
        if not token or len(token) > _MAX_CURSOR_LENGTH:
            raise InvalidCandidateCursor("invalid candidate cursor")
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
        except ValueError as exc:
            raise InvalidCandidateCursor("invalid candidate cursor") from exc
        try:
            signed_payload = encoded_payload.encode("ascii")
        except UnicodeEncodeError as exc:
            raise InvalidCandidateCursor("invalid candidate cursor") from exc
        expected_signature = hmac.new(
            self._secret_key,
            signed_payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_signature, _decode_bytes(encoded_signature)):
            raise InvalidCandidateCursor("invalid candidate cursor")
        try:
            payload = json.loads(_decode_bytes(encoded_payload))
            cursor = CandidateCursor(
                sort_version=payload["sort_version"],
                query_fingerprint=payload["query_fingerprint"],
                is_boosted=payload["is_boosted"],
                last_active_at=_cursor_timestamp(payload["last_active_at"]),
                user_id=payload["user_id"],
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise InvalidCandidateCursor("invalid candidate cursor") from exc
        if (
            cursor.sort_version != SORT_VERSION
            or not isinstance(cursor.query_fingerprint, str)
            or not cursor.query_fingerprint
            or not isinstance(cursor.is_boosted, bool)
            or isinstance(cursor.user_id, bool)
            or not isinstance(cursor.user_id, int)
            or cursor.user_id < 1
        ):
            raise InvalidCandidateCursor("invalid candidate cursor")
        if (
            expected_fingerprint is not None
            and cursor.query_fingerprint != expected_fingerprint
        ):
            raise InvalidCandidateCursor("candidate cursor does not match this query")
        return cursor

    async def count(self, db: AsyncSession, snapshot: CandidateQuerySnapshot) -> int:
        result = await db.execute(
            text(f"{snapshot.count_sql} WHERE {snapshot.where_sql}"),
            dict(snapshot.params),
        )
        return int(result.scalar_one())

    async def fetch_page(
        self,
        db: AsyncSession,
        snapshot: CandidateQuerySnapshot,
        *,
        cursor: str | None,
        page_size: int,
    ) -> CandidatePage:
        if page_size < 1:
            raise ValueError("candidate page size must be positive")
        decoded_cursor = (
            self.decode_cursor(cursor, expected_fingerprint=snapshot.query_fingerprint)
            if cursor
            else None
        )
        total = await self.count(db, snapshot)
        params = dict(snapshot.params)
        sql = f"SELECT * FROM ({snapshot.select_sql} WHERE {snapshot.where_sql}) candidate_rows"
        if decoded_cursor is not None:
            sql += f" WHERE {self._cursor_clause(decoded_cursor, params)}"
            params["candidate_query_limit"] = page_size + 1
            sql += " ORDER BY candidate_rows.is_boosted DESC, candidate_rows.last_active_at DESC, candidate_rows.user_id DESC LIMIT :candidate_query_limit"
        else:
            params["candidate_query_limit"] = page_size
            params["candidate_query_offset"] = (snapshot.page - 1) * page_size
            sql += " ORDER BY candidate_rows.is_boosted DESC, candidate_rows.last_active_at DESC, candidate_rows.user_id DESC LIMIT :candidate_query_limit OFFSET :candidate_query_offset"
        result = await db.execute(text(sql), params)
        rows = [dict(row) for row in result.mappings().all()]
        if decoded_cursor is not None:
            has_more = len(rows) > page_size
            rows = rows[:page_size]
        else:
            has_more = snapshot.page * page_size < total
        next_cursor = self._next_cursor(rows, snapshot, has_more)
        return CandidatePage(
            items=rows,
            page=snapshot.page,
            page_size=page_size,
            total=total,
            has_more=has_more,
            next_cursor=next_cursor,
            sort_version=snapshot.sort_version,
        )

    def _cursor_clause(
        self,
        cursor: CandidateCursor,
        params: dict[str, Any],
    ) -> str:
        params["cursor_is_boosted"] = int(cursor.is_boosted)
        params["cursor_user_id"] = cursor.user_id
        if cursor.last_active_at is None:
            return (
                "(candidate_rows.is_boosted < :cursor_is_boosted "
                "OR (candidate_rows.is_boosted = :cursor_is_boosted "
                "AND candidate_rows.last_active_at IS NULL "
                "AND candidate_rows.user_id < :cursor_user_id))"
            )
        params["cursor_last_active_at"] = cursor.last_active_at
        return (
            "(candidate_rows.is_boosted < :cursor_is_boosted "
            "OR (candidate_rows.is_boosted = :cursor_is_boosted AND ("
            "candidate_rows.last_active_at IS NULL "
            "OR candidate_rows.last_active_at < :cursor_last_active_at "
            "OR (candidate_rows.last_active_at = :cursor_last_active_at "
            "AND candidate_rows.user_id < :cursor_user_id))))"
        )

    def _next_cursor(
        self,
        rows: list[dict[str, Any]],
        snapshot: CandidateQuerySnapshot,
        has_more: bool,
    ) -> str | None:
        if not rows or not has_more:
            return None
        last_row = rows[-1]
        last_active_at = last_row.get("last_active_at")
        if isinstance(last_active_at, str):
            last_active_at = datetime.fromisoformat(last_active_at)
        if last_active_at is not None and not isinstance(last_active_at, datetime):
            raise ValueError("candidate query returned an invalid activity timestamp")
        cursor = CandidateCursor(
            sort_version=snapshot.sort_version,
            query_fingerprint=snapshot.query_fingerprint,
            is_boosted=bool(last_row.get("is_boosted")),
            last_active_at=last_active_at,
            user_id=int(last_row["user_id"]),
        )
        return self.encode_cursor(cursor)
