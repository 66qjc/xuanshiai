"""Phase 4 P4-01 —— projection_status service 单元测试。

覆盖:
- mark_pending 后 is_projection_active=False
- mark_active 后 is_projection_active=True + kind 维度
- mark_invalidated 后该 kind 不再 active(其他 kind 仍可 active)
- mark_deleted 后该 kind 永远不 active
- mark_failed 后 is_projection_active=False
- 同一 kind 多次 mark_active → 单行 UNIQUE 行为(fake upsert 覆盖)
- 跨 kind 独立:personal_searchable active 不影响 ideal_partner_preference
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.ai.projection_status import (
    ALL_KINDS,
    KIND_FOR_SUBJECT,
    KIND_IDEAL_PARTNER_PREFERENCE,
    KIND_PERSONAL_COMPATIBILITY,
    KIND_PERSONAL_SEARCHABLE,
    ProjectionStatusRepository,
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_FAILED,
    STATUS_PENDING,
    filter_active_kinds,
    is_projection_active,
    mark_active,
    mark_deleted,
    mark_failed,
    mark_invalidated,
    mark_pending,
)


def _await(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRepo:
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


def test_kinds_are_known() -> None:
    assert KIND_PERSONAL_SEARCHABLE in ALL_KINDS
    assert KIND_PERSONAL_COMPATIBILITY in ALL_KINDS
    assert KIND_IDEAL_PARTNER_PREFERENCE in ALL_KINDS
    assert len(ALL_KINDS) == 3


def test_kind_for_subject_mapping() -> None:
    personal_kinds = KIND_FOR_SUBJECT["personal"]
    ideal_kinds = KIND_FOR_SUBJECT["ideal_partner"]
    assert KIND_PERSONAL_SEARCHABLE in personal_kinds
    assert KIND_PERSONAL_COMPATIBILITY in personal_kinds
    assert KIND_IDEAL_PARTNER_PREFERENCE in ideal_kinds
    # 不重叠
    assert set(personal_kinds).isdisjoint(set(ideal_kinds))


def test_pending_then_active() -> None:
    repo = _FakeRepo()
    _await(mark_pending(user_id=100, kind=KIND_PERSONAL_SEARCHABLE, repo=repo))
    assert _await(
        is_projection_active(user_id=100, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)
    ) is False
    _await(
        mark_active(
            user_id=100,
            kind=KIND_PERSONAL_SEARCHABLE,
            source_revision=42,
            projection_id=999,
            repo=repo,
        )
    )
    assert _await(
        is_projection_active(user_id=100, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)
    ) is True
    row = _await(repo.get(100, KIND_PERSONAL_SEARCHABLE))
    assert row is not None
    assert row["source_revision"] == 42
    assert row["projection_id"] == 999


def test_invalidated_disables_only_target_kind() -> None:
    repo = _FakeRepo()
    _await(mark_active(user_id=200, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=10, repo=repo))
    _await(mark_active(user_id=200, kind=KIND_PERSONAL_COMPATIBILITY, source_revision=1, projection_id=11, repo=repo))
    _await(mark_invalidated(user_id=200, kind=KIND_PERSONAL_SEARCHABLE, repo=repo))
    assert _await(
        is_projection_active(user_id=200, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)
    ) is False
    assert _await(
        is_projection_active(user_id=200, kind=KIND_PERSONAL_COMPATIBILITY, repo=repo)
    ) is True


def test_deleted_disables_only_target_kind() -> None:
    repo = _FakeRepo()
    _await(mark_active(user_id=300, kind=KIND_IDEAL_PARTNER_PREFERENCE, source_revision=7, projection_id=77, repo=repo))
    _await(mark_deleted(user_id=300, kind=KIND_IDEAL_PARTNER_PREFERENCE, repo=repo, reason="consent_revoked"))
    assert _await(
        is_projection_active(user_id=300, kind=KIND_IDEAL_PARTNER_PREFERENCE, repo=repo)
    ) is False
    # 数据保留:row 还在,status=deleted
    row = _await(repo.get(300, KIND_IDEAL_PARTNER_PREFERENCE))
    assert row is not None
    assert row["status"] == STATUS_DELETED
    assert row["last_error"] == "consent_revoked"


def test_failed_disables_active() -> None:
    repo = _FakeRepo()
    _await(mark_active(user_id=400, kind=KIND_PERSONAL_SEARCHABLE, source_revision=2, projection_id=22, repo=repo))
    _await(mark_failed(user_id=400, kind=KIND_PERSONAL_SEARCHABLE, repo=repo, error="allowlist empty"))
    assert _await(
        is_projection_active(user_id=400, kind=KIND_PERSONAL_SEARCHABLE, repo=repo)
    ) is False
    row = _await(repo.get(400, KIND_PERSONAL_SEARCHABLE))
    assert row is not None
    assert row["status"] == STATUS_FAILED
    assert row["last_error"] == "allowlist empty"


def test_upsert_overwrites_same_kind() -> None:
    """同 kind 多次 mark_active:fake upsert 单行覆盖,新 source/projection_id 生效。"""
    repo = _FakeRepo()
    _await(mark_active(user_id=500, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=10, repo=repo))
    _await(mark_active(user_id=500, kind=KIND_PERSONAL_SEARCHABLE, source_revision=2, projection_id=20, repo=repo))
    rows = _await(repo.get_active_for_user(500))
    assert len(rows) == 1
    assert rows[0]["source_revision"] == 2
    assert rows[0]["projection_id"] == 20


def test_filter_active_kinds_returns_only_active() -> None:
    repo = _FakeRepo()
    _await(mark_active(user_id=600, kind=KIND_PERSONAL_SEARCHABLE, source_revision=1, projection_id=10, repo=repo))
    _await(mark_active(user_id=600, kind=KIND_PERSONAL_COMPATIBILITY, source_revision=1, projection_id=11, repo=repo))
    # ideal_partner_preference 不设 → 永远不在 active 列表
    out = _await(
        filter_active_kinds(
            user_id=600,
            kinds=[KIND_PERSONAL_SEARCHABLE, KIND_PERSONAL_COMPATIBILITY, KIND_IDEAL_PARTNER_PREFERENCE],
            repo=repo,
        )
    )
    assert set(out) == {KIND_PERSONAL_SEARCHABLE, KIND_PERSONAL_COMPATIBILITY}


def test_unknown_user_returns_empty_active() -> None:
    repo = _FakeRepo()
    rows = _await(repo.get_active_for_user(999))
    assert rows == []


def test_repository_protocol_satisfied_by_fake() -> None:
    repo: ProjectionStatusRepository = _FakeRepo()  # type: ignore[assignment]
    for name in ("get", "upsert", "get_active_for_user"):
        assert hasattr(repo, name), name


def test_unknown_kind_passes_through_fake() -> None:
    """Fake repo 不做 kind 校验(单元测试 fake 不复刻 SQL ENUM);
    真实 SqlProjectionStatusRepository 在 upsert 入口会抛 ValueError。
    本测试固定 fake 行为,避免 fake 走 SQL 路径。"""
    repo = _FakeRepo()
    # 不抛
    _await(mark_pending(user_id=700, kind="bogus_kind", repo=repo))  # type: ignore[arg-type]
    row = _await(repo.get(700, "bogus_kind"))  # type: ignore[arg-type]
    assert row is not None
    assert row["status"] == STATUS_PENDING


def test_subject_to_kinds_projection_consistency() -> None:
    """每个 subject 重建时只会更新映射表里的 kind,不会越界影响其他 kind。"""
    repo = _FakeRepo()
    # 模拟 personal 主体发布:仅 personal_searchable + personal_compatibility active
    for kind in KIND_FOR_SUBJECT["personal"]:
        _await(
            mark_active(
                user_id=800,
                kind=kind,
                source_revision=1,
                projection_id=hash(kind) & 0xFFFF,
                repo=repo,
            )
        )
    # 检查 ideal_partner_preference 仍未 active
    assert _await(
        is_projection_active(
            user_id=800, kind=KIND_IDEAL_PARTNER_PREFERENCE, repo=repo
        )
    ) is False
