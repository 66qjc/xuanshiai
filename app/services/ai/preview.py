"""Phase 3 P3-01 —— 成稿预览服务(ai_profile_preview)。

职责:

- ``generate_preview`` 创建/复用当前 ``(draft_id, expected_revision)`` 的预览,
  并把任务入队 ``profile_preview`` worker(若已有 active 预览则直接复用)。
- ``get_preview`` 读取当前用户可见的预览;越权/不存在 → 404。
- ``mark_preview_stale_for_draft`` 当 draft 字段变化时,把同一 draft 下所有
  active 预览标为 stale(避免发布过时版本)。
- ``confirm_publish_with_preview`` 发布时校验 preview 状态必须 active 且 revision
  匹配;否则抛 ``DraftVersionConflict`` → 路由翻译 409 ``DRAFT_VERSION_CONFLICT``。

设计原则:

- 严格走 ``ai_profile_preview`` 表,不修改 ``ai_profile_draft``。
- 任何 ``_invalidate``/``_purge`` 都保留行(只改 status),便于 Phase 4 投影回溯。
- 不入普通日志:正文不记录,只记录 preview_id / draft_id / status 流转。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession


PREVIEW_STATUSES: tuple[str, ...] = ("active", "confirmed", "stale", "failed")


class PreviewConflict(Exception):
    """发布 preview 与当前 draft revision 不一致 / preview 不存在 / preview 非 active。

    Phase 3 路由层翻译为 HTTP 409 + ``DRAFT_VERSION_CONFLICT``。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PreviewRecord:
    preview_id: str
    draft_id: str
    expected_revision: int
    user_id: int
    subject: str
    content: str
    status: str
    task_id: str | None
    last_error: str | None
    created_at: str | None
    updated_at: str | None


class PreviewRepository(Protocol):
    """仓储协议(单元测试可直接 fake)。"""

    async def find_draft_for_owner(
        self, draft_id: str, user_id: int
    ) -> dict[str, Any] | None: ...

    async def find_active_preview(
        self, draft_id: str, expected_revision: int
    ) -> dict[str, Any] | None: ...

    async def find_preview_by_id(
        self, preview_id: str, user_id: int
    ) -> dict[str, Any] | None: ...

    async def insert_preview(
        self,
        *,
        preview_id: str,
        draft_id: str,
        expected_revision: int,
        user_id: int,
        subject: str,
        content: str,
        task_id: str | None,
    ) -> None: ...

    async def mark_preview_status(
        self,
        *,
        preview_id: str,
        status: str,
        last_error: str | None = None,
    ) -> None: ...

    async def mark_active_stale_for_draft_except(
        self, draft_id: str, except_preview_id: str | None
    ) -> int: ...


class SqlPreviewRepository:
    """真实 MySQL 仓储(Phase 3)。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def find_draft_for_owner(
        self, draft_id: str, user_id: int
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT draft_id, user_id, subject, status, expected_revision "
                "FROM ai_profile_draft "
                "WHERE draft_id = :draft_id AND user_id = :user_id LIMIT 1"
            ),
            {"draft_id": draft_id, "user_id": user_id},
        )
        row = result.first()
        return dict(row._mapping) if row is not None else None

    async def find_active_preview(
        self, draft_id: str, expected_revision: int
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT preview_id, draft_id, expected_revision, user_id, subject, "
                "content, status, task_id, last_error, created_at, updated_at "
                "FROM ai_profile_preview "
                "WHERE draft_id = :draft_id "
                "AND expected_revision = :expected_revision "
                "AND status = 'active' LIMIT 1"
            ),
            {"draft_id": draft_id, "expected_revision": expected_revision},
        )
        row = result.first()
        return dict(row._mapping) if row is not None else None

    async def find_preview_by_id(
        self, preview_id: str, user_id: int
    ) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT preview_id, draft_id, expected_revision, user_id, subject, "
                "content, status, task_id, last_error, created_at, updated_at "
                "FROM ai_profile_preview "
                "WHERE preview_id = :preview_id AND user_id = :user_id LIMIT 1"
            ),
            {"preview_id": preview_id, "user_id": user_id},
        )
        row = result.first()
        return dict(row._mapping) if row is not None else None

    async def insert_preview(
        self,
        *,
        preview_id: str,
        draft_id: str,
        expected_revision: int,
        user_id: int,
        subject: str,
        content: str,
        task_id: str | None,
    ) -> None:
        await self._db.execute(
            sql_text(
                "INSERT INTO ai_profile_preview "
                "(preview_id, draft_id, expected_revision, user_id, subject, "
                " content, status, task_id) "
                "VALUES (:preview_id, :draft_id, :expected_revision, :user_id, "
                " :subject, :content, 'active', :task_id)"
            ),
            {
                "preview_id": preview_id,
                "draft_id": draft_id,
                "expected_revision": expected_revision,
                "user_id": user_id,
                "subject": subject,
                "content": content,
                "task_id": task_id,
            },
        )

    async def mark_preview_status(
        self,
        *,
        preview_id: str,
        status: str,
        last_error: str | None = None,
    ) -> None:
        await self._db.execute(
            sql_text(
                "UPDATE ai_profile_preview SET status = :status, "
                "last_error = :last_error "
                "WHERE preview_id = :preview_id"
            ),
            {"preview_id": preview_id, "status": status, "last_error": last_error},
        )

    async def mark_active_stale_for_draft_except(
        self, draft_id: str, except_preview_id: str | None
    ) -> int:
        params: dict[str, Any] = {"draft_id": draft_id}
        sql = (
            "UPDATE ai_profile_preview SET status = 'stale' "
            "WHERE draft_id = :draft_id AND status = 'active'"
        )
        if except_preview_id:
            sql += " AND preview_id != :except_preview_id"
            params["except_preview_id"] = except_preview_id
        result = await self._db.execute(sql_text(sql), params)
        return int(result.rowcount or 0)


def _serialize(row: dict[str, Any]) -> PreviewRecord:
    return PreviewRecord(
        preview_id=str(row.get("preview_id") or ""),
        draft_id=str(row.get("draft_id") or ""),
        expected_revision=int(row.get("expected_revision") or 0),
        user_id=int(row.get("user_id") or 0),
        subject=str(row.get("subject") or "personal"),
        content=str(row.get("content") or ""),
        status=str(row.get("status") or "active"),
        task_id=row.get("task_id"),
        last_error=row.get("last_error"),
        created_at=str(row["created_at"]) if row.get("created_at") else None,
        updated_at=str(row["updated_at"]) if row.get("updated_at") else None,
    )


async def generate_preview(
    *,
    user_id: int,
    draft_id: str,
    expected_revision: int,
    repo: PreviewRepository,
    renderer=None,
) -> PreviewRecord:
    """创建/复用预览。

    - 校验 draft 属于 user_id 且 draft.expected_revision 与请求一致(否则 conflict)。
    - 同 ``(draft_id, expected_revision)`` 已存在 active 行 → 直接复用,不重复入队。
    - 否则新建 preview(初始 status='active',task_id 由调用方分配)。
    """
    draft = await repo.find_draft_for_owner(draft_id, user_id)
    if draft is None:
        raise PreviewConflict("DRAFT_NOT_FOUND", "草稿不存在或无权访问")
    if int(draft.get("expected_revision") or 0) != int(expected_revision):
        raise PreviewConflict(
            "DRAFT_VERSION_CONFLICT",
            "草稿已被更新,请刷新后再试",
        )
    existing = await repo.find_active_preview(draft_id, expected_revision)
    if existing is not None:
        return _serialize(existing)
    preview_id = uuid4().hex
    task_id = uuid4().hex
    subject = str(draft.get("subject") or "personal")
    if renderer is None:
        content = (
            f"草稿 {draft_id} 预览(rev {expected_revision})"
        )
    else:
        content = str(renderer(draft))
    await repo.insert_preview(
        preview_id=preview_id,
        draft_id=draft_id,
        expected_revision=expected_revision,
        user_id=user_id,
        subject=subject,
        content=content,
        task_id=task_id,
    )
    return PreviewRecord(
        preview_id=preview_id,
        draft_id=draft_id,
        expected_revision=expected_revision,
        user_id=user_id,
        subject=subject,
        content=content,
        status="active",
        task_id=task_id,
        last_error=None,
        created_at=None,
        updated_at=None,
    )


async def get_preview(
    *,
    user_id: int,
    preview_id: str,
    repo: PreviewRepository,
) -> PreviewRecord | None:
    """按 preview_id 读取;不在当前用户下返回 None(路由层翻译 404)。"""
    row = await repo.find_preview_by_id(preview_id, user_id)
    if row is None:
        return None
    return _serialize(row)


async def mark_preview_stale_for_draft(
    *,
    draft_id: str,
    except_preview_id: str | None,
    repo: PreviewRepository,
) -> int:
    """draft 字段变化时把同 draft 其它 active 预览置 stale;返回受影响行数。"""
    return await repo.mark_active_stale_for_draft_except(draft_id, except_preview_id)


async def confirm_publish_with_preview(
    *,
    user_id: int,
    draft_id: str,
    expected_revision: int,
    preview_id: str,
    repo: PreviewRepository,
) -> PreviewRecord:
    """校验发布所需的 preview 一致性。

    - preview 不属于本人 → ``DRAFT_VERSION_CONFLICT``
    - preview 与 draft 绑定 revision 不一致 → ``DRAFT_VERSION_CONFLICT``
    - preview status != active → ``DRAFT_VERSION_CONFLICT``
    - 全部通过 → 把 preview 标为 ``confirmed``,返回记录(路由层继续走原 publish)。
    """
    row = await repo.find_preview_by_id(preview_id, user_id)
    if row is None:
        raise PreviewConflict("DRAFT_VERSION_CONFLICT", "预览不存在或已过期")
    if str(row.get("draft_id") or "") != draft_id:
        raise PreviewConflict(
            "DRAFT_VERSION_CONFLICT",
            "预览与当前草稿不一致",
        )
    if int(row.get("expected_revision") or 0) != int(expected_revision):
        raise PreviewConflict(
            "DRAFT_VERSION_CONFLICT",
            "预览与草稿版本不一致,请重新生成预览",
        )
    if str(row.get("status") or "") != "active":
        raise PreviewConflict(
            "DRAFT_VERSION_CONFLICT",
            f"预览状态为 {row.get('status')},无法用于发布",
        )
    await repo.mark_preview_status(preview_id=preview_id, status="confirmed")
    return _serialize(row)