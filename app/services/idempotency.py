"""Durable idempotency reservations for database-backed create operations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    record_id: int
    owner_token: str
    response: dict[str, Any] | None = None


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _response_json(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise HTTPException(409, detail="幂等记录响应不可用")
    return value


async def reserve_or_replay(
    db: AsyncSession,
    user_id: int,
    operation: str,
    idempotency_key: str,
    payload: Any,
) -> IdempotencyReservation:
    payload_hash = _payload_hash(payload)
    owner_token = str(uuid4())
    try:
        created = await db.execute(
            text(
                """INSERT INTO api_idempotency_record
                (user_id, operation, idempotency_key, payload_hash, state, owner_token,
                 created_at, updated_at)
                VALUES (:user_id, :operation, :idempotency_key, :payload_hash, 'reserved',
                        :owner_token, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))"""
            ),
            {
                "user_id": user_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
                "payload_hash": payload_hash,
                "owner_token": owner_token,
            },
        )
        await db.commit()
        return IdempotencyReservation(int(created.lastrowid), owner_token)
    except IntegrityError:
        await db.rollback()
        existing = await db.execute(
            text(
                """SELECT id, payload_hash, state, owner_token, response_json
                FROM api_idempotency_record
                WHERE user_id = :user_id AND operation = :operation
                  AND BINARY idempotency_key = BINARY :idempotency_key
                LIMIT 1"""
            ),
            {
                "user_id": user_id,
                "operation": operation,
                "idempotency_key": idempotency_key,
            },
        )
        row = existing.mappings().first()
        if not row:
            raise

    if row["payload_hash"] != payload_hash:
        raise HTTPException(409, detail="Idempotency-Key 已用于不同请求")
    if row["state"] == "completed":
        response = _response_json(row.get("response_json"))
        if response is None:
            raise HTTPException(409, detail="幂等记录响应不可用")
        return IdempotencyReservation(int(row["id"]), str(row["owner_token"]), response)

    takeover = await db.execute(
        text(
            """UPDATE api_idempotency_record
            SET owner_token = :owner_token, updated_at = UTC_TIMESTAMP(6)
            WHERE id = :record_id AND state = 'reserved'
              AND updated_at < DATE_SUB(UTC_TIMESTAMP(6), INTERVAL 5 MINUTE)"""
        ),
        {"record_id": row["id"], "owner_token": owner_token},
    )
    if takeover.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, detail="相同请求正在处理中")
    await db.commit()
    return IdempotencyReservation(int(row["id"]), owner_token)


async def complete(
    db: AsyncSession,
    reservation: IdempotencyReservation,
    response: dict[str, Any],
) -> None:
    result = await db.execute(
        text(
            """UPDATE api_idempotency_record
            SET state = 'completed', response_json = :response_json,
                updated_at = UTC_TIMESTAMP(6)
            WHERE id = :record_id AND state = 'reserved' AND owner_token = :owner_token"""
        ),
        {
            "record_id": reservation.record_id,
            "owner_token": reservation.owner_token,
            "response_json": json.dumps(response, ensure_ascii=False, separators=(",", ":")),
        },
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, detail="幂等请求所有权已失效")
    await db.commit()


async def abort(db: AsyncSession, reservation: IdempotencyReservation) -> None:
    await db.execute(
        text(
            """DELETE FROM api_idempotency_record
            WHERE id = :record_id AND state = 'reserved' AND owner_token = :owner_token"""
        ),
        {"record_id": reservation.record_id, "owner_token": reservation.owner_token},
    )
    await db.commit()
