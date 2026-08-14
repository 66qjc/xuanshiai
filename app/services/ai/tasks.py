"""AI-CORE durable task runtime: state machine, leases, cancel and recovery.

This module is the single authority over the ``ai_task`` fact table
(统一方案 §6.4, §11.3; execution plan §3.1/§3.2).  Every transition is checked
against :data:`ALLOWED_TRANSITIONS` before it is persisted, workers claim rows
with a locking ``SELECT ... FOR UPDATE SKIP LOCKED`` and write leases with a
conditional ``UPDATE``, and a result is only committed after the task, the
cancel flag and the source revision vector are re-read.  Version or consent
invalidation funnels a running task into ``superseded`` instead of letting an
old result overwrite a newer state.

Redis is never the source of truth: a failed Redis notification only degrades
a task, never its MySQL state.  Following the Task 4 outbox precedent, none of
the functions in this module call ``commit()`` — the caller's transaction owns
durability.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.schemas.ai_common import AiTaskStatus
from app.services.ai.flags import AiFeature, AiFeatureDisabledError, require_ai_feature
from app.services.revisions import RevisionVector

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 状态机 (统一方案 §6.4, 执行计划 §3.1)
# ----------------------------------------------------------------------

ALLOWED_TRANSITIONS: dict[AiTaskStatus, set[AiTaskStatus]] = {
    AiTaskStatus.QUEUED: {AiTaskStatus.LEASED, AiTaskStatus.CANCELLED},
    AiTaskStatus.LEASED: {
        AiTaskStatus.RUNNING,
        AiTaskStatus.RETRY_WAIT,
        AiTaskStatus.CANCELLED,
    },
    AiTaskStatus.RUNNING: {
        AiTaskStatus.SUCCEEDED,
        AiTaskStatus.RETRY_WAIT,
        AiTaskStatus.FAILED,
        AiTaskStatus.CANCELLED,
        AiTaskStatus.SUPERSEDED,
    },
    AiTaskStatus.RETRY_WAIT: {AiTaskStatus.LEASED, AiTaskStatus.CANCELLED},
}

# 可取消窗口：queued/leased/running/retry_wait → cancelled。
_CANCELLABLE = frozenset(
    {
        AiTaskStatus.QUEUED,
        AiTaskStatus.LEASED,
        AiTaskStatus.RUNNING,
        AiTaskStatus.RETRY_WAIT,
    }
)
# Worker 可领取的状态。
_CLAIMABLE = frozenset({AiTaskStatus.QUEUED, AiTaskStatus.RETRY_WAIT})
# 持有租约的任务状态。
_LEASED_OR_RUNNING = frozenset({AiTaskStatus.LEASED, AiTaskStatus.RUNNING})
# 终态，取消/完成/失败写入均不再覆盖。
_TERMINAL = frozenset(
    {
        AiTaskStatus.SUCCEEDED,
        AiTaskStatus.FAILED,
        AiTaskStatus.CANCELLED,
        AiTaskStatus.SUPERSEDED,
    }
)

# 指数退避：30s → 60s → 120s ... 封顶 15 分钟。
RETRY_BACKOFF_BASE_SECONDS = 30
RETRY_BACKOFF_CAP_SECONDS = 900

# 稳定错误码 → 安全文案。任何写回 ai_task.error_message 的文本都来自这里，
# 普通响应和日志绝不携带 provider trace 或原文。
_SAFE_ERROR_MESSAGES: dict[str, str] = {
    "AI_FEATURE_DISABLED": "AI 功能未启用",
    "AI_INPUT_INVALID": "provider 输出未通过 Schema 校验",
    "AI_POLICY_DENIED": "请求未通过 AI 安全与策略校验",
    "AI_QUOTA_EXCEEDED": "AI 服务请求频率过高，请稍后重试",
    "AI_TEMPORARILY_UNAVAILABLE": "AI 服务暂时不可用",
    "AI_CONSENT_REQUIRED": "AI 授权不可用，任务无法继续",
    "RESULT_STALE": "输入或策略版本已变化",
}
_DEFAULT_SAFE_ERROR_MESSAGE = "AI 服务调用失败"

_SELECT_COLUMNS = """
    id, task_id, owner_user_id, task_type, scene, idempotency_key,
    request_digest, status, stage, attempt_count, max_attempts, next_run_at,
    lease_owner, lease_until, consent_snapshot_json, source_revision_json,
    payload_summary, error_code, error_message, result_ref,
    created_at, updated_at, started_at, finished_at
"""


def assert_transition(source: AiTaskStatus, target: AiTaskStatus) -> None:
    """Raise :class:`ValueError` unless ``source -> target`` is a legal move.

    The error message always contains the ``illegal ai_task transition``
    marker that tests and the state machine rely on.
    """
    if target not in ALLOWED_TRANSITIONS.get(source, set()):
        raise ValueError(f"illegal ai_task transition: {source} -> {target}")


class TaskError(Exception):
    """Stable domain error of the task machine (§11.2/§3.2).

    ``code`` is a frozen business code, ``status_code`` its HTTP mapping and
    ``message`` is always safe for error responses (never provider text).
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        retryable: bool = False,
        retry_after_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms


@dataclass(frozen=True)
class AiTaskRecord:
    """One ``ai_task`` row surfaced to callers and the worker."""

    id: int
    task_id: str
    owner_user_id: int
    task_type: str
    scene: str
    idempotency_key: str
    request_digest: str | None
    status: AiTaskStatus
    stage: str | None
    attempt_count: int
    max_attempts: int
    next_run_at: datetime | None
    lease_owner: str | None
    lease_until: datetime | None
    consent_snapshot_json: dict[str, Any] | None
    source_revision_json: dict[str, Any] | None
    payload_summary: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    result_ref: str | None
    created_at: datetime | None
    updated_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> AiTaskRecord:
        return cls(
            id=int(row["id"]),
            task_id=str(row["task_id"]),
            owner_user_id=int(row["owner_user_id"]),
            task_type=str(row["task_type"]),
            scene=str(row["scene"]),
            idempotency_key=str(row["idempotency_key"]),
            request_digest=str(row["request_digest"]) if row.get("request_digest") else None,
            status=AiTaskStatus(row["status"]),
            stage=str(row["stage"]) if row.get("stage") else None,
            attempt_count=int(row["attempt_count"] or 0),
            max_attempts=int(row["max_attempts"] or 0),
            next_run_at=row["next_run_at"],
            lease_owner=str(row["lease_owner"]) if row.get("lease_owner") else None,
            lease_until=row["lease_until"],
            consent_snapshot_json=_maybe_json(row.get("consent_snapshot_json")),
            source_revision_json=_maybe_json(row.get("source_revision_json")),
            payload_summary=_maybe_json(row.get("payload_summary")),
            error_code=str(row["error_code"]) if row.get("error_code") else None,
            error_message=str(row["error_message"]) if row.get("error_message") else None,
            result_ref=str(row["result_ref"]) if row.get("result_ref") else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------

def _now_utc() -> datetime:
    """Naive UTC ``datetime`` suitable for MySQL DATETIME columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _maybe_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _revision_dict(revisions: Any) -> dict[str, int]:
    if isinstance(revisions, RevisionVector):
        return revisions.as_dict()
    if isinstance(revisions, dict):
        return {str(key): int(value) for key, value in revisions.items()}
    return {}


def _consent_dict(consent: Any) -> dict[str, Any] | None:
    if consent is None:
        return None
    if isinstance(consent, dict):
        return dict(consent)
    if hasattr(consent, "model_dump"):
        return consent.model_dump()
    return dict(consent)


def _safe_error_message(code: str) -> str:
    return _SAFE_ERROR_MESSAGES.get(code, _DEFAULT_SAFE_ERROR_MESSAGE)


def _owner_tombstone_value(task: AiTaskRecord) -> str | None:
    if task.owner_user_id is None:
        return None
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"ai-task-owner:{task.owner_user_id}:{task.task_id}"
    ).hex


async def _first_row(result: Any) -> dict[str, Any] | None:
    return result.mappings().first()


async def _get_by_id(
    db: AsyncSession, task_id: str, *, for_update: bool = False
) -> AiTaskRecord | None:
    lock = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_SELECT_COLUMNS} FROM ai_task "
            f"WHERE task_id = :task_id{lock}"
        ),
        {"task_id": task_id},
    )
    row = await _first_row(result)
    return AiTaskRecord.from_row(row) if row else None


async def _find_by_idempotency(
    db: AsyncSession,
    owner_user_id: int,
    task_type: str,
    idempotency_key: str,
    *,
    for_update: bool = False,
) -> AiTaskRecord | None:
    lock = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_SELECT_COLUMNS} FROM ai_task "
            "WHERE owner_user_id = :owner_user_id AND task_type = :task_type "
            "AND idempotency_key = :idempotency_key "
            f"LIMIT 1{lock}"
        ),
        {
            "owner_user_id": owner_user_id,
            "task_type": task_type,
            "idempotency_key": idempotency_key,
        },
    )
    row = await _first_row(result)
    return AiTaskRecord.from_row(row) if row else None


def _revisions_changed(stored: dict[str, Any] | None, current: Any) -> bool:
    """True when the completion-time vector no longer matches the enqueue one.

    ``current=None`` means the caller did not supply a comparison vector, so
    nothing is deemed changed (backwards compatible with callers that only care
    about cancel flags).
    """
    if current is None:
        return False
    return (stored or {}) != _revision_dict(current)


_TASK_FEATURES = {
    "profile_extract": AiFeature.PROFILE,
    "profile_projection": AiFeature.PROFILE,
    "search_parse": AiFeature.SEARCH,
    "search_execute": AiFeature.SEARCH,
    "compatibility": AiFeature.COMPATIBILITY_SHADOW,
}


async def _load_current_completion_context(
    db: AsyncSession, task: AiTaskRecord
) -> tuple[bool, bool, RevisionVector]:
    """Re-read release gate, active consent and revisions before writing output."""
    feature = _TASK_FEATURES.get(task.task_type)
    feature_enabled = True
    if feature is not None:
        try:
            require_ai_feature(feature, settings)
        except AiFeatureDisabledError:
            feature_enabled = False

    result = await db.execute(
        text(
            "SELECT profile_revision, preference_revision, privacy_revision, "
            "relationship_revision, policy_revision "
            "FROM user_revision_state WHERE user_id = :user_id"
        ),
        {"user_id": task.owner_user_id},
    )
    row = await _first_row(result)
    current_revision = RevisionVector(
        profile=int(row["profile_revision"] or 0) if row else 0,
        preference=int(row["preference_revision"] or 0) if row else 0,
        privacy=int(row["privacy_revision"] or 0) if row else 0,
        relationship=int(row["relationship_revision"] or 0) if row else 0,
        policy=int(row["policy_revision"] or 0) if row else 0,
    )

    consent_matches = True
    snapshot = task.consent_snapshot_json or {}
    scope = str(snapshot.get("scope") or "")
    if scope:
        consent_result = await db.execute(
            text(
                "SELECT version, policy_revision, granted_at "
                "FROM ai_consent_grant "
                "WHERE user_id = :user_id AND scope = :scope AND revoked_at IS NULL "
                "ORDER BY granted_at DESC LIMIT 1"
            ),
            {"user_id": task.owner_user_id, "scope": scope},
        )
        current_consent = await _first_row(consent_result)
        consent_matches = current_consent is not None and str(
            current_consent["version"]
        ) == str(snapshot.get("version") or "")
        if current_consent is not None and snapshot.get("policy_revision"):
            consent_matches = consent_matches and str(
                current_consent["policy_revision"]
            ) == str(snapshot["policy_revision"])
    return feature_enabled, consent_matches, current_revision


async def _supersede(db: AsyncSession, task: AiTaskRecord, now: datetime) -> AiTaskRecord:
    assert_transition(task.status, AiTaskStatus.SUPERSEDED)
    await db.execute(
        text(
            "UPDATE ai_task SET status = 'superseded', finished_at = :now, "
            "lease_owner = NULL, lease_until = NULL, "
            "owner_tombstone = COALESCE(owner_tombstone, :owner_tombstone), "
            "consent_snapshot_json = NULL, source_revision_json = NULL, "
            "payload_summary = NULL, result_ref = NULL, "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id"
        ),
        {
            "now": now,
            "task_id": task.task_id,
            "owner_tombstone": _owner_tombstone_value(task),
        },
    )
    updated = await _get_by_id(db, task.task_id)
    assert updated is not None
    return updated


async def _supersede_guarded(
    db: AsyncSession, task: AiTaskRecord
) -> AiTaskRecord:
    """Move ``task`` to ``superseded`` inside a savepoint when the driver supports it.

    The completion gate (consent / version re-check) runs unconditionally before
    this; the savepoint only protects the supersede sub-write so a failure here
    can roll back the inner write without erasing the caller's draft/outbox work.
    On drivers without ``begin_nested`` the write still happens — the gate must
    not fail open (缺陷 2).
    """
    if hasattr(db, "begin_nested"):
        async with db.begin_nested():
            return await _supersede(db, task, now=_now_utc())
    return await _supersede(db, task, now=_now_utc())


# ----------------------------------------------------------------------
# 对外接口
# ----------------------------------------------------------------------

async def enqueue_task(
    db: AsyncSession,
    owner_user_id: int,
    task_type: str,
    idempotency_key: str,
    request_hash: str,
    revisions: Any = None,
    consent: Any = None,
) -> AiTaskRecord:
    """Create a queued task, replaying the existing one on the same key.

    Idempotency follows the ``reserve_or_replay`` experience: same
    (user, task_type, key) with the same request digest returns the first task,
    a different digest raises ``409 TASK_IDEMPOTENCY_CONFLICT``.  The insert
    races are absorbed by the unique key ``uk_ai_task_owner_type_key`` and an
    ``IntegrityError`` re-read.  The function never commits.
    """
    revision_dict = _revision_dict(revisions)
    consent_dict = _consent_dict(consent)
    existing = await _find_by_idempotency(db, owner_user_id, task_type, idempotency_key)
    if existing is not None:
        if existing.request_digest != request_hash:
            raise TaskError(
                code="TASK_IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key 已用于不同请求内容",
                status_code=409,
            )
        return existing

    task_id = uuid.uuid4().hex
    scene = str(consent_dict.get("scope", task_type)) if consent_dict else task_type
    try:
        insert_statement = text(
            "INSERT INTO ai_task "
            "(task_id, owner_user_id, task_type, scene, idempotency_key, "
            " request_digest, status, attempt_count, max_attempts, next_run_at, "
            " consent_snapshot_json, source_revision_json, payload_summary, "
            " created_at, updated_at) "
            "VALUES (:task_id, :owner_user_id, :task_type, :scene, "
            " :idempotency_key, :request_digest, 'queued', 0, :max_attempts, "
            " NULL, :consent_snapshot_json, :source_revision_json, "
            " NULL, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        )
        insert_params = {
            "task_id": task_id,
            "owner_user_id": owner_user_id,
            "task_type": task_type,
            "scene": scene,
            "idempotency_key": idempotency_key,
            "request_digest": request_hash,
            "max_attempts": settings.ai_max_attempts,
            "consent_snapshot_json": (
                json.dumps(consent_dict, ensure_ascii=False) if consent_dict else None
            ),
            "source_revision_json": json.dumps(revision_dict, ensure_ascii=False),
        }
        # A duplicate-key race must roll back only this INSERT. Rolling back the
        # caller's transaction can erase an unrelated draft/outbox write.
        if hasattr(db, "begin_nested"):
            async with db.begin_nested():
                await db.execute(insert_statement, insert_params)
        else:
            await db.execute(insert_statement, insert_params)
    except IntegrityError:
        # A concurrent enqueue with the same key won the race.
        # The caller may already have established a REPEATABLE READ snapshot
        # while checking visibility. A locking read is a MySQL current read,
        # so it observes the committed winner that caused this duplicate-key
        # error instead of re-raising the race.
        existing = await _find_by_idempotency(
            db,
            owner_user_id,
            task_type,
            idempotency_key,
            for_update=True,
        )
        if existing is None:
            raise
        if existing.request_digest != request_hash:
            raise TaskError(
                code="TASK_IDEMPOTENCY_CONFLICT",
                message="Idempotency-Key 已用于不同请求内容",
                status_code=409,
            ) from None
        return existing

    created = await _get_by_id(db, task_id)
    assert created is not None
    return created


async def get_task(db: AsyncSession, task_id: str) -> AiTaskRecord | None:
    """Return one task row without raising (route ownership checks on it)."""
    return await _get_by_id(db, task_id)


async def claim_tasks(
    db: AsyncSession, worker_id: str, now: datetime, limit: int
) -> list[AiTaskRecord]:
    """Lease due ``queued/retry_wait`` rows for one worker.

    One locking ``SELECT ... FOR UPDATE SKIP LOCKED`` picks the oldest eligible
    rows — status in ``queued/retry_wait``, ``next_run_at <= now`` and no live
    lease — so two workers can never claim the same row (rows another
    transaction already locked are skipped, and once a lease is committed the
    ``lease_until`` predicate excludes the row until it expires).  The lease
    write is a per-row single-table ``UPDATE``; multi-table ``UPDATE ... ORDER
    BY ... LIMIT`` is illegal in MySQL (ERROR 1221), so ordering/limiting live
    in the SELECT, exactly like Task 4's outbox claim.  The function never
    commits; the caller's transaction owns durability.
    """
    lease_until = now + timedelta(seconds=settings.ai_lease_seconds)
    result = await db.execute(
        text(
            f"SELECT {_SELECT_COLUMNS} FROM ai_task "
            "WHERE status IN ('queued', 'retry_wait') "
            "AND (next_run_at IS NULL OR next_run_at <= :now) "
            "AND (lease_owner IS NULL OR lease_until IS NULL OR lease_until < :now) "
            "ORDER BY created_at ASC "
            "LIMIT :limit "
            "FOR UPDATE SKIP LOCKED"
        ),
        {"now": now, "limit": limit},
    )
    rows = result.mappings().all()
    for row in rows:
        await db.execute(
            text(
                "UPDATE ai_task SET status = 'leased', "
                "lease_owner = :worker_id, lease_until = :lease_until, "
                "updated_at = UTC_TIMESTAMP() "
                "WHERE task_id = :task_id"
            ),
            {
                "worker_id": worker_id,
                "lease_until": lease_until,
                "task_id": row["task_id"],
            },
        )
    return [AiTaskRecord.from_row(row) for row in rows]


async def start_task(
    db: AsyncSession, task_id: str, worker_id: str
) -> AiTaskRecord:
    """Move a task ``leased -> running`` after validating lease ownership."""
    task = await _get_by_id(db, task_id, for_update=True)
    if task is None:
        raise TaskError(code="TASK_NOT_FOUND", message="任务不存在", status_code=404)
    if task.status is not AiTaskStatus.LEASED:
        raise TaskError(
            code="TASK_NOT_FOUND", message="任务不在可启动状态", status_code=404
        )
    if task.lease_owner != worker_id:
        raise TaskError(
            code="TASK_NOT_FOUND",
            message="任务租约不属于当前 Worker",
            status_code=404,
        )
    assert_transition(task.status, AiTaskStatus.RUNNING)
    now = _now_utc()
    result = await db.execute(
        text(
            "UPDATE ai_task SET status = 'running', "
            "started_at = COALESCE(started_at, :now), updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id AND status = 'leased' AND lease_owner = :worker_id"
        ),
        {"now": now, "task_id": task_id, "worker_id": worker_id},
    )
    if result.rowcount != 1:
        raise TaskError(
            code="TASK_NOT_FOUND",
            message="任务启动失败：状态或租约已变更",
            status_code=404,
        )
    updated = await _get_by_id(db, task_id)
    assert updated is not None
    return updated


async def heartbeat_lease(
    db: AsyncSession, task_id: str, worker_id: str, now: datetime
) -> None:
    """Renew the lease of a ``running/leased`` task owned by the worker."""
    lease_until = now + timedelta(seconds=settings.ai_lease_seconds)
    await db.execute(
        text(
            "UPDATE ai_task SET lease_until = :lease_until, "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id AND status IN ('running', 'leased') "
            "AND lease_owner = :worker_id"
        ),
        {
            "lease_until": lease_until,
            "task_id": task_id,
            "worker_id": worker_id,
        },
    )


async def request_cancel(
    db: AsyncSession, task_id: str, owner_user_id: int
) -> AiTaskRecord:
    """Cancel a task inside its cancellable window, owner-scoped.

    Missing or foreign tasks both surface ``404 TASK_NOT_FOUND`` so ownership
    is never leaked; already-terminal tasks surface ``409
    TASK_NOT_CANCELLABLE``.
    """
    task = await _get_by_id(db, task_id)
    if task is None or task.owner_user_id != owner_user_id:
        raise TaskError(code="TASK_NOT_FOUND", message="任务不存在", status_code=404)
    if task.status not in _CANCELLABLE:
        raise TaskError(
            code="TASK_NOT_CANCELLABLE",
            message="任务已处于不可取消状态",
            status_code=409,
        )
    assert_transition(task.status, AiTaskStatus.CANCELLED)
    now = _now_utc()
    await db.execute(
        text(
            "UPDATE ai_task SET status = 'cancelled', finished_at = :now, "
            "owner_tombstone = COALESCE(owner_tombstone, :owner_tombstone), "
            "consent_snapshot_json = NULL, source_revision_json = NULL, "
            "payload_summary = NULL, result_ref = NULL, "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id AND owner_user_id = :owner_user_id "
            "AND status IN ('queued', 'leased', 'running', 'retry_wait')"
        ),
        {
            "now": now,
            "task_id": task_id,
            "owner_user_id": owner_user_id,
            "owner_tombstone": uuid.uuid5(
                uuid.NAMESPACE_URL, f"ai-task-owner:{owner_user_id}:{task_id}"
            ).hex,
        },
    )
    updated = await _get_by_id(db, task_id)
    assert updated is not None
    if updated.status is not AiTaskStatus.CANCELLED:
        # 状态在读取与写入之间被并发推进（如刚好完成），按不可取消处理。
        raise TaskError(
            code="TASK_NOT_CANCELLABLE",
            message="任务状态已变更，无法取消",
            status_code=409,
        )
    return updated


def build_task_owner_tombstone(owner_user_id: int | None, task_id: str) -> str | None:
    if owner_user_id is None:
        return None
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"ai-task-owner:{int(owner_user_id)}:{task_id}"
    ).hex


async def tombstone_owner_tasks(
    db: AsyncSession,
    owner_user_id: int,
    *,
    task_type: str | None = None,
) -> None:
    """Tombstone terminal owners and clear any retained task resource refs."""
    filters = ["owner_user_id = :owner_user_id", "finished_at IS NOT NULL"]
    params: dict[str, Any] = {"owner_user_id": owner_user_id}
    if task_type:
        filters.append("task_type = :task_type")
        params["task_type"] = task_type
    await db.execute(
        text(
            "UPDATE ai_task SET owner_tombstone = COALESCE(owner_tombstone, SHA2(CONCAT('ai-task-owner:', owner_user_id, ':', task_id), 256)), "
            "consent_snapshot_json = NULL, source_revision_json = NULL, "
            "payload_summary = NULL, result_ref = NULL "
            f"WHERE {' AND '.join(filters)}"
        ),
        params,
    )


async def complete_task(
    db: AsyncSession,
    task_id: str,
    worker_id: str,
    result_ref: str,
    revisions: Any = None,
) -> AiTaskRecord:
    """Write a result only when the task is still owned, uncancelled and current.

    The task, the cancel flag and the source revision vector are re-read under
    a row lock before the result is committed.  Version invalidation funnels a
    ``running`` task into ``superseded`` instead of overwriting newer state.
    """
    task = await _get_by_id(db, task_id, for_update=True)
    if task is None:
        raise TaskError(code="TASK_NOT_FOUND", message="任务不存在", status_code=404)
    if task.status in _TERMINAL:
        return task
    if task.status not in {AiTaskStatus.RUNNING, AiTaskStatus.LEASED}:
        return task
    if task.lease_owner is not None and task.lease_owner != worker_id:
        # Worker 已失去租约；结果交由新持有者处理，不覆盖。
        return task
    # 安全复查（consent 复查、版本向量对比）必须无条件执行：不能依赖
    # 驱动是否支持嵌套事务（savepoint），否则不支持 begin_nested 的驱动
    # 会完全跳过完成门禁，导致旧版本/已撤回 consent 的结果覆盖新状态。
    # 只有需要回滚保护的子写（supersede）才在 savepoint 内执行；安全
    # 复查本身是只读 SELECT，不依赖 savepoint。
    feature_enabled, consent_matches, current_revision = (
        await _load_current_completion_context(db, task)
    )
    if not feature_enabled or not consent_matches:
        return await _supersede_guarded(db, task)
    if task.source_revision_json and (
        task.source_revision_json != current_revision.as_dict()
    ):
        return await _supersede_guarded(db, task)
    if revisions is not None and _revision_dict(revisions) != current_revision.as_dict():
        return await _supersede_guarded(db, task)
    if _revisions_changed(task.source_revision_json, revisions):
        if task.status is AiTaskStatus.RUNNING:
            return await _supersede_guarded(db, task)
        return task
    if task.status is not AiTaskStatus.RUNNING:
        # 未启动的任务不能直接完成（leased -> succeeded 非法）。
        return task
    assert_transition(task.status, AiTaskStatus.SUCCEEDED)
    now = _now_utc()
    await db.execute(
        text(
            "UPDATE ai_task SET status = 'succeeded', result_ref = :result_ref, "
            "finished_at = :now, error_code = NULL, error_message = NULL, "
            "owner_tombstone = COALESCE(owner_tombstone, :owner_tombstone), "
            "consent_snapshot_json = NULL, source_revision_json = NULL, "
            "payload_summary = NULL, "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id"
        ),
        {
            "result_ref": result_ref,
            "now": now,
            "task_id": task_id,
            "owner_tombstone": _owner_tombstone_value(task),
        },
    )
    updated = await _get_by_id(db, task_id)
    assert updated is not None
    return updated


async def fail_task(
    db: AsyncSession,
    task_id: str,
    worker_id: str,
    error_code: str,
    retryable: bool,
) -> AiTaskRecord:
    """Record a failure: retryable -> ``retry_wait`` (attempt+backoff), else ``failed``.

    Retryable classification follows the Task 5 provider semantics: timeout,
    provider 429 and transient 5xx are retryable; schema violation, policy
    denial, consent revocation, missing resource and version conflict are not.
    Only a ``running`` task may move to ``failed`` (``leased -> failed`` is an
    illegal transition), so an unstarted task stays leased and is reclaimed by
    the reaper instead.
    """
    task = await _get_by_id(db, task_id, for_update=True)
    if task is None:
        raise TaskError(code="TASK_NOT_FOUND", message="任务不存在", status_code=404)
    if task.status in _TERMINAL:
        return task
    if task.status not in {AiTaskStatus.RUNNING, AiTaskStatus.LEASED}:
        return task
    if task.lease_owner is not None and task.lease_owner != worker_id:
        return task
    if retryable and task.status in {AiTaskStatus.RUNNING, AiTaskStatus.LEASED}:
        next_attempt = task.attempt_count + 1
        if next_attempt <= task.max_attempts or task.status is AiTaskStatus.LEASED:
            # leased 无法直接 failed（非法转换），耗尽重试时仍回 retry_wait。
            backoff = min(
                RETRY_BACKOFF_CAP_SECONDS,
                RETRY_BACKOFF_BASE_SECONDS * (2 ** (next_attempt - 1)),
            )
            assert_transition(task.status, AiTaskStatus.RETRY_WAIT)
            now = _now_utc()
            await db.execute(
                text(
                    "UPDATE ai_task SET status = 'retry_wait', "
                    "attempt_count = :attempt_count, next_run_at = :next_run_at, "
                    "error_code = :error_code, error_message = :error_message, "
                    "lease_owner = NULL, lease_until = NULL, updated_at = UTC_TIMESTAMP() "
                    "WHERE task_id = :task_id"
                ),
                {
                    "attempt_count": next_attempt,
                    "next_run_at": now + timedelta(seconds=backoff),
                    "error_code": error_code,
                    "error_message": _safe_error_message(error_code),
                    "task_id": task_id,
                },
            )
            updated = await _get_by_id(db, task_id)
            assert updated is not None
            return updated
        # 重试次数耗尽 → 仅 running 可进入终态 failed。
        return await _fail_terminal(db, task, error_code)
    if task.status is AiTaskStatus.RUNNING:
        return await _fail_terminal(db, task, error_code)
    return task


async def _fail_terminal(
    db: AsyncSession, task: AiTaskRecord, error_code: str
) -> AiTaskRecord:
    assert_transition(task.status, AiTaskStatus.FAILED)
    now = _now_utc()
    await db.execute(
        text(
            "UPDATE ai_task SET status = 'failed', error_code = :error_code, "
            "error_message = :error_message, finished_at = :now, "
            "lease_owner = NULL, lease_until = NULL, "
            "owner_tombstone = COALESCE(owner_tombstone, :owner_tombstone), "
            "consent_snapshot_json = NULL, source_revision_json = NULL, "
            "payload_summary = NULL, result_ref = NULL, "
            "updated_at = UTC_TIMESTAMP() "
            "WHERE task_id = :task_id"
        ),
        {
            "error_code": error_code,
            "error_message": _safe_error_message(error_code),
            "now": now,
            "task_id": task.task_id,
            "owner_tombstone": _owner_tombstone_value(task),
        },
    )
    updated = await _get_by_id(db, task.task_id)
    assert updated is not None
    return updated


# 租约丢失超限时写回的稳定错误码。复用冻结码 AI_TEMPORARILY_UNAVAILABLE
# （AI 服务持续无法在租约内完成任务），但落库为终态 failed、不再重试，因此
# 不构成重试风暴。AiErrorCode 是跨任务冻结的 14 码，不得新增。
_LEASE_LOST_ERROR_CODE = "AI_TEMPORARILY_UNAVAILABLE"


async def reap_expired_leases(
    db: AsyncSession, now: datetime, limit: int
) -> list[str]:
    """Recover ``leased/running`` tasks whose lease has expired.

    Expired tasks go back to ``retry_wait`` so a live worker can claim them
    again, and each recovery advances ``attempt_count`` so a crashing worker
    cannot loop ``running -> retry_wait -> claimed`` forever (review finding
    I-3): once the counter would exceed ``max_attempts`` the recovery is
    terminal — a ``running`` task moves to ``failed`` (``running -> failed``
    is a legal transition).  A ``leased`` task at the cap is also moved
    directly to ``failed`` via a special reaper path: ``leased -> failed`` is
    illegal in the state machine, but the reaper is a recovery mechanism whose
    UPDATE bypasses ``assert_transition``; without this, a task stuck in the
    ``retry_wait <-> leased`` cycle (worker claims but never starts) would
    never reach a terminal state (缺陷 34).  ``lease_owner``/``lease_until``
    are always cleared.  The recovery is idempotent and safe under concurrent
    workers thanks to ``FOR UPDATE SKIP LOCKED``.
    """
    result = await db.execute(
        text(
            "SELECT task_id, status, attempt_count, max_attempts FROM ai_task "
            "WHERE status IN ('leased', 'running') "
            "AND lease_until IS NOT NULL AND lease_until < :now "
            "ORDER BY lease_until ASC "
            "LIMIT :limit "
            "FOR UPDATE SKIP LOCKED"
        ),
        {"now": now, "limit": limit},
    )
    rows = result.mappings().all()
    recovered: list[str] = []
    for row in rows:
        attempt_count = int(row["attempt_count"] or 0)
        max_attempts = int(row["max_attempts"] or 0)
        next_attempt = attempt_count + 1
        if next_attempt > max_attempts:
            if row["status"] == "running":
                await db.execute(
                    text(
                        "UPDATE ai_task SET status = 'failed', "
                        "error_code = :error_code, "
                        "error_message = :error_message, finished_at = :now, "
                        "lease_owner = NULL, lease_until = NULL, "
                        "updated_at = UTC_TIMESTAMP() "
                        "WHERE task_id = :task_id"
                    ),
                    {
                        "error_code": _LEASE_LOST_ERROR_CODE,
                        "error_message": _safe_error_message(_LEASE_LOST_ERROR_CODE),
                        "now": now,
                        "task_id": row["task_id"],
                    },
                )
            else:
                # leased 状态 attempt 已超限：``leased -> failed`` 在状态机中
                # 非法，正常路径需经 leased -> retry_wait -> leased -> running
                # -> failed 才能终态。但如果 worker 持续 claim 但从不
                # ``start_task``（崩溃或租约反复过期），任务会永远卡在
                # ``retry_wait <-> leased`` 循环中无法终态（缺陷 34）。
                # reaper 是恢复机制，其 UPDATE 不经 ``assert_transition``，
                # 因此这里作为特殊路径直接终态 ``failed`` 并告警，打破死循环。
                logger.warning(
                    "ai_task_reap_leased_at_attempts_cap task_id=%s "
                    "attempt_count=%d max_attempts=%d",
                    row["task_id"],
                    attempt_count,
                    max_attempts,
                )
                await db.execute(
                    text(
                        "UPDATE ai_task SET status = 'failed', "
                        "error_code = :error_code, "
                        "error_message = :error_message, finished_at = :now, "
                        "lease_owner = NULL, lease_until = NULL, "
                        "updated_at = UTC_TIMESTAMP() "
                        "WHERE task_id = :task_id"
                    ),
                    {
                        "error_code": _LEASE_LOST_ERROR_CODE,
                        "error_message": _safe_error_message(_LEASE_LOST_ERROR_CODE),
                        "now": now,
                        "task_id": row["task_id"],
                    },
                )
        else:
            await db.execute(
                text(
                    "UPDATE ai_task SET status = 'retry_wait', "
                    "attempt_count = :attempt_count, next_run_at = :now, "
                    "lease_owner = NULL, lease_until = NULL, "
                    "updated_at = UTC_TIMESTAMP() "
                    "WHERE task_id = :task_id"
                ),
                {
                    "attempt_count": next_attempt,
                    "now": now,
                    "task_id": row["task_id"],
                },
            )
        recovered.append(str(row["task_id"]))
    return recovered
