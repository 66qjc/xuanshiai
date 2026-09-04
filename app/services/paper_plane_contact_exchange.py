"""Bilateral consent state for paper-plane contact-exchange requests.

This module intentionally returns consent state only; it never reads or
returns phone numbers, WeChat IDs, or other raw contact values.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class ContactExchangeError(Exception):
    pass


class ContactExchangeConflict(ContactExchangeError):
    pass


class ContactExchangeForbidden(ContactExchangeError):
    pass


class ContactExchangeNotFound(ContactExchangeError):
    pass


class ContactExchangeStore(Protocol):
    async def create_request(self, conversation_id: int, requester_id: int, target_id: int, kind: str, idempotency_key: str) -> dict[str, object]: ...
    async def respond(self, exchange_id: int, actor_id: int, decision: str, idempotency_key: str) -> dict[str, object]: ...


class PaperPlaneContactExchangeService:
    def __init__(self, store: ContactExchangeStore) -> None:
        self.store = store

    async def request(self, conversation_id: int, requester_id: int, target_id: int, kind: str, idempotency_key: str) -> dict[str, object]:
        if conversation_id < 1 or requester_id < 1 or target_id < 0 or (target_id > 0 and requester_id == target_id):
            raise ContactExchangeForbidden("无效的纸飞机会话参与者")
        if kind not in {"wechat", "phone"}:
            raise ContactExchangeConflict("联系方式类型仅支持 wechat 或 phone")
        if not 1 <= len(idempotency_key) <= 128:
            raise ContactExchangeConflict("无效的 Idempotency-Key")
        return await self.store.create_request(conversation_id, requester_id, target_id, kind, idempotency_key)

    async def respond(self, actor_id: int, exchange_id: int, decision: str, idempotency_key: str) -> dict[str, object]:
        if actor_id < 1 or exchange_id < 1:
            raise ContactExchangeForbidden("无效的联系方式交换申请")
        if decision not in {"accept", "reject", "withdraw"}:
            raise ContactExchangeConflict("不支持的交换申请操作")
        if not 1 <= len(idempotency_key) <= 128:
            raise ContactExchangeConflict("无效的 Idempotency-Key")
        return await self.store.respond(exchange_id, actor_id, decision, idempotency_key)


ContactExchangeService = PaperPlaneContactExchangeService


def _response(row: dict[str, Any]) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "conversation_id": int(row["conversation_id"]),
        "kind": str(row["kind"]),
        "requester_user_id": int(row["requester_user_id"]),
        "target_user_id": int(row["target_user_id"]),
        "status": str(row["status"]),
        "requester_consented_at": row.get("requester_consented_at"),
        "target_consented_at": row.get("target_consented_at"),
        "responded_at": row.get("responded_at"),
        "created_at": row["created_at"],
    }


class SqlAlchemyPaperPlaneContactExchangeStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _conversation_peer(self, conversation_id: int, requester_id: int) -> int:
        result = await self.db.execute(text("SELECT owner_id, replier_id, status FROM paper_plane_conversation WHERE id = :id FOR UPDATE"), {"id": conversation_id})
        row = result.mappings().first()
        if not row:
            raise ContactExchangeNotFound("纸飞机会话不存在")
        if int(row["status"] or 0) != 1:
            raise ContactExchangeConflict("纸飞机会话已结束")
        if requester_id == int(row["owner_id"]):
            return int(row["replier_id"])
        if requester_id == int(row["replier_id"]):
            return int(row["owner_id"])
        raise ContactExchangeForbidden("当前用户不属于该纸飞机会话")

    async def _ensure_pair_allowed(self, requester_id: int, target_id: int) -> None:
        blocked = await self.db.execute(text("SELECT 1 FROM user_block WHERE (user_id = :left_id AND target_user_id = :right_id) OR (user_id = :right_id AND target_user_id = :left_id) LIMIT 1"), {"left_id": requester_id, "right_id": target_id})
        if blocked.scalar():
            raise ContactExchangeForbidden("双方当前不能交换联系方式")
        restricted = await self.db.execute(text("SELECT 1 FROM user_restriction WHERE user_id IN (:left_id, :right_id) AND status = 1 AND starts_at <= UTC_TIMESTAMP() AND (ends_at IS NULL OR ends_at > UTC_TIMESTAMP()) AND restriction_type IN ('TOTAL_BAN', 'MESSAGE_RESTRICTED') LIMIT 1"), {"left_id": requester_id, "right_id": target_id})
        if restricted.scalar():
            raise ContactExchangeForbidden("双方当前不能交换联系方式")

    async def create_request(self, conversation_id: int, requester_id: int, target_id: int, kind: str, idempotency_key: str) -> dict[str, object]:
        async with self.db.begin():
            actual_target = await self._conversation_peer(conversation_id, requester_id)
            if target_id <= 0:
                target_id = actual_target
            if actual_target != target_id:
                raise ContactExchangeForbidden("交换对象必须是当前会话另一方")
            await self._ensure_pair_allowed(requester_id, target_id)
            unlocked = await self.db.execute(text("SELECT 1 FROM paper_plane_profile_unlock WHERE viewer_user_id = :viewer AND target_user_id = :target LIMIT 1"), {"viewer": requester_id, "target": target_id})
            if not unlocked.scalar():
                raise ContactExchangeForbidden("请先解锁对方资料后再发起交换申请")
            key_row = (await self.db.execute(text("SELECT * FROM paper_plane_contact_exchange WHERE requester_user_id = :requester AND idempotency_key = :key FOR UPDATE"), {"requester": requester_id, "key": idempotency_key})).mappings().first()
            if key_row:
                if int(key_row["target_user_id"]) != target_id or str(key_row["kind"]) != kind:
                    raise ContactExchangeConflict("幂等键已用于其他交换申请")
                return _response(dict(key_row))
            active = (await self.db.execute(text("SELECT * FROM paper_plane_contact_exchange WHERE conversation_id = :conversation AND requester_user_id = :requester AND kind = :kind AND status IN ('PENDING', 'APPROVED') ORDER BY id DESC LIMIT 1 FOR UPDATE"), {"conversation": conversation_id, "requester": requester_id, "kind": kind})).mappings().first()
            if active:
                return _response(dict(active))
            inserted = await self.db.execute(text("INSERT INTO paper_plane_contact_exchange (conversation_id, kind, requester_user_id, target_user_id, status, requester_consented_at, idempotency_key) VALUES (:conversation, :kind, :requester, :target, 'PENDING', UTC_TIMESTAMP(), :key)"), {"conversation": conversation_id, "kind": kind, "requester": requester_id, "target": target_id, "key": idempotency_key})
            row = (await self.db.execute(text("SELECT * FROM paper_plane_contact_exchange WHERE id = :id"), {"id": inserted.lastrowid})).mappings().one()
            return _response(dict(row))

    async def respond(self, exchange_id: int, actor_id: int, decision: str, idempotency_key: str) -> dict[str, object]:
        async with self.db.begin():
            row = (await self.db.execute(text("SELECT * FROM paper_plane_contact_exchange WHERE id = :id FOR UPDATE"), {"id": exchange_id})).mappings().first()
            if not row:
                raise ContactExchangeNotFound("交换申请不存在")
            payload = dict(row)
            requester_id, target_id = int(payload["requester_user_id"]), int(payload["target_user_id"])
            if decision == "withdraw":
                if actor_id != requester_id:
                    raise ContactExchangeForbidden("只有申请人可以撤回交换申请")
            elif actor_id != target_id:
                raise ContactExchangeForbidden("只有对方可以处理交换申请")
            await self._ensure_pair_allowed(requester_id, target_id)
            status = {"accept": "APPROVED", "reject": "REJECTED", "withdraw": "REVOKED"}[decision]
            if payload["status"] != "PENDING":
                if payload["status"] == status:
                    return _response(payload)
                raise ContactExchangeConflict("申请状态不允许该操作")
            await self.db.execute(text("UPDATE paper_plane_contact_exchange SET status = :status, target_consented_at = CASE WHEN :decision = 'accept' THEN UTC_TIMESTAMP() ELSE target_consented_at END, responded_at = UTC_TIMESTAMP(), response_idempotency_key = :key WHERE id = :id"), {"status": status, "decision": decision, "key": idempotency_key, "id": exchange_id})
            updated = (await self.db.execute(text("SELECT * FROM paper_plane_contact_exchange WHERE id = :id"), {"id": exchange_id})).mappings().one()
            return _response(dict(updated))
