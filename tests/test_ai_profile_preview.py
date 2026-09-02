"""Phase 3 P3-01 — preview service 单元测试。

覆盖:
- generate_preview 创建新 active 行(返回 status=active,preview_id 唯一)
- 同一 (draft_id, expected_revision) 重复调用复用既有 active 行
- expected_revision 不匹配 draft → 抛 DRAFT_VERSION_CONFLICT
- draft 不属于本人 → 抛 DRAFT_NOT_FOUND
- get_preview 越权/不存在 → None
- mark_preview_stale_for_draft 把除 except_preview_id 外的 active 行改 stale
- confirm_publish_with_preview:状态非 active、draft_id 不匹配、revision 不匹配都抛 conflict
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


def _await(coro: Any) -> Any:
    """同步驱动 async 函数;Phase 3 单元测试不引入 pytest-asyncio。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRepo:
    def __init__(self):
        self.drafts: dict[tuple[str, int], dict] = {}
        self.previews: dict[str, dict] = {}
        self.preview_by_draft_rev: dict[tuple[str, int], str] = {}

    async def find_draft_for_owner(self, draft_id, user_id):
        return self.drafts.get((draft_id, user_id))

    async def find_active_preview(self, draft_id, expected_revision):
        pid = self.preview_by_draft_rev.get((draft_id, expected_revision))
        if pid is None:
            return None
        row = self.previews.get(pid)
        if row and row.get("status") == "active":
            return row
        return None

    async def find_preview_by_id(self, preview_id, user_id):
        row = self.previews.get(preview_id)
        if row is None:
            return None
        if int(row.get("user_id") or 0) != int(user_id):
            return None
        return row

    async def insert_preview(
        self,
        *,
        preview_id,
        draft_id,
        expected_revision,
        user_id,
        subject,
        content,
        task_id,
    ):
        self.previews[preview_id] = {
            "preview_id": preview_id,
            "draft_id": draft_id,
            "expected_revision": expected_revision,
            "user_id": user_id,
            "subject": subject,
            "content": content,
            "status": "active",
            "task_id": task_id,
            "last_error": None,
            "created_at": "2026-09-01T10:00:00Z",
            "updated_at": "2026-09-01T10:00:00Z",
        }
        self.preview_by_draft_rev[(draft_id, expected_revision)] = preview_id

    async def mark_preview_status(self, *, preview_id, status, last_error=None):
        row = self.previews.get(preview_id)
        if row is None:
            return
        row["status"] = status
        if last_error is not None:
            row["last_error"] = last_error

    async def mark_active_stale_for_draft_except(self, draft_id, except_preview_id):
        n = 0
        for row in self.previews.values():
            if (
                row.get("draft_id") == draft_id
                and row.get("status") == "active"
                and row.get("preview_id") != except_preview_id
            ):
                row["status"] = "stale"
                n += 1
        return n


def _seed_draft(repo, draft_id="d-1", user_id=42, subject="personal", expected_revision=3):
    repo.drafts[(draft_id, user_id)] = {
        "draft_id": draft_id,
        "user_id": user_id,
        "subject": subject,
        "status": "draft",
        "expected_revision": expected_revision,
    }


def test_schema_has_preview_module() -> None:
    """Phase 3 P3-01: 预览模块导出稳定 API。"""
    import app.services.ai.preview as preview_mod

    assert hasattr(preview_mod, "generate_preview")
    assert hasattr(preview_mod, "get_preview")
    assert hasattr(preview_mod, "mark_preview_stale_for_draft")
    assert hasattr(preview_mod, "confirm_publish_with_preview")
    assert hasattr(preview_mod, "PreviewConflict")


def test_generate_preview_creates_active_row() -> None:
    from app.services.ai.preview import generate_preview

    repo = _FakeRepo()
    _seed_draft(repo)
    rec = _await(
        generate_preview(
            user_id=42,
            draft_id="d-1",
            expected_revision=3,
            repo=repo,
        )
    )
    assert rec.status == "active"
    assert rec.preview_id != ""
    assert rec.draft_id == "d-1"
    assert rec.expected_revision == 3


def test_generate_preview_reuses_active_row() -> None:
    from app.services.ai.preview import generate_preview

    repo = _FakeRepo()
    _seed_draft(repo)
    first = _await(
        generate_preview(user_id=42, draft_id="d-1", expected_revision=3, repo=repo)
    )
    second = _await(
        generate_preview(user_id=42, draft_id="d-1", expected_revision=3, repo=repo)
    )
    assert first.preview_id == second.preview_id, "同一 active preview 应被复用"


def test_generate_preview_version_mismatch_raises() -> None:
    from app.services.ai.preview import PreviewConflict, generate_preview

    repo = _FakeRepo()
    _seed_draft(repo, expected_revision=5)
    with pytest.raises(PreviewConflict) as ei:
        _await(
            generate_preview(user_id=42, draft_id="d-1", expected_revision=4, repo=repo)
        )
    assert ei.value.code == "DRAFT_VERSION_CONFLICT"


def test_generate_preview_draft_not_owned_raises() -> None:
    from app.services.ai.preview import PreviewConflict, generate_preview

    repo = _FakeRepo()
    _seed_draft(repo, user_id=42)
    with pytest.raises(PreviewConflict) as ei:
        _await(
            generate_preview(user_id=99, draft_id="d-1", expected_revision=3, repo=repo)
        )
    assert ei.value.code == "DRAFT_NOT_FOUND"


def test_get_preview_returns_none_when_foreign() -> None:
    from app.services.ai.preview import generate_preview, get_preview

    repo = _FakeRepo()
    _seed_draft(repo, user_id=42)
    rec = _await(
        generate_preview(user_id=42, draft_id="d-1", expected_revision=3, repo=repo)
    )
    fetched = _await(get_preview(user_id=99, preview_id=rec.preview_id, repo=repo))
    assert fetched is None


def test_mark_preview_stale_excludes_protected() -> None:
    from app.services.ai.preview import (
        generate_preview,
        mark_preview_stale_for_draft,
    )

    repo = _FakeRepo()
    _seed_draft(repo)
    a = _await(
        generate_preview(user_id=42, draft_id="d-1", expected_revision=3, repo=repo)
    )
    _seed_draft(repo, expected_revision=4)  # 新 revision,复用同 draft
    b = _await(
        generate_preview(user_id=42, draft_id="d-1", expected_revision=4, repo=repo)
    )
    # 把 b 设为 except,a 应被置 stale
    n = _await(
        mark_preview_stale_for_draft(
            draft_id="d-1", except_preview_id=b.preview_id, repo=repo
        )
    )
    assert n == 1
    assert repo.previews[a.preview_id]["status"] == "stale"
    assert repo.previews[b.preview_id]["status"] == "active"


def test_confirm_publish_with_preview_succeeds_only_when_active_and_matching() -> None:
    from app.services.ai.preview import (
        PreviewConflict,
        confirm_publish_with_preview,
        generate_preview,
    )

    repo = _FakeRepo()
    _seed_draft(repo)
    rec = _await(
        generate_preview(user_id=42, draft_id="d-1", expected_revision=3, repo=repo)
    )
    out = _await(
        confirm_publish_with_preview(
            user_id=42,
            draft_id="d-1",
            expected_revision=3,
            preview_id=rec.preview_id,
            repo=repo,
        )
    )
    assert out.status == "confirmed"
    # 二次调用:已 confirmed → 失败
    with pytest.raises(PreviewConflict) as ei:
        _await(
            confirm_publish_with_preview(
                user_id=42,
                draft_id="d-1",
                expected_revision=3,
                preview_id=rec.preview_id,
                repo=repo,
            )
        )
    assert ei.value.code == "DRAFT_VERSION_CONFLICT"


def test_confirm_publish_mismatched_revision_raises() -> None:
    from app.services.ai.preview import (
        PreviewConflict,
        confirm_publish_with_preview,
        generate_preview,
    )

    repo = _FakeRepo()
    _seed_draft(repo, expected_revision=5)
    rec = _await(
        generate_preview(user_id=42, draft_id="d-1", expected_revision=5, repo=repo)
    )
    with pytest.raises(PreviewConflict):
        _await(
            confirm_publish_with_preview(
                user_id=42,
                draft_id="d-1",
                expected_revision=4,
                preview_id=rec.preview_id,
                repo=repo,
            )
        )