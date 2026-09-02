"""Phase 4 P4-01 —— 投影准入位服务(ai_profile_projection_status)。

职责:

- 每个 (user_id, kind) 唯一对应一个 status 状态行。
- 下游消费者(搜索/匹配/推荐)通过本表过滤:仅当 status='active' 时,
  才允许读 ai_feature_projection 中相应 kind 的最新匹配行。
- 状态机:
    pending     -- 投影构建任务入队,下游视为"无投影"
    active      -- 唯一有效,下游可读
    invalidated -- 旧版本被新版本踢出,数据保留
    deleted     -- 画像删除/授权撤回,数据保留但下游永远不读
    failed      -- 投影构建失败,可由下次发布重新置 pending

设计原则:

- 不修改 ai_feature_projection,仅在其上方加一层"准入位"。
- 所有状态切换走 UPDATE(基于 UNIQUE(user_id, kind) 单行),不允许
  INSERT ON DUPLICATE KEY 之外的方式(避免 race condition 出现双 active)。
- 状态切换 + projection_id 设置放在同一事务;绝不出现
  "status=active 但 projection_id 为 NULL" 的中间态。
- 不入普通日志,只记录 user_id / kind / status 流转 + 关联 revision_id。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession


# 状态常量(与 SQL ENUM 同步,前端不会读)
STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_INVALIDATED = "invalidated"
STATUS_DELETED = "deleted"
STATUS_FAILED = "failed"
ALL_STATUSES: tuple[str, ...] = (
    STATUS_PENDING,
    STATUS_ACTIVE,
    STATUS_INVALIDATED,
    STATUS_DELETED,
    STATUS_FAILED,
)

# 已知的 kind 枚举(与 ai_feature_projection.projection_kind 对齐)
KIND_PERSONAL_SEARCHABLE = "personal_searchable"
KIND_PERSONAL_COMPATIBILITY = "personal_compatibility"
KIND_IDEAL_PARTNER_PREFERENCE = "ideal_partner_preference"
ALL_KINDS: tuple[str, ...] = (
    KIND_PERSONAL_SEARCHABLE,
    KIND_PERSONAL_COMPATIBILITY,
    KIND_IDEAL_PARTNER_PREFERENCE,
)

# 每个 subject 重建时同时影响的所有 kind(rebuild_subject_kinds)
KIND_FOR_SUBJECT = {
    "personal": (KIND_PERSONAL_SEARCHABLE, KIND_PERSONAL_COMPATIBILITY),
    "ideal_partner": (KIND_IDEAL_PARTNER_PREFERENCE,),
}


@dataclass(frozen=True)
class ProjectionStatusRecord:
    user_id: int
    kind: str
    status: str
    source_revision: int | None
    projection_id: int | None
    last_error: str | None
    activated_at: str | None
    invalidated_at: str | None
    deleted_at: str | None
    created_at: str | None
    updated_at: str | None


class ProjectionStatusRepository(Protocol):
    """仓储协议(单元测试可直接 fake)。"""

    def get(self, user_id: int, kind: str) -> dict[str, Any] | None: ...

    def upsert(
        self,
        *,
        user_id: int,
        kind: str,
        status: str,
        source_revision: int | None,
        projection_id: int | None,
        last_error: str | None,
    ) -> None: ...

    def get_active_for_user(self, user_id: int) -> list[dict[str, Any]]: ...


class SqlProjectionStatusRepository:
    """生产 SQL 仓储(Phase 4)。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, user_id: int, kind: str) -> dict[str, Any] | None:
        result = await self._db.execute(
            sql_text(
                "SELECT user_id, kind, status, source_revision, projection_id, "
                "last_error, activated_at, invalidated_at, deleted_at, "
                "created_at, updated_at "
                "FROM ai_profile_projection_status "
                "WHERE user_id = :user_id AND kind = :kind LIMIT 1"
            ),
            {"user_id": user_id, "kind": kind},
        )
        row = result.first()
        if row is None:
            return None
        try:
            return dict(row._mapping)
        except AttributeError:
            return dict(row)

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
        if status not in ALL_STATUSES:
            raise ValueError(f"unknown projection_status: {status}")
        if kind not in ALL_KINDS:
            raise ValueError(f"unknown projection_kind: {kind}")
        # 状态机相关时间戳自动维护
        ts_col = {
            STATUS_PENDING: None,
            STATUS_ACTIVE: "activated_at",
            STATUS_INVALIDATED: "invalidated_at",
            STATUS_DELETED: "deleted_at",
            STATUS_FAILED: None,
        }[status]
        params: dict[str, Any] = {
            "user_id": user_id,
            "kind": kind,
            "status": status,
            "source_revision": source_revision,
            "projection_id": projection_id,
            "last_error": last_error,
        }
        set_clauses = [
            "status = :status",
            "source_revision = :source_revision",
            "projection_id = :projection_id",
            "last_error = :last_error",
        ]
        if ts_col:
            set_clauses.append(f"{ts_col} = COALESCE({ts_col}, UTC_TIMESTAMP())")
        sql = (
            "INSERT INTO ai_profile_projection_status "
            "(user_id, kind, status, source_revision, projection_id, last_error) "
            "VALUES (:user_id, :kind, :status, :source_revision, :projection_id, :last_error) "
            "ON DUPLICATE KEY UPDATE "
            + ", ".join(set_clauses)
        )
        await self._db.execute(sql_text(sql), params)

    async def get_active_for_user(self, user_id: int) -> list[dict[str, Any]]:
        result = await self._db.execute(
            sql_text(
                "SELECT user_id, kind, source_revision, projection_id, "
                "activated_at "
                "FROM ai_profile_projection_status "
                "WHERE user_id = :user_id AND status = 'active'"
            ),
            {"user_id": user_id},
        )
        return [dict(r._mapping) for r in result.mappings().all()]


# ----------------------------------------------------------------------
# 业务投影
# ----------------------------------------------------------------------


def _serialize(row: dict[str, Any]) -> ProjectionStatusRecord:
    return ProjectionStatusRecord(
        user_id=int(row.get("user_id") or 0),
        kind=str(row.get("kind") or ""),
        status=str(row.get("status") or "pending"),
        source_revision=(
            int(row["source_revision"])
            if row.get("source_revision") is not None
            else None
        ),
        projection_id=(
            int(row["projection_id"]) if row.get("projection_id") is not None else None
        ),
        last_error=row.get("last_error"),
        activated_at=_format_dt(row.get("activated_at")),
        invalidated_at=_format_dt(row.get("invalidated_at")),
        deleted_at=_format_dt(row.get("deleted_at")),
        created_at=_format_dt(row.get("created_at")),
        updated_at=_format_dt(row.get("updated_at")),
    )


def _format_dt(value: Any) -> str | None:
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


async def mark_pending(
    *,
    user_id: int,
    kind: str,
    repo: ProjectionStatusRepository,
) -> None:
    """发布后:把目标 kind 置为 pending(等待 worker 写入 ai_feature_projection)。"""
    await repo.upsert(
        user_id=user_id,
        kind=kind,
        status=STATUS_PENDING,
        source_revision=None,
        projection_id=None,
        last_error=None,
    )


async def mark_active(
    *,
    user_id: int,
    kind: str,
    source_revision: int,
    projection_id: int,
    repo: ProjectionStatusRepository,
) -> None:
    """worker 完成后:把 (user, kind) 置 active 并绑定 projection_id。
    同 kind 已有 active 行 → 走 UPSERT(替换)。
    """
    await repo.upsert(
        user_id=user_id,
        kind=kind,
        status=STATUS_ACTIVE,
        source_revision=source_revision,
        projection_id=projection_id,
        last_error=None,
    )


async def mark_invalidated(
    *,
    user_id: int,
    kind: str,
    repo: ProjectionStatusRepository,
    reason: str | None = None,
) -> None:
    """新版本发布时把同 kind 旧 active 行 → invalidated(数据保留)。"""
    await repo.upsert(
        user_id=user_id,
        kind=kind,
        status=STATUS_INVALIDATED,
        source_revision=None,
        projection_id=None,
        last_error=reason,
    )


async def mark_deleted(
    *,
    user_id: int,
    kind: str,
    repo: ProjectionStatusRepository,
    reason: str | None = None,
) -> None:
    """画像删除/授权撤回:把同 kind 行 → deleted(数据保留但下游不读)。"""
    await repo.upsert(
        user_id=user_id,
        kind=kind,
        status=STATUS_DELETED,
        source_revision=None,
        projection_id=None,
        last_error=reason,
    )


async def mark_failed(
    *,
    user_id: int,
    kind: str,
    repo: ProjectionStatusRepository,
    error: str,
) -> None:
    """worker 失败:置 failed(可由下次发布重新置 pending)。"""
    await repo.upsert(
        user_id=user_id,
        kind=kind,
        status=STATUS_FAILED,
        source_revision=None,
        projection_id=None,
        last_error=error,
    )


async def is_projection_active(
    *,
    user_id: int,
    kind: str,
    repo: ProjectionStatusRepository,
) -> bool:
    """下游消费者用:仅当 status='active' 才读 ai_feature_projection。"""
    row = await repo.get(user_id, kind)
    return row is not None and str(row.get("status") or "") == STATUS_ACTIVE


async def filter_active_kinds(
    *,
    user_id: int,
    kinds: list[str],
    repo: ProjectionStatusRepository,
) -> list[str]:
    """批量过滤:返回 user 在这些 kind 中处于 active 状态的子集。"""
    out: list[str] = []
    for kind in kinds:
        if await is_projection_active(user_id=user_id, kind=kind, repo=repo):
            out.append(kind)
    return out
