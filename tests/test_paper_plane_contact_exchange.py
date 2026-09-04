"""Red contract for the paper-plane bilateral contact exchange state machine."""

import pytest

from app.api.routes.paper_plane_contact_exchange import create_contact_exchange
from app.services.paper_plane_contact_exchange import (
    ContactExchangeConflict,
    ContactExchangeForbidden,
    ContactExchangeService,
)


class MemoryExchangeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[int, str, int], dict[str, object]] = {}
        self.keys: dict[tuple[int, str], int] = {}
        self.next_id = 1

    async def create_request(
        self, conversation_id: int, requester_id: int, target_id: int, kind: str, idempotency_key: str
    ) -> dict[str, object]:
        replay = self.keys.get((requester_id, idempotency_key))
        if replay is not None:
            row = next(row for row in self.rows.values() if row['id'] == replay)
            if row['target_user_id'] != target_id or row['kind'] != kind:
                raise ContactExchangeConflict('幂等键已用于其他交换申请')
            return row
        key = (conversation_id, kind, requester_id)
        row = self.rows.get(key)
        if row is not None and row['status'] == 'PENDING':
            raise ContactExchangeConflict('已有待处理申请')
        if row is None:
            row = {
                'id': self.next_id, 'conversation_id': conversation_id, 'kind': kind,
                'requester_user_id': requester_id, 'target_user_id': target_id,
                'status': 'PENDING', 'responded_at': None,
            }
            self.next_id += 1
            self.rows[key] = row
        else:
            row.update({'target_user_id': target_id, 'status': 'PENDING', 'responded_at': None})
        self.keys[(requester_id, idempotency_key)] = int(row['id'])
        return row

    async def respond(
        self, exchange_id: int, actor_id: int, decision: str, idempotency_key: str
    ) -> dict[str, object]:
        row = next((item for item in self.rows.values() if item['id'] == exchange_id), None)
        if row is None or row['target_user_id'] != actor_id:
            raise ContactExchangeForbidden('无权处理该交换申请')
        if row['status'] != 'PENDING':
            if row['status'] == ('APPROVED' if decision == 'accept' else 'REJECTED'):
                return row
            raise ContactExchangeConflict('申请状态不允许该操作')
        row['status'] = 'APPROVED' if decision == 'accept' else 'REJECTED'
        row['responded_at'] = 'now'
        return row


def service(store: MemoryExchangeStore) -> ContactExchangeService:
    return ContactExchangeService(store=store)


@pytest.mark.asyncio
async def test_exchange_requires_bilateral_consent_and_never_returns_contact_values() -> None:
    store = MemoryExchangeStore()
    svc = service(store)
    pending = await svc.request(9, 100, 200, 'wechat', 'request-wechat')
    assert pending['status'] == 'PENDING'
    assert pending.get('wechat') is None and pending.get('phone') is None
    approved = await svc.respond(200, int(pending['id']), 'accept', 'respond-1')
    assert approved['status'] == 'APPROVED'
    assert approved.get('wechat') is None and approved.get('phone') is None


@pytest.mark.asyncio
async def test_exchange_is_scoped_to_conversation_and_idempotent() -> None:
    store = MemoryExchangeStore()
    svc = service(store)
    first = await svc.request(9, 100, 200, 'phone', 'same-key')
    replay = await svc.request(9, 100, 200, 'phone', 'same-key')
    assert replay == first
    with pytest.raises(ContactExchangeConflict):
        await svc.request(9, 100, 200, 'wechat', 'same-key')
    with pytest.raises(ContactExchangeForbidden):
        await svc.respond(300, int(first['id']), 'accept', 'respond-2')


def test_route_module_is_present() -> None:
    assert create_contact_exchange is not None
