"""Phase 4 P4-04 —— projection_status 集成测试。

覆盖:
- 发布画像 → projection_status 写 active(由 worker handler 触发,本测试 mock)
- 删除画像 → projection_status 全部写 deleted
- 同一 kind 多次 mark_active → 单行 UNIQUE 行为
- 撤回授权 → mark_deleted(数据保留但下游不读)
- 候选/未发布 draft 不进入 active(未发布就没有 projection_status 行)
"""

from __future__ import annotations

import asyncio
from typing import Any


from app.services.ai.projection_status import (
    KIND_IDEAL_PARTNER_PREFERENCE,
    KIND_PERSONAL_COMPATIBILITY,
    KIND_PERSONAL_SEARCHABLE,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_INVALIDATED,
    filter_active_kinds,
    is_projection_active,
    mark_active,
    mark_deleted,
    mark_invalidated,
    mark_pending,
)


def _await(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StoreRepo:
    """更接近 SQL 行为的 in-memory store:UNIQUE(user_id, kind) 单行覆盖。"""

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str], dict] = {}

    async def get(self, user_id: int, kind: str) -> dict[str, Any] | None:
        return self.rows.get((int(user_id), str(kind)))

    async def upsert(
        self,
        *,
        user_id: int,
        kind: str,
        status: str,
        source_revision: int | None,
        projection_id: int | None,
        last_error: str | None,
    ) -> None:
        self.rows[(int(user_id), str(kind))] = {
            "user_id": int(user_id),
            "kind": str(kind),
            "status": str(status),
            "source_revision": source_revision,
            "projection_id": projection_id,
            "last_error": last_error,
        }

    async def get_active_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return [
            row for (uid, _), row in self.rows.items()
            if uid == int(user_id) and row["status"] == STATUS_ACTIVE
        ]


def test_publish_lifecycle_marks_active() -> None:
    """模拟 profile_projection_handler 成功路径:同主体的两个 kind 都置 active。"""
    repo = _StoreRepo()
    user_id = 1234
    revision_id = 99
    # personal_searchable
    _await(mark_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, source_revision=revision_id, projection_id=1001, repo=repo))
    # personal_compatibility
    _await(mark_active(user_id=user_id, kind=KIND_PERSONAL_COMPATIBILITY, source_revision=revision_id, projection_id=1002, repo=repo))
    # 校验
    assert _await(is_projection_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)) is True
    assert _await(is_projection_active(user_id=user_id, kind=KIND_PERSONAL_COMPATIBILITY, repo=repo)) is True
    # ideal_partner_preference 尚未发布 → not active
    assert _await(is_projection_active(user_id=user_id, kind=KIND_IDEAL_PARTNER_PREFERENCE, repo=repo)) is False
    # get_active_for_user 应返回 2 行
    active = _await(repo.get_active_for_user(user_id))
    assert len(active) == 2


def test_republish_invalidates_old_then_activates_new() -> None:
    """二次发布:旧 active 行 invalidated,新行 active(同 kind 唯一)。"""
    repo = _StoreRepo()
    user_id = 1235
    # 第一次发布
    _await(mark_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=2001, repo=repo))
    # 第二次发布前:先 mark_invalidated(实际是 mark_active 自身会覆盖;这里手动模拟显式失效)
    _await(mark_invalidated(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo, reason="rebuild"))
    row = _await(repo.get(user_id, KIND_PERSONAL_SEARCHABLE))
    assert row is not None
    assert row["status"] == STATUS_INVALIDATED
    # 再 mark_active 一次
    _await(mark_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, source_revision=2, projection_id=2002, repo=repo))
    row2 = _await(repo.get(user_id, KIND_PERSONAL_SEARCHABLE))
    assert row2 is not None
    assert row2["status"] == STATUS_ACTIVE
    assert row2["source_revision"] == 2
    assert row2["projection_id"] == 2002
    # UNIQUE 行为:rows 字典中只应该有 1 行
    matching = [k for k in repo.rows.keys() if k[0] == user_id]
    assert len(matching) == 1


def test_delete_profile_marks_all_subject_kinds_deleted() -> None:
    """模拟 delete_ai_profile personal 主体:两个 kind 都 mark_deleted。"""
    repo = _StoreRepo()
    user_id = 1236
    # 先建立两个 personal_kinds 的 active
    _await(mark_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=3001, repo=repo))
    _await(mark_active(user_id=user_id, kind=KIND_PERSONAL_COMPATIBILITY, source_revision=1, projection_id=3002, repo=repo))
    # 删除
    _await(mark_deleted(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo, reason="ai_profile_deleted"))
    _await(mark_deleted(user_id=user_id, kind=KIND_PERSONAL_COMPATIBILITY, repo=repo, reason="ai_profile_deleted"))
    # 下游:filter_active_kinds 应返回空
    out = _await(
        filter_active_kinds(
            user_id=user_id,
            kinds=[KIND_PERSONAL_SEARCHABLE, KIND_PERSONAL_COMPATIBILITY, KIND_IDEAL_PARTNER_PREFERENCE],
            repo=repo,
        )
    )
    assert out == []
    # 数据保留
    assert _await(repo.get(user_id, KIND_PERSONAL_SEARCHABLE)) is not None
    assert _await(repo.get(user_id, KIND_PERSONAL_COMPATIBILITY)) is not None
    row = _await(repo.get(user_id, KIND_PERSONAL_SEARCHABLE))
    assert row is not None
    assert row["status"] == STATUS_DELETED


def test_revoke_consent_only_targets_kinds_with_active_projection() -> None:
    """撤回授权:不破坏其他用户的投影,只影响本人。"""
    repo = _StoreRepo()
    # 用户 A 有 personal_searchable active
    _await(mark_active(user_id=2000, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=4001, repo=repo))
    # 用户 B 也有
    _await(mark_active(user_id=2001, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=4002, repo=repo))
    # 撤回 A 的授权
    _await(mark_deleted(user_id=2000, kind=KIND_PERSONAL_SEARCHABLE, repo=repo, reason="consent_revoked"))
    # A 不可读,B 仍可读
    assert _await(is_projection_active(user_id=2000, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)) is False
    assert _await(is_projection_active(user_id=2001, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)) is True


def test_unpublished_drafts_have_no_projection_status() -> None:
    """未发布/未确认的草稿不进 ai_profile_projection_status(active 全无)。"""
    repo = _StoreRepo()
    # 模拟"只有候选,从未发布"的用户
    user_id = 3000
    # 这里不调任何 mark_* —— 假库 rows 应当为空
    out = _await(
        filter_active_kinds(
            user_id=user_id,
            kinds=[KIND_PERSONAL_SEARCHABLE, KIND_PERSONAL_COMPATIBILITY, KIND_IDEAL_PARTNER_PREFERENCE],
            repo=repo,
        )
    )
    assert out == []
    # active list 也空
    assert _await(repo.get_active_for_user(user_id)) == []


def test_pending_kind_does_not_count_as_active() -> None:
    """pending(worker 还在跑)不应当作 active。"""
    repo = _StoreRepo()
    user_id = 3001
    _await(mark_pending(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo))
    assert _await(is_projection_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)) is False
    out = _await(filter_active_kinds(user_id=user_id, kinds=[KIND_PERSONAL_SEARCHABLE], repo=repo))
    assert out == []


def test_idempotent_replay_does_not_duplicate() -> None:
    """同一 kind 多次 mark_active 不会产生多行。"""
    repo = _StoreRepo()
    user_id = 4000
    for i in range(5):
        _await(
            mark_active(
                user_id=user_id,
                kind=KIND_PERSONAL_SEARCHABLE,
                source_revision=i + 1,
                projection_id=5000 + i,
                repo=repo,
            )
        )
    matching = [k for k in repo.rows.keys() if k[0] == user_id]
    assert len(matching) == 1
    # 最新一次生效
    row = _await(repo.get(user_id, KIND_PERSONAL_SEARCHABLE))
    assert row is not None
    assert row["source_revision"] == 5
    assert row["projection_id"] == 5004


def test_subject_kinds_disjoint() -> None:
    """personal 与 ideal_partner 各自的 kind 集合不重叠。"""
    from app.services.ai.projection_status import KIND_FOR_SUBJECT
    personal = set(KIND_FOR_SUBJECT["personal"])
    ideal = set(KIND_FOR_SUBJECT["ideal_partner"])
    assert personal.isdisjoint(ideal)
    assert KIND_PERSONAL_SEARCHABLE in personal
    assert KIND_PERSONAL_COMPATIBILITY in personal
    assert KIND_IDEAL_PARTNER_PREFERENCE in ideal


def test_failed_status_does_not_block_retry() -> None:
    """failed → pending → active 链路必须可走。"""
    repo = _StoreRepo()
    user_id = 5000
    _await(mark_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=6000, repo=repo))
    _await(
        __import__(
            "app.services.ai.projection_status", fromlist=["mark_failed"]
        ).mark_failed(
            user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo, error="allowlist empty"
        )
    )
    assert _await(is_projection_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)) is False
    # 重新发布
    _await(mark_pending(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo))
    _await(
        mark_active(
            user_id=user_id,
            kind=KIND_PERSONAL_SEARCHABLE,
            source_revision=2,
            projection_id=6001,
            repo=repo,
        )
    )
    assert _await(is_projection_active(user_id=user_id, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)) is True
    row = _await(repo.get(user_id, KIND_PERSONAL_SEARCHABLE))
    assert row is not None
    assert row["source_revision"] == 2
    assert row["last_error"] is None  # 成功后清空 last_error
