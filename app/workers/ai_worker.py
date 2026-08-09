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
from app.services.ai.profile import extract_profile_turn
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
    db: Any,
    handler: Callable[..., Awaitable[Any]],
    task: AiTaskRecord,
    worker_id: str,
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
    """
    interval = _heartbeat_interval()
    handler_task = asyncio.create_task(handler(db, task, worker_id))
    try:
        while True:
            done, _ = await asyncio.wait({handler_task}, timeout=interval)
            if handler_task in done:
                return handler_task.result()
            try:
                await heartbeat_lease(db, task.task_id, worker_id, _now())
            except Exception:  # noqa: BLE001 - heartbeat must never kill the handler
                logger.warning(
                    "ai_worker_heartbeat_failed task_id=%s",
                    task.task_id,
                    exc_info=True,
                )
    except asyncio.CancelledError:
        handler_task.cancel()
        raise


async def _process(
    db: Any, task: AiTaskRecord, worker_id: str
) -> str:
    """Run one claimed task; returns ``completed``/``failed``/``skipped``."""
    try:
        started = await start_task(db, task.task_id, worker_id)
    except Exception:  # noqa: BLE001 - lease or status race, leave for reaper
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
            await fail_task(
                db,
                started.task_id,
                worker_id,
                error_code="AI_FEATURE_DISABLED",
                retryable=False,
            )
        except Exception:  # noqa: BLE001 - 终态写入失败，留给 reaper/下一轮兜底
            logger.exception(
                "ai_worker_no_handler_fail_failed task_id=%s", started.task_id
            )
        return "failed"
    try:
        outcome = await _run_with_heartbeat(db, handler, started, worker_id)
    except Exception as exc:  # noqa: BLE001 - boundary conversion
        logger.warning(
            "ai_worker_exec_failed task_id=%s error=%s",
            started.task_id,
            type(exc).__name__,
        )
        await fail_task(
            db,
            started.task_id,
            worker_id,
            error_code="AI_TEMPORARILY_UNAVAILABLE",
            retryable=True,
        )
        return "failed"
    if outcome is None:
        await fail_task(
            db,
            started.task_id,
            worker_id,
            error_code="AI_TEMPORARILY_UNAVAILABLE",
            retryable=True,
        )
        return "failed"
    result_ref, revisions = outcome
    await complete_task(db, started.task_id, worker_id, result_ref, revisions)
    return "completed"


async def _run_round(worker_id: str, batch_size: int) -> tuple[int, int, int]:
    """One real round: reap stale leases, claim, start and dispatch tasks."""
    if session_factory is None:
        raise RuntimeError("数据库驱动未安装，无法运行 AI Worker")
    if not TASK_HANDLERS:
        # 未注册任何业务 handler 时绝不触碰生产数据：不 reap、不 claim、不写
        # 库（review finding I-2）——``--once`` 非 dry-run 在此也是纯只读空转。
        logger.info("ai_worker_no_handlers_skip_round worker_id=%s", worker_id)
        return 0, 0, 0
    async with session_factory() as db:
        now = _now()
        try:
            reaped = await reap_expired_leases(db, now, limit=batch_size)
        except Exception:  # noqa: BLE001 - reaping must not stop the round
            logger.exception("ai_worker_reap_failed")
            reaped = []
        if reaped:
            logger.info("ai_worker_reaped count=%d", len(reaped))
        try:
            claimed = await claim_tasks(db, worker_id, now, limit=batch_size)
        except Exception:  # noqa: BLE001 - claiming must not stop the round
            logger.exception("ai_worker_claim_failed")
            claimed = []
        completed = 0
        failed = 0
        for task in claimed:
            outcome = await _process(db, task, worker_id)
            if outcome == "completed":
                completed += 1
            elif outcome == "failed":
                failed += 1
        await db.commit()
        return len(claimed), completed, failed


async def _run_cleanup_round(worker_id: str, batch_size: int) -> dict[str, int]:
    """One cleanup-consumer round: claim outbox delete events and consume them."""
    if session_factory is None:
        raise RuntimeError("数据库驱动未安装，无法运行 AI Worker 消费者")
    from app.services.derivation_outbox import run_cleanup_consumer_round

    async with session_factory() as db:
        stats = await run_cleanup_consumer_round(db, worker_id, _now(), batch_size)
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
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("ai_worker_round_failed")
        await asyncio.sleep(idle_seconds)


async def _run_cleanup_forever(
    worker_id: str, batch_size: int, idle_seconds: float
) -> None:
    while True:
        try:
            stats = await _run_cleanup_round(worker_id, batch_size)
            logger.info("ai_worker_cleanup_round %s", stats)
        except Exception:  # noqa: BLE001 - keep the loop alive
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
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
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
        COMPATIBILITY_TASK_TYPE,
        compatibility_execute_handler,
    )
    from app.services.ai.profile import (
        CLEANUP_TASK_TYPE,
        PROJECTION_TASK_TYPE,
        cleanup_handler,
        profile_projection_handler,
    )
    from app.services.ai.search import (
        SEARCH_EXECUTE_TASK_TYPE,
        SEARCH_PARSE_TASK_TYPE,
        parse_search_draft,
        search_execute_handler,
    )

    TASK_HANDLERS.setdefault(SEARCH_PARSE_TASK_TYPE, parse_search_draft)
    TASK_HANDLERS.setdefault(SEARCH_EXECUTE_TASK_TYPE, search_execute_handler)
    TASK_HANDLERS.setdefault(COMPATIBILITY_TASK_TYPE, compatibility_execute_handler)
    TASK_HANDLERS.setdefault(PROJECTION_TASK_TYPE, profile_projection_handler)
    TASK_HANDLERS.setdefault(CLEANUP_TASK_TYPE, cleanup_handler)


register_business_handlers()


if __name__ == "__main__":
    raise SystemExit(main())
