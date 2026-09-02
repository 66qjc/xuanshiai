"""Phase 3 P3-03 —— 我的墨相档案聚合服务。

职责:为 ``GET /ai/moxiang/archive`` 提供 ``personal`` + ``ideal_partner`` 两主体
的归档聚合(只读)。不修改任何表,不入普通日志。

聚合结构(契约 v1.1 草稿 §8.3 / 任务清单 P3-03):

```
{
  "personal": {
    "subject": "personal",
    "consent_active": bool,
    "current_revision": {revision_id, revision_no, published_at} | null,
    "active_draft":    {draft_id, status, expected_revision, updated_at} | null,
    "summary_excerpt": str | null,
    "history": [{revision_id, revision_no, published_at, field_count}, ...],
    "preview":   {preview_id, expected_revision, status, created_at} | null,
  },
  "ideal_partner": { ... 同上 },
  "fallback_available": bool
}
```

设计要点:

- 所有 SQL 由 ``ArchiveRepository`` 协议暴露,单元测试可注入 fake。
- 公开字段全部 snake_case,前端 ``archiveAdapt`` 翻译 camelCase。
- 删除/撤回后聚合仍可读(主体资产卡继续可见,只把投影状态标记为
  ``invalidated``/``deleted``)——见 Phase 4 投影边界。
- 不返回正文(只返回 summary_excerpt + preview_id 引用),正文另走
  ``GET /profile-previews/{preview_id}`` 与 ``GET /profiles/{subject}/narrative``。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import inspect
from typing import Any, Awaitable, Protocol

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession


_ARCHIVE_SUBJECTS: tuple[str, ...] = ("personal", "ideal_partner")
_HISTORY_LIMIT = 20


async def _resolve(value: Any) -> Any:
    """兼容生产 async 仓储与纯函数测试 fake 的同步返回值。"""
    if inspect.isawaitable(value):
        return await value
    return value


# ----------------------------------------------------------------------
# 响应模型
# ----------------------------------------------------------------------


@dataclass
class CurrentRevisionInfo:
    revision_id: int
    revision_no: int
    published_at: str | None
    policy_revision: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActiveDraftInfo:
    draft_id: str
    status: str
    expected_revision: int
    updated_at: str | None
    last_topic_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HistoryItem:
    revision_id: int
    revision_no: int
    published_at: str | None
    field_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PreviewBrief:
    preview_id: str
    expected_revision: int
    status: str
    created_at: str | None
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubjectArchive:
    subject: str
    consent_active: bool
    current_revision: CurrentRevisionInfo | None
    active_draft: ActiveDraftInfo | None
    summary_excerpt: str | None
    history: list[HistoryItem] = field(default_factory=list)
    preview: PreviewBrief | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "consent_active": self.consent_active,
            "current_revision": (
                self.current_revision.to_dict() if self.current_revision else None
            ),
            "active_draft": (
                self.active_draft.to_dict() if self.active_draft else None
            ),
            "summary_excerpt": self.summary_excerpt,
            "history": [item.to_dict() for item in self.history],
            "preview": self.preview.to_dict() if self.preview else None,
        }


@dataclass
class ArchiveResponse:
    personal: SubjectArchive
    ideal_partner: SubjectArchive
    fallback_available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "personal": self.personal.to_dict(),
            "ideal_partner": self.ideal_partner.to_dict(),
            "fallback_available": self.fallback_available,
        }


# ----------------------------------------------------------------------
# 仓储协议
# ----------------------------------------------------------------------


class ArchiveRepository(Protocol):
    """``archive`` 服务的仓储协议(单元测试可直接 fake)。"""

    def has_active_consent(
        self, user_id: int, scope: str, version: str
    ) -> bool | Awaitable[bool]: ...

    def find_current_revision(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]: ...

    def find_active_draft(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]: ...

    def list_recent_revisions(
        self, user_id: int, subject: str, limit: int
    ) -> list[dict[str, Any]] | Awaitable[list[dict[str, Any]]]: ...

    def find_latest_summary(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]: ...

    def find_latest_active_preview(
        self, user_id: int, draft_id: str | None
    ) -> dict[str, Any] | None | Awaitable[dict[str, Any] | None]: ...


class SqlArchiveRepository:
    """生产 SQL 仓储(Phase 3)。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def has_active_consent(
        self, user_id: int, scope: str, version: str
    ) -> bool:
        result = await self._db.execute(
            sql_text(
                "SELECT 1 FROM ai_consent_grant "
                "WHERE user_id = :user_id AND scope = :scope AND version = :version "
                "AND revoked_at IS NULL LIMIT 1"
            ),
            {"user_id": user_id, "scope": scope, "version": version},
        )
        return result.first() is not None

    async def find_current_revision(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT r.id AS revision_id, r.revision_no, r.policy_revision, "
                "r.published_at "
                "FROM ai_profile_revision r "
                "WHERE r.user_id = :user_id AND r.subject = :subject "
                "ORDER BY r.id DESC LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        row = result.first()
        if row is None:
            return None
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)

    async def find_active_draft(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT draft_id, status, expected_revision, updated_at "
                "FROM ai_profile_draft "
                "WHERE user_id = :user_id AND subject = :subject "
                "AND status NOT IN ('archived','published') "
                "ORDER BY updated_at DESC LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        row = result.first()
        if row is None:
            return None
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)

    async def list_recent_revisions(
        self, user_id: int, subject: str, limit: int
    ) -> list[dict[str, Any]]:
        result = await self._db.execute(
            sql_text(
                "SELECT r.id AS revision_id, r.revision_no, r.published_at, "
                "COUNT(f.revision_id) AS field_count "
                "FROM ai_profile_revision r "
                "LEFT JOIN ai_profile_revision_field f ON f.revision_id = r.id "
                "WHERE r.user_id = :user_id AND r.subject = :subject "
                "GROUP BY r.id, r.revision_no, r.published_at "
                "ORDER BY r.id DESC LIMIT :limit"
            ),
            {"user_id": user_id, "subject": subject, "limit": int(limit)},
        )
        rows = result.mappings().all()
        return [dict(r) for r in rows]

    async def find_latest_summary(
        self, user_id: int, subject: str
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT summary_text, status, content_hash, created_at, updated_at "
                "FROM ai_profile_summary "
                "WHERE user_id = :user_id AND subject = :subject "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"user_id": user_id, "subject": subject},
        )
        row = result.first()
        if row is None:
            return None
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)

    async def find_latest_active_preview(
        self, user_id: int, draft_id: str | None
    ) -> dict[str, Any] | None:
        if not draft_id:
            return None
        result = await self._db.execute(
            sql_text(
                "SELECT preview_id, expected_revision, status, created_at, updated_at "
                "FROM ai_profile_preview "
                "WHERE user_id = :user_id AND draft_id = :draft_id "
                "AND status = 'active' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"user_id": user_id, "draft_id": draft_id},
        )
        row = result.first()
        if row is None:
            return None
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)


# ----------------------------------------------------------------------
# 业务投影
# ----------------------------------------------------------------------


def _excerpt(text: str | None, limit: int = 120) -> str | None:
    """摘要截断,空 / None / 非 str 都返回 None。"""
    if not isinstance(text, str) or not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


async def build_subject_archive(
    *,
    user_id: int,
    subject: str,
    repo: ArchiveRepository,
    consent_scope: str = "profile_text_extract",
    consent_version: str = "v1",
) -> SubjectArchive:
    """装配单个主体的归档信息。"""
    consent_active = await _resolve(
        repo.has_active_consent(user_id, consent_scope, consent_version)
    )
    cur = await _resolve(repo.find_current_revision(user_id, subject))
    draft = await _resolve(repo.find_active_draft(user_id, subject))
    history_rows = await _resolve(
        repo.list_recent_revisions(user_id, subject, _HISTORY_LIMIT)
    )
    summary = await _resolve(repo.find_latest_summary(user_id, subject))
    preview_row = await _resolve(
        repo.find_latest_active_preview(
            user_id, draft["draft_id"] if draft else None
        )
    )

    current_revision: CurrentRevisionInfo | None = None
    if cur is not None:
        current_revision = CurrentRevisionInfo(
            revision_id=int(cur.get("revision_id") or 0),
            revision_no=int(cur.get("revision_no") or 0),
            published_at=_format_dt(cur.get("published_at")),
            policy_revision=str(cur.get("policy_revision") or "") or None,
        )

    active_draft: ActiveDraftInfo | None = None
    if draft is not None:
        active_draft = ActiveDraftInfo(
            draft_id=str(draft.get("draft_id") or ""),
            status=str(draft.get("status") or ""),
            expected_revision=int(draft.get("expected_revision") or 0),
            updated_at=_format_dt(draft.get("updated_at")),
        )

    history: list[HistoryItem] = []
    for row in history_rows:
        history.append(
            HistoryItem(
                revision_id=int(row.get("revision_id") or 0),
                revision_no=int(row.get("revision_no") or 0),
                published_at=_format_dt(row.get("published_at")),
                field_count=int(row.get("field_count") or 0),
            )
        )

    summary_excerpt = _excerpt(
        str(summary.get("summary_text"))
        if summary and isinstance(summary, dict)
        else None
    )

    preview: PreviewBrief | None = None
    if preview_row is not None:
        preview = PreviewBrief(
            preview_id=str(preview_row.get("preview_id") or ""),
            expected_revision=int(preview_row.get("expected_revision") or 0),
            status=str(preview_row.get("status") or "active"),
            created_at=_format_dt(preview_row.get("created_at")),
            updated_at=_format_dt(preview_row.get("updated_at")),
        )

    return SubjectArchive(
        subject=subject,
        consent_active=bool(consent_active),
        current_revision=current_revision,
        active_draft=active_draft,
        summary_excerpt=summary_excerpt,
        history=history,
        preview=preview,
    )


async def build_archive(
    *,
    user_id: int,
    repo: ArchiveRepository,
    consent_scope: str = "profile_text_extract",
    consent_version: str = "v1",
) -> ArchiveResponse:
    """装配双主体归档。"""
    personal = await build_subject_archive(
        user_id=user_id,
        subject="personal",
        repo=repo,
        consent_scope=consent_scope,
        consent_version=consent_version,
    )
    ideal_partner = await build_subject_archive(
        user_id=user_id,
        subject="ideal_partner",
        repo=repo,
        consent_scope=consent_scope,
        consent_version=consent_version,
    )
    # fallback_available —— 至少一个主体有 current_revision 或 active_draft
    fallback_available = (
        personal.current_revision is not None
        or personal.active_draft is not None
        or ideal_partner.current_revision is not None
        or ideal_partner.active_draft is not None
    )
    return ArchiveResponse(
        personal=personal,
        ideal_partner=ideal_partner,
        fallback_available=fallback_available,
    )


def _format_dt(value: Any) -> str | None:
    """Datetime / str → ISO 字符串。None / 非可序列化 → None。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, str):
        return value
    return str(value)
