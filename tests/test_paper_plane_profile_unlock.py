"""Red contract for server-authoritative paper-plane profile unlocking."""

import pytest

# These deliberately fail now as missing production modules, not a fixture error.
from app.api.routes.paper_plane_profile_unlock import unlock_paper_plane_profile
from app.services.paper_plane_profile_unlock import (
    PaperPlaneProfileUnlockService,
    ProfileUnlockConflict,
    ProfileUnlockForbidden,
    ProfileUnlockInsufficientPoints,
)


class MemoryUnlockStore:
    def __init__(self, points: dict[int, int], visible_targets: set[int]) -> None:
        self.points = points
        self.visible_targets = visible_targets
        self.unlocks: set[tuple[int, int]] = set()
        self.idempotency: dict[tuple[int, str], tuple[int, dict[str, object]]] = {}
        self.debit_calls = 0

    async def unlock_atomically(self, viewer_id: int, target_id: int, idempotency_key: str, points_cost: int) -> dict[str, object]:
        key = (viewer_id, idempotency_key)
        replay = self.idempotency.get(key)
        if replay is not None:
            if replay[0] != target_id:
                raise ProfileUnlockConflict('idempotency key belongs to another target')
            return replay[1]
        if viewer_id == target_id or target_id not in self.visible_targets:
            raise ProfileUnlockForbidden('target is not unlockable')
        if self.points.get(viewer_id, 0) < points_cost:
            raise ProfileUnlockInsufficientPoints('insufficient points')
        already_unlocked = (viewer_id, target_id) in self.unlocks
        if not already_unlocked:
            self.points[viewer_id] -= points_cost
            self.debit_calls += 1
            self.unlocks.add((viewer_id, target_id))
        result = {'viewer_id': viewer_id, 'target_id': target_id, 'unlocked': True, 'points_debited': 0 if already_unlocked else points_cost, 'profile': {'display_name': '匿名用户', 'phone': None, 'wechat': None}}
        self.idempotency[key] = (target_id, result)
        return result


def service(store: MemoryUnlockStore) -> PaperPlaneProfileUnlockService:
    return PaperPlaneProfileUnlockService(store=store, points_cost=80)


@pytest.mark.asyncio
async def test_unlock_is_scoped_to_viewer_and_target_and_never_returns_raw_contact() -> None:
    store = MemoryUnlockStore(points={7: 160}, visible_targets={11, 12})
    first = await service(store).unlock(viewer_id=7, target_id=11, idempotency_key='unlock-11')
    second = await service(store).unlock(viewer_id=7, target_id=12, idempotency_key='unlock-12')
    assert (7, 11) in store.unlocks and (7, 12) in store.unlocks
    assert first['target_id'] == 11 and second['target_id'] == 12
    assert first['profile']['phone'] is None and first['profile']['wechat'] is None
    assert store.debit_calls == 2


@pytest.mark.asyncio
async def test_same_idempotency_key_replays_one_target_without_a_second_debit() -> None:
    store = MemoryUnlockStore(points={7: 80}, visible_targets={11})
    first = await service(store).unlock(viewer_id=7, target_id=11, idempotency_key='same-key')
    replay = await service(store).unlock(viewer_id=7, target_id=11, idempotency_key='same-key')
    assert replay == first
    assert store.points[7] == 0 and store.debit_calls == 1


@pytest.mark.asyncio
async def test_unlock_rejects_insufficient_points_self_and_invisible_target() -> None:
    with pytest.raises(ProfileUnlockInsufficientPoints):
        await service(MemoryUnlockStore(points={7: 79}, visible_targets={11})).unlock(viewer_id=7, target_id=11, idempotency_key='low')
    with pytest.raises(ProfileUnlockForbidden):
        await service(MemoryUnlockStore(points={7: 80}, visible_targets={7})).unlock(viewer_id=7, target_id=7, idempotency_key='self')
    with pytest.raises(ProfileUnlockForbidden):
        await service(MemoryUnlockStore(points={7: 80}, visible_targets=set())).unlock(viewer_id=7, target_id=11, idempotency_key='invisible')


@pytest.mark.asyncio
async def test_reusing_a_key_for_a_different_target_is_a_conflict() -> None:
    store = MemoryUnlockStore(points={7: 160}, visible_targets={11, 12})
    unlocker = service(store)
    await unlocker.unlock(viewer_id=7, target_id=11, idempotency_key='reused-key')
    with pytest.raises(ProfileUnlockConflict):
        await unlocker.unlock(viewer_id=7, target_id=12, idempotency_key='reused-key')
    assert store.debit_calls == 1
    assert unlock_paper_plane_profile is not None
