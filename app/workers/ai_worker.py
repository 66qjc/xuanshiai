"""AI worker: claim ``ai_task`` leases, run registered handlers, reap stale leases.

Run ``python -m app.workers.ai_worker --once --dry-run`` for a safe summary:

    claimed=0 completed=0 failed=0

``--dry-run`` never opens a database session and never writes data; ``--once``
runs a single real round.  Business handlers are registered in
:data:`TASK_HANDLERS` keyed by ``ai_task.task_type``; :func:`register_business_handlers`
runs at import time so a standalone ``python -m app.workers.ai_worker`` process
can dispatch every business task type (``profile_extract`` / ``search_parse`` /
``search_execute`` / ``compatibility`` / ``profile_projection`` / ``cleanup``).
A handler has the signature ``async def handler(db, task, worker_id) ->
(result_ref, revisions) | None`` — returning ``None`` records a retryable
failure, returning a tuple completes the task after a version re-check in
:func:`app.services.ai.tasks.complete_task`.

``--consumers`` switches the process to the derivation-outbox cleanup consumer
loop (:func:`app.services.derivation_outbox.run_cleanup_consumer_round`), which
propagates deletes/withdrawals asynchronously (projection invalidation + stale
marking of derived search/compat results).  ``--consumers --once`` runs a single
round; ``--consumers --dry-run`` prints ``claimed=0 applied=0 superseded=0
duplicate=0 skipped=0`` without touching the database.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.db.session import session_factory
from app.schemas.ai_common import AiTaskStatus
from app.services.ai.audit import emit_ai_metric
from app.services.ai.profile import extract_profile_turn
from app.services.ai.journey import extract_journey_candidates
from app.services.ai.tasks import (
    AiTaskRecord,
    claim_tasks,
    complete_task,
    fail_task,
    heartbeat_lease,
    reap_expired_leases,
    start_task,
)

logger = logging.getLogger(__name__)

# Task 7 注册 profile_extract 业务 handler；其余业务 handler（search_parse/
# search_execute/compatibility/profile_projection/cleanup）由文件底部的
# register_business_handlers() 显式注册。key = ai_task.task_type。
TASK_HANDLERS: dict[str, Callable[..., Awaitable[Any]]] = {
    "profile_extract": extract_profile_turn,
    "moxiang_candidate_extract": extract_journey_candidates,
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _worker_id() -> str:
    return f"worker-{socket.gethostname()}-{os.getpid()}"


def _heartbeat_interval() -> float:
    """Lease renewal cadence: half the lease but never slower than 15s.

    A long handler (LLM batch extraction etc.) must renew its own lease, or a
    live reaper would reclaim the task while the handler is still running and
    the task would be executed twice (review finding I-1).  The cadence keeps
    ``lease_until`` comfortably ahead of expiry even for pathological small
    lease settings.
    """
    return min(max(settings.ai_lease_seconds / 2, 1.0), 15.0)


async def _run_with_heartbeat(
    handler: Callable[..., Awaitable[Any]],
    task: AiTaskRecord,
    worker_id: str,
    *,
    db: Any = None,
    session_provider: Any = None,
) -> Any:
    """Await ``handler`` while renewing the task lease every interval.

    The handler runs as a child task; while it is not done we wake up every
    ``_heartbeat_interval()`` seconds and call :func:`heartbeat_lease`, which
    extends ``lease_until`` to ``now + lease_seconds`` but only for rows the
    worker still owns (``lease_owner`` guard), so a reclaimed task is never
    touched.  A failed heartbeat is logged and ignored — the handler is never
    aborted by a transient DB blip; if the lease is genuinely lost the reaper
    still recovers the task (fallback, not the happy path).  On cancellation
    the child handler task is cancelled too.

    When ``session_provider`` is set, the handler runs in its own session and
    an inner savepoint protects its business writes until completion's gate has
    run.  The result is a ``(outcome, finalize_handler)`` pair where
    ``finalize_handler`` accepts either no completion operation (rollback /
    discard) or an async operation receiving ``handler_db``.  Completion and
    handler writes then share one outer commit.  When the gate supersedes the
    task, the nested savepoint is rolled back before the terminal task update
    is written, so consent revocation cannot leak business writes (AI-P0-07).
    When ``session_provider`` is ``None`` the result is simply ``outcome``.
    """
    async def invoke_handler() -> Any:
        if session_provider is None:
            return await handler(db, task, worker_id)
        handler_db_cm = session_provider()
        handler_db = await handler_db_cm.__aenter__()
        if hasattr(handler_db, "begin_nested"):
            handler_nested = handler_db.begin_nested()
            await handler_nested.start()
        else:
            # Test doubles and legacy session adapters may not expose
            # SQLAlchemy's savepoint API.  Rolling back the whole session is
            # the safe fallback; supersede then writes the terminal task state
            # in a fresh transaction before the outer commit.
            class _FallbackNested:
                is_active = True

                async def start(self) -> "_FallbackNested":
                    return self

                async def rollback(self) -> None:
                    if not self.is_active:
                        return
                    await handler_db.rollback()
                    self.is_active = False

                async def commit(self) -> None:
                    self.is_active = False
                    return None

            handler_nested = _FallbackNested()
        finalized = False
        handler_writes_discarded = False
        supersede_update_pending = False

        async def rollback_handler_writes() -> None:
            nonlocal handler_writes_discarded
            if handler_writes_discarded:
                return
            handler_writes_discarded = True
            if getattr(handler_nested, "is_active", True):
                await handler_nested.rollback()

        async def rollback_before_supersede() -> None:
            nonlocal supersede_update_pending
            supersede_update_pending = True
            await rollback_handler_writes()

        async def finalize_handler(
            completion_operation: Callable[[Any], Awaitable[Any]] | None = None,
        ) -> Any:
            nonlocal finalized
            if finalized:
                return None
            finalized = True
            try:
                if completion_operation is None:
                    await rollback_handler_writes()
                    await handler_db.rollback()
                    return None
                completed = await completion_operation(handler_db)
                status = getattr(completed, "status", None)
                if (
                    status is AiTaskStatus.SUCCEEDED
                    and not handler_writes_discarded
                    and getattr(handler_nested, "is_active", True)
                ):
                    await handler_nested.commit()
                    await handler_db.commit()
                elif status is AiTaskStatus.SUPERSEDED and supersede_update_pending:
                    # ``complete_task`` invokes before_supersede before its
                    # guarded terminal update, leaving only that update in the
                    # outer transaction for this commit.
                    await handler_db.commit()
                else:
                    await rollback_handler_writes()
                    await handler_db.rollback()
                return completed
            except BaseException:
                await handler_db.rollback()
                raise
            finally:
                await handler_db_cm.__aexit__(None, None, None)

        # ``_process`` builds the completion operation after receiving this
        # closure.  Keep the savepoint callback on the closure so that the
        # operation can pass it to ``complete_task`` without exposing the
        # transaction object in the public handler result tuple.
        setattr(finalize_handler, "before_supersede", rollback_before_supersede)
        setattr(finalize_handler, "before_not_applied", rollback_handler_writes)

        try:
            outcome = await handler(handler_db, task, worker_id)
            return outcome, finalize_handler
        except BaseException:
            await finalize_handler()
            raise

    async def renew_lease() -> None:
        if session_provider is None:
            await heartbeat_lease(db, task.task_id, worker_id, _now())
            return
        async with session_provider() as heartbeat_db:
            await heartbeat_lease(heartbeat_db, task.task_id, worker_id, _now())
            await heartbeat_db.commit()

    interval = _heartbeat_interval()
    handler_task = asyncio.create_task(invoke_handler())
    try:
        while True:
            done, _ = await asyncio.wait({handler_task}, timeout=interval)
            if handler_task in done:
                return handler_task.result()
            try:
                await renew_lease()
            except Exception:
                logger.warning(
                    "ai_worker_heartbeat_failed task_id=%s",
                    task.task_id,
                    exc_info=True,
                )
    except asyncio.CancelledError:
        handler_task.cancel()
        raise


async def _process(
    db: Any, task: AiTaskRecord, worker_id: str, *, session_provider: Any = None
) -> str:
    """Run one claimed task; returns ``completed``/``failed``/``skipped``."""
    async def start_in_session() -> AiTaskRecord:
        if session_provider is None:
            started = await start_task(db, task.task_id, worker_id)
            return started
        async with session_provider() as lifecycle_db:
            try:
                started = await start_task(lifecycle_db, task.task_id, worker_id)
                await lifecycle_db.commit()
                return started
            except Exception:
                await lifecycle_db.rollback()
                raise

    async def finish_in_session(operation: Callable[[Any], Awaitable[Any]]) -> Any:
        if session_provider is None:
            return await operation(db)
        async with session_provider() as finalize_db:
            try:
                result = await operation(finalize_db)
                await finalize_db.commit()
                return result
            except Exception:
                await finalize_db.rollback()
                raise

    try:
        started = await start_in_session()
    except Exception:
        logger.exception("ai_worker_start_failed task_id=%s", task.task_id)
        return "skipped"
    handler = TASK_HANDLERS.get(started.task_type)
    if handler is None:
        # 任务类型未注册业务 handler：服务端配置缺失，重试不会改善，直接终态
        # failed（复用冻结码 AI_FEATURE_DISABLED，retryable=false），否则任务
        # 会卡在 running 并被 reaper 无界地反复回收（review finding I-2）。
        logger.warning(
            "ai_worker_no_handler task_type=%s task_id=%s",
            started.task_type,
            started.task_id,
        )
        try:
            await finish_in_session(
                lambda finalize_db: fail_task(
                    finalize_db,
                    started.task_id,
                    worker_id,
                    error_code="AI_FEATURE_DISABLED",
                    retryable=False,
                )
            )
        except Exception:
            logger.exception(
                "ai_worker_no_handler_fail_failed task_id=%s", started.task_id
            )
        return "failed"
    try:
        heartbeat_result = await _run_with_heartbeat(
            handler,
            started,
            worker_id,
            db=db,
            session_provider=session_provider,
        )
    except Exception as exc:  # noqa: BLE001 - boundary conversion
        emit_ai_metric("provider_5xx", 1, {"task_type": started.task_type})
        logger.warning(
            "ai_worker_exec_failed task_id=%s error=%s",
            started.task_id,
            type(exc).__name__,
        )
        await finish_in_session(
            lambda finalize_db: fail_task(
                finalize_db,
                started.task_id,
                worker_id,
                error_code="AI_TEMPORARILY_UNAVAILABLE",
                retryable=True,
            )
        )
        emit_ai_metric("retry_rate", 1, {"task_type": started.task_type})
        return "failed"
    # _run_with_heartbeat returns either a bare outcome (no session_provider)
    # or a (outcome, finalize_handler) pair.  The handler finalizer owns the
    # completion operation, savepoint release/rollback and exactly one outer
    # commit.  finalize_handler is None when no session_provider is used.
    if session_provider is None:
        outcome = heartbeat_result
        finalize_handler = None
    else:
        outcome, finalize_handler = heartbeat_result
    if outcome is None:
        # Handler returned None (e.g. provider failure); its in-session writes
        # (if any, e.g. an inner fail_task) must not be persisted — _process
        # re-records the failure via a separate finalize_db, and the handler's
        # business writes are discarded.
        if finalize_handler is not None:
            await finalize_handler()
        await finish_in_session(
            lambda finalize_db: fail_task(
                finalize_db,
                started.task_id,
                worker_id,
                error_code="AI_TEMPORARILY_UNAVAILABLE",
                retryable=True,
            )
        )
        emit_ai_metric("retry_rate", 1, {"task_type": started.task_type})
        return "failed"
    result_ref, revisions = outcome
    async def complete_in_handler(handler_db: Any) -> Any:
        before_supersede = getattr(finalize_handler, "before_supersede", None)
        before_not_applied = getattr(finalize_handler, "before_not_applied", None)
        return await complete_task(
            handler_db,
            started.task_id,
            worker_id,
            result_ref,
            revisions,
            before_supersede=before_supersede,
            before_not_applied=before_not_applied,
        )

    if finalize_handler is None:
        await finish_in_session(
            lambda finalize_db: complete_task(
                finalize_db, started.task_id, worker_id, result_ref, revisions
            )
        )
    else:
        await finalize_handler(complete_in_handler)
    return "completed"


async def _run_round(worker_id: str, batch_size: int) -> tuple[int, int, int]:
    """One real round: reap stale leases, then claim/process tasks one by one."""
    if session_factory is None:
        raise RuntimeError("数据库驱动未安装，无法运行 AI Worker")
    if not TASK_HANDLERS:
        # 未注册任何业务 handler 时绝不触碰生产数据：不 reap、不 claim、不写
        # 库（review finding I-2）——``--once`` 非 dry-run 在此也是纯只读空转。
        logger.info("ai_worker_no_handlers_skip_round worker_id=%s", worker_id)
        return 0, 0, 0
    async with session_factory() as reap_db:
        now = _now()
        try:
            reaped = await reap_expired_leases(reap_db, now, limit=batch_size)
            await reap_db.commit()
        except Exception:
            await reap_db.rollback()
            logger.exception("ai_worker_reap_failed")
            reaped = []
    if reaped:
        logger.info("ai_worker_reaped count=%d", len(reaped))
        emit_ai_metric("lease_reclaimed", len(reaped), {"worker_id": worker_id})

    # 批次3 #24：retry_wait 积压仪表。每轮按 task_type 统计重试积压并计入
    # task_retry_backlog（超过 ai_metrics_backlog_warn_threshold 时由
    # emit_ai_metric 统一打告警）；重试耗尽仍滞留 retry_wait 的行
    # （leased 阶段失败只能回 retry_wait）单独点名，覆盖「卡重试一天」盲区。
    from sqlalchemy import text as _sa_text
    try:
        async with session_factory() as gauge_db:
            backlog_rows = (
                await gauge_db.execute(
                    _sa_text(
                        "SELECT task_type, COUNT(*) AS n FROM ai_task "
                        "WHERE status = 'retry_wait' GROUP BY task_type"
                    )
                )
            ).mappings().all()
            stuck = int(
                await gauge_db.scalar(
                    _sa_text(
                        "SELECT COUNT(*) FROM ai_task "
                        "WHERE status = 'retry_wait' "
                        "AND attempt_count >= max_attempts"
                    )
                )
                or 0
            )
        for row in backlog_rows:
            emit_ai_metric(
                "task_retry_backlog",
                float(row["n"]),
                {"task_type": str(row["task_type"])},
            )
        if stuck:
            logger.warning("ai_task_retry_exhausted_stuck count=%d", stuck)
    except Exception:
        logger.exception("ai_worker_retry_backlog_gauge_failed")

    claimed_count = 0
    completed = 0
    failed = 0
    for _ in range(batch_size):
        async with session_factory() as claim_db:
            try:
                claimed = await claim_tasks(
                    claim_db, worker_id, _now(), limit=1
                )
                await claim_db.commit()
            except Exception:
                await claim_db.rollback()
                logger.exception("ai_worker_claim_failed")
                break
        if not claimed:
            break
        task = claimed[0]
        claimed_count += 1
        if task.created_at is not None:
            # 遍历 claimed 时重新取 _now() 计算 queue_age：reap 阶段的 now
            # 已经过时（claim 期间发生了 IO/等待），用陈旧 now 会让 age 偏小。
            age_now = _now()
            age = max(0.0, (age_now - task.created_at).total_seconds())
            emit_ai_metric("queue_age", age, {"task_type": task.task_type})
        outcome = await _process(
            None, task, worker_id, session_provider=session_factory
        )
        if outcome == "completed":
            completed += 1
        elif outcome == "failed":
            failed += 1
    if failed:
        emit_ai_metric("retry_rate", failed, {"worker_id": worker_id})
    return claimed_count, completed, failed


async def _run_cleanup_round(worker_id: str, batch_size: int) -> dict[str, int]:
    """One cleanup-consumer round: claim outbox delete events and consume them."""
    if session_factory is None:
        raise RuntimeError("数据库驱动未安装，无法运行 AI Worker 消费者")
    from sqlalchemy import text

    from app.services.derivation_outbox import run_cleanup_consumer_round

    async with session_factory() as db:
        now = _now()
        stats = await run_cleanup_consumer_round(db, worker_id, now, batch_size)
        # 缺陷 12：deletion_propagation_seconds 应测量事件 occurred_at 到当前
        # 的处理延迟（秒），而非把应用事件计数当秒数。从 derivation_outbox 中
        # 取本轮刚标记 succeeded 的事件的 occurred_at 计算平均延迟：用子查询
        # 限定到最近 applied 条 succeeded 行，再对外层算 AVG。
        applied = int(stats.get("applied", 0))
        if applied > 0:
            delay_result = await db.execute(
                text(
                    "SELECT AVG(TIMESTAMPDIFF(SECOND, occurred_at, :now)) "
                    "AS avg_delay FROM ( "
                    "  SELECT occurred_at FROM derivation_outbox "
                    "  WHERE status = 'succeeded' AND occurred_at <= :now "
                    "  ORDER BY occurred_at DESC LIMIT :applied"
                    ") AS recent"
                ),
                {"now": now, "applied": applied},
            )
            delay_row = delay_result.mappings().first()
            avg_delay = float(delay_row["avg_delay"]) if delay_row and delay_row["avg_delay"] is not None else 0.0
            emit_ai_metric("deletion_propagation_seconds", max(0.0, avg_delay), {"worker_id": worker_id})
        else:
            emit_ai_metric("deletion_propagation_seconds", 0.0, {"worker_id": worker_id})
        # 缺陷 13：outbox_backlog 应为 derivation_outbox 中 status='pending' 的
        # 积压总数，而非本轮认领数（claimed）。
        backlog_result = await db.execute(
            text(
                "SELECT COUNT(*) AS cnt FROM derivation_outbox WHERE status = 'pending'"
            )
        )
        backlog_row = backlog_result.mappings().first()
        backlog = int(backlog_row["cnt"]) if backlog_row else 0
        emit_ai_metric("outbox_backlog", float(backlog), {"worker_id": worker_id})
        await db.commit()
        return stats


async def _run_forever(worker_id: str, batch_size: int, idle_seconds: float) -> None:
    while True:
        try:
            claimed, completed, failed = await _run_round(worker_id, batch_size)
            logger.info(
                "ai_worker_round claimed=%d completed=%d failed=%d",
                claimed,
                completed,
                failed,
            )
        except Exception:
            logger.exception("ai_worker_round_failed")
        await asyncio.sleep(idle_seconds)


async def _run_cleanup_forever(
    worker_id: str, batch_size: int, idle_seconds: float
) -> None:
    while True:
        try:
            stats = await _run_cleanup_round(worker_id, batch_size)
            logger.info("ai_worker_cleanup_round %s", stats)
        except Exception:
            logger.exception("ai_worker_cleanup_round_failed")
        await asyncio.sleep(idle_seconds)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI 通用任务 Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="运行单轮后退出（不带 --once 时持续循环）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印概要，不访问数据库、不修改任何数据",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="单轮领取/回收任务上限",
    )
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=5.0,
        help="循环模式两轮之间的间隔秒数",
    )
    parser.add_argument(
        "--consumers",
        action="store_true",
        help="切换到 derivation-outbox 清理消费者模式（删除/撤回的异步传播）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.dry_run:
        # 安全空转：不建立会话、不写任何数据。
        if args.consumers:
            print("claimed=0 applied=0 superseded=0 duplicate=0 skipped=0")
        else:
            print("claimed=0 completed=0 failed=0")
        return 0
    worker_id = _worker_id()
    try:
        if args.consumers:
            if args.once:
                stats = asyncio.run(_run_cleanup_round(worker_id, args.batch_size))
                print(
                    "claimed=%d applied=%d superseded=%d duplicate=%d skipped=%d"
                    % (
                        stats["claimed"],
                        stats["applied"],
                        stats["superseded"],
                        stats["duplicate"],
                        stats["skipped"],
                    )
                )
                return 0
            asyncio.run(
                _run_cleanup_forever(worker_id, args.batch_size, args.idle_seconds)
            )
            return 0
        if args.once:
            claimed, completed, failed = asyncio.run(_run_round(worker_id, args.batch_size))
            print(f"claimed={claimed} completed={completed} failed={failed}")
            return 0
        asyncio.run(_run_forever(worker_id, args.batch_size, args.idle_seconds))
        return 0
    except KeyboardInterrupt:
        logger.info("ai_worker_stopped worker_id=%s", worker_id)
        return 0
    except Exception as exc:
        logger.exception("ai_worker_fatal worker_id=%s", worker_id)
        print(f"worker failed: {type(exc).__name__}", file=sys.stderr)
        return 1


def register_business_handlers() -> None:
    """显式注册全部业务 handler（Task 10/11/9/8 的交接收尾，review C-1/C-2/C-3）。

    独立 ``python -m app.workers.ai_worker`` 进程只导入本模块，绝不靠「路由导入
    服务模块时副作用注册」兜底（那在真实部署中不成立）。这里显式导入并注册：

    - ``search_parse``（草稿解析）/``search_execute``（快照执行）→ app.services.ai.search
    - ``compatibility``（重算）→ app.services.ai.compatibility
    - ``profile_projection``（发布后投影重建）→ app.services.ai.profile_projection_handler
    - ``cleanup``（删除/撤回物理清理）→ app.services.ai.profile.cleanup_handler

    search/compatibility/features 模块均不反向 import worker，因此无循环依赖；
    ``setdefault`` 使注册幂等（测试中重复调用安全）。
    """
    from app.services.ai.compatibility import (
        COMPATIBILITY_LLM_TASK_TYPE,
        COMPATIBILITY_TASK_TYPE,
        compatibility_execute_handler,
        compatibility_llm_execute_handler,
    )
    from app.services.ai.profile import (
        CLEANUP_TASK_TYPE,
        NARRATIVE_TASK_TYPE,
        PROJECTION_TASK_TYPE,
        cleanup_handler,
        generate_profile_narrative_handler,
        profile_projection_handler,
    )
    from app.services.ai.recommend import (
        RECOMMEND_TASK_TYPE,
        recommend_rebuild_handler,
    )
    from app.services.ai.search import (
        SEARCH_EXECUTE_TASK_TYPE,
        SEARCH_PARSE_TASK_TYPE,
        SEARCH_SUGGEST_TASK_TYPE,
        parse_search_draft,
        search_execute_handler,
        search_suggest_handler,
    )
    from app.services.voice.transcribe_handler import (
        VOICE_TRANSCRIBE_TASK_TYPE,
        voice_transcribe_handler,
    )

    TASK_HANDLERS.setdefault(SEARCH_PARSE_TASK_TYPE, parse_search_draft)
    TASK_HANDLERS.setdefault(SEARCH_EXECUTE_TASK_TYPE, search_execute_handler)
    TASK_HANDLERS.setdefault(SEARCH_SUGGEST_TASK_TYPE, search_suggest_handler)
    TASK_HANDLERS.setdefault(COMPATIBILITY_TASK_TYPE, compatibility_execute_handler)
    TASK_HANDLERS.setdefault(
        COMPATIBILITY_LLM_TASK_TYPE, compatibility_llm_execute_handler
    )
    TASK_HANDLERS.setdefault(PROJECTION_TASK_TYPE, profile_projection_handler)
    TASK_HANDLERS.setdefault(CLEANUP_TASK_TYPE, cleanup_handler)
    TASK_HANDLERS.setdefault(NARRATIVE_TASK_TYPE, generate_profile_narrative_handler)
    TASK_HANDLERS.setdefault(RECOMMEND_TASK_TYPE, recommend_rebuild_handler)
    TASK_HANDLERS.setdefault(VOICE_TRANSCRIBE_TASK_TYPE, voice_transcribe_handler)


register_business_handlers()


if __name__ == "__main__":
    raise SystemExit(main())
