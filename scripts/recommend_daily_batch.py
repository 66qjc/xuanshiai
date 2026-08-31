"""每日批量物化三类推荐快照（WP-P6 触发时机之二，方案 §四 WP-P6）。

对每个存在 active ``personal_compatibility`` 投影的用户入队一条
``recommend_rebuild`` 任务（幂等键 ``recommend-daily-{user}-{UTC日期}``，
同用户同日至多一任务），由 ai_worker 异步消费物化。

用法（后端仓库根目录）::

    python scripts/recommend_daily_batch.py [--limit 500]

建议生产 crontab（与 ai_worker 常驻进程同一环境）::

    0 3 * * * cd /path/to/xuanshiai-backend && python scripts/recommend_daily_batch.py

只入队不物化：打分/可见性/授权门禁全部在 worker handler 内执行，本脚本
对业务库只写 ai_task 行。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import text


async def enqueue_daily_batch(limit: int, session_factory=None) -> int:
    """入队每日批量；``session_factory`` 可注入（集成测试用测试库工厂）。"""
    from app.services.ai.recommend import RECOMMEND_TASK_TYPE
    from app.services.ai.tasks import enqueue_task

    if session_factory is None:
        from app.db.session import session_factory as session_factory  # noqa: F811

    from app.services.ai.compatibility import _load_revision_vector

    today = datetime.now(UTC).strftime("%Y%m%d")
    enqueued = 0
    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT DISTINCT subject_user_id FROM ai_feature_projection "
                    "WHERE projection_kind = 'personal_compatibility' "
                    "AND status = 'active' "
                    "AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP()) "
                    "ORDER BY subject_user_id LIMIT :limit"
                ),
                {"limit": int(limit)},
            )
        ).all()
        for (user_id,) in rows:
            # revisions 必须携带：complete_task 复核以"入队向量==完成向量"判定
            # 任务有效性，缺省 {} 会让重建结果在完成时被整批回滚（review P1）。
            await enqueue_task(
                db=db,
                owner_user_id=int(user_id),
                task_type=RECOMMEND_TASK_TYPE,
                idempotency_key=f"recommend-daily-{int(user_id)}-{today}",
                request_hash=f"recommend-daily:{int(user_id)}:{today}",
                revisions=await _load_revision_vector(db, int(user_id)),
            )
            enqueued += 1
        await db.commit()
    return enqueued


def main() -> None:
    parser = argparse.ArgumentParser(description="每日批量入队三类推荐重建任务")
    parser.add_argument("--limit", type=int, default=500, help="本次最多入队用户数")
    args = parser.parse_args()
    enqueued = asyncio.run(enqueue_daily_batch(args.limit))
    print(f"recommend daily batch enqueued: {enqueued} tasks (run_id hint: {uuid4().hex[:8]})")


if __name__ == "__main__":
    main()
