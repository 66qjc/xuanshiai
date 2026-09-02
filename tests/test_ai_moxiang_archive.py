"""Phase 3 P3-03 — 我的墨相档案聚合服务单元测试。

覆盖:
- build_archive 新用户两主体全空 + fallback_available=False
- personal 已发布 + ideal_partner 草稿 → 双主体分别聚合正确
- 撤回授权(consent_active=False)仍可读主体资产
- 摘要超长截断(_excerpt)
- fallback_available 判定逻辑
- archive REST 端点返回结构稳定
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.ai.archive import (
    ArchiveRepository,
    SubjectArchive,
    _excerpt,
    build_archive,
    build_subject_archive,
)


def _await(coro: Any) -> Any:
    """同步驱动 async 函数;Phase 3 单元测试不引入 pytest-asyncio。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeRepo:
    """ArchiveRepository 协议 fake —— 单元测试可注入受控数据。"""

    def __init__(self) -> None:
        self.consents: dict[tuple[int, str, str], bool] = {}
        self.revisions: dict[tuple[int, str], dict] = {}
        self.drafts: dict[tuple[int, str], dict] = {}
        self.history: dict[tuple[int, str], list[dict]] = {}
        self.summaries: dict[tuple[int, str], dict] = {}
        self.previews: dict[tuple[int, str], dict] = {}

    def has_active_consent(self, user_id, scope, version):
        return bool(self.consents.get((int(user_id), str(scope), str(version)), False))

    def find_current_revision(self, user_id, subject):
        return self.revisions.get((int(user_id), str(subject)))

    def find_active_draft(self, user_id, subject):
        return self.drafts.get((int(user_id), str(subject)))

    def list_recent_revisions(self, user_id, subject, limit):
        rows = list(self.history.get((int(user_id), str(subject)), []))
        return rows[: int(limit)]

    def find_latest_summary(self, user_id, subject):
        return self.summaries.get((int(user_id), str(subject)))

    def find_latest_active_preview(self, user_id, draft_id):
        if not draft_id:
            return None
        return self.previews.get((int(user_id), str(draft_id)))


class _AsyncRepo(_FakeRepo):
    """真实 SQL 仓储同构 fake：所有归档取数方法均返回 awaitable。"""

    async def has_active_consent(self, user_id, scope, version):
        return super().has_active_consent(user_id, scope, version)

    async def find_current_revision(self, user_id, subject):
        return super().find_current_revision(user_id, subject)

    async def find_active_draft(self, user_id, subject):
        return super().find_active_draft(user_id, subject)

    async def list_recent_revisions(self, user_id, subject, limit):
        return super().list_recent_revisions(user_id, subject, limit)

    async def find_latest_summary(self, user_id, subject):
        return super().find_latest_summary(user_id, subject)

    async def find_latest_active_preview(self, user_id, draft_id):
        return super().find_latest_active_preview(user_id, draft_id)


def test_excerpt_handles_empty_and_long() -> None:
    assert _excerpt(None) is None
    assert _excerpt("") is None
    assert _excerpt(123) is None  # type: ignore[arg-type]
    short = "你好世界"
    assert _excerpt(short) == short
    long = "一二三四五" * 50  # 250 字符
    out = _excerpt(long, limit=20)
    assert out is not None
    assert len(out) <= 21  # 20 + "…"
    assert out.endswith("…")


def test_new_user_archive_both_empty() -> None:
    repo = _FakeRepo()
    out = _await(
        build_archive(user_id=1001, repo=repo)
    )
    assert out.personal.subject == "personal"
    assert out.ideal_partner.subject == "ideal_partner"
    assert out.personal.current_revision is None
    assert out.personal.active_draft is None
    assert out.personal.history == []
    assert out.personal.preview is None
    assert out.personal.summary_excerpt is None
    assert out.personal.consent_active is False
    assert out.ideal_partner.current_revision is None
    assert out.fallback_available is False


def test_archive_awaits_async_sql_repository() -> None:
    """归档聚合必须 await 生产仓储，不能把 coroutine 当作字典处理。"""
    repo = _AsyncRepo()
    out = _await(build_archive(user_id=1002, repo=repo))
    assert out.personal.current_revision is None
    assert out.ideal_partner.current_revision is None


def test_archive_aggregates_personal_published_and_ideal_partner_draft() -> None:
    repo = _FakeRepo()
    repo.consents[(9001, "profile_text_extract", "v1")] = True
    repo.revisions[(9001, "personal")] = {
        "revision_id": 11,
        "revision_no": 1,
        "policy_revision": "policy-v1",
        "published_at": "2026-08-30T08:00:00",
    }
    repo.history[(9001, "personal")] = [
        {"revision_id": 11, "revision_no": 1, "published_at": "2026-08-30T08:00:00", "field_count": 6},
        {"revision_id": 10, "revision_no": 0, "published_at": "2026-08-20T08:00:00", "field_count": 5},
    ]
    repo.summaries[(9001, "personal")] = {
        "summary_text": "墨案简短摘要" * 30,
        "status": "published",
    }
    repo.drafts[(9001, "ideal_partner")] = {
        "draft_id": "dft_ideal_001",
        "status": "in_progress",
        "expected_revision": 7,
        "updated_at": "2026-08-31T12:00:00",
    }
    repo.previews[(9001, "dft_ideal_001")] = {
        "preview_id": "pv_001",
        "expected_revision": 7,
        "status": "active",
        "created_at": "2026-08-31T12:30:00",
        "updated_at": "2026-08-31T12:30:00",
    }

    out = _await(build_archive(user_id=9001, repo=repo))
    # personal
    assert out.personal.consent_active is True
    assert out.personal.current_revision is not None
    assert out.personal.current_revision.revision_id == 11
    assert out.personal.current_revision.revision_no == 1
    assert out.personal.active_draft is None
    assert len(out.personal.history) == 2
    assert out.personal.history[0].revision_id == 11
    assert out.personal.history[1].field_count == 5
    assert out.personal.summary_excerpt is not None
    assert out.personal.summary_excerpt.endswith("…")
    assert out.personal.preview is None
    # ideal_partner
    assert out.ideal_partner.consent_active is True
    assert out.ideal_partner.current_revision is None
    assert out.ideal_partner.active_draft is not None
    assert out.ideal_partner.active_draft.draft_id == "dft_ideal_001"
    assert out.ideal_partner.active_draft.expected_revision == 7
    assert out.ideal_partner.preview is not None
    assert out.ideal_partner.preview.preview_id == "pv_001"
    # fallback_available:任一主体有 revision 或 draft
    assert out.fallback_available is True


def test_archive_to_dict_shape_is_stable() -> None:
    repo = _FakeRepo()
    out = _await(build_archive(user_id=42, repo=repo))
    payload = out.to_dict()
    assert set(payload.keys()) == {"personal", "ideal_partner", "fallback_available"}
    personal = payload["personal"]
    assert set(personal.keys()) == {
        "subject",
        "consent_active",
        "current_revision",
        "active_draft",
        "summary_excerpt",
        "history",
        "preview",
    }
    assert personal["current_revision"] is None
    assert personal["history"] == []
    assert personal["preview"] is None


def test_consent_revoked_still_readable() -> None:
    """撤回授权后主体资产卡仍可读,consent_active=False 但 revision 保留。"""
    repo = _FakeRepo()
    # consents 中不写 key → has_active_consent 返回 False
    repo.revisions[(8001, "personal")] = {
        "revision_id": 1,
        "revision_no": 0,
        "policy_revision": "policy-v1",
        "published_at": "2026-08-01T00:00:00",
    }
    out = _await(
        build_subject_archive(user_id=8001, subject="personal", repo=repo)
    )
    assert out.consent_active is False
    assert out.current_revision is not None
    assert out.current_revision.revision_id == 1


def test_active_draft_with_no_preview_returns_none_preview() -> None:
    repo = _FakeRepo()
    repo.drafts[(7001, "personal")] = {
        "draft_id": "dft_p_1",
        "status": "in_progress",
        "expected_revision": 0,
        "updated_at": "2026-09-01T00:00:00",
    }
    out = _await(
        build_subject_archive(user_id=7001, subject="personal", repo=repo)
    )
    assert out.active_draft is not None
    assert out.preview is None


def test_fallback_available_requires_real_assets() -> None:
    """仅 consent 不算 fallback;必须 revision 或 draft 实际存在。"""
    repo = _FakeRepo()
    repo.consents[(5001, "profile_text_extract", "v1")] = True
    out = _await(build_archive(user_id=5001, repo=repo))
    assert out.personal.consent_active is True
    assert out.ideal_partner.consent_active is True
    assert out.fallback_available is False

    repo.revisions[(5001, "ideal_partner")] = {
        "revision_id": 1,
        "revision_no": 0,
        "policy_revision": "policy-v1",
        "published_at": "2026-09-01T00:00:00",
    }
    out2 = _await(build_archive(user_id=5001, repo=repo))
    assert out2.fallback_available is True


def test_repository_protocol_satisfied_by_fake() -> None:
    """FakeRepo 实现了 ArchiveRepository 协议(静态 duck-typing 校验)。"""
    repo: ArchiveRepository = _FakeRepo()  # type: ignore[assignment]
    # 仅做方法存在性确认
    for name in (
        "has_active_consent",
        "find_current_revision",
        "find_active_draft",
        "list_recent_revisions",
        "find_latest_summary",
        "find_latest_active_preview",
    ):
        assert hasattr(repo, name), name


def test_subject_archive_is_dataclass() -> None:
    """SubjectArchive 仍是 dataclass(契约稳定字段)。"""
    sa = SubjectArchive(
        subject="personal",
        consent_active=True,
        current_revision=None,
        active_draft=None,
        summary_excerpt=None,
        history=[],
        preview=None,
    )
    d = sa.to_dict()
    assert d["subject"] == "personal"
    assert d["consent_active"] is True
    assert d["current_revision"] is None
