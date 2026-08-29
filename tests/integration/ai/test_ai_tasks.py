"""Real-MySQL checks for the ai_task percentage-progress path (WP-S1 / F9).

The search pipeline stages write ``progress_percent`` alongside ``stage`` via
``_set_search_task_stage``; these checks pin the column write and read-back
against the dedicated test MySQL service from ``compose.ai-test.yml``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_real_set_search_task_stage_writes_progress(
    real_db_session: AsyncSession,
) -> None:
    from app.services.ai.search import _set_search_task_stage

    task_id = "task-progress-check-0001"
    # owner_user_id 落在 conftest sweep 用户段内：即使断言失败残留 running 行，
    # 下一个用例开始前也会被 sweep 清除——否则迁移测试会因
    # "refusing down while AI tasks are active" 拒绝回滚。
    await real_db_session.execute(
        text("DELETE FROM ai_task WHERE task_id = :t"), {"t": task_id}
    )
    await real_db_session.execute(
        text(
            "INSERT INTO ai_task (task_id, owner_user_id, task_type, scene, "
            " idempotency_key, status) VALUES (:t, 9876543001, "
            " 'search_execute', 'ai', 'k-progress-1', 'running')"
        ),
        {"t": task_id},
    )
    await real_db_session.commit()

    try:
        await _set_search_task_stage(real_db_session, task_id, "filtering", progress=30)
        row = (
            await real_db_session.execute(
                text("SELECT stage, progress_percent FROM ai_task WHERE task_id = :t"),
                {"t": task_id},
            )
        ).mappings().first()
        assert row["stage"] == "filtering"
        assert int(row["progress_percent"]) == 30
        # NULL 保留语义（终审 Minor）：不带 progress 的阶段更新走 COALESCE，
        # 不得覆盖已写入的进度值。
        await _set_search_task_stage(real_db_session, task_id, "ranking")
        row = (
            await real_db_session.execute(
                text("SELECT stage, progress_percent FROM ai_task WHERE task_id = :t"),
                {"t": task_id},
            )
        ).mappings().first()
        assert row["stage"] == "ranking"
        assert int(row["progress_percent"]) == 30, "progress=None 必须保留旧进度"
        await real_db_session.commit()
    finally:
        # 终态/删除收尾，避免在共享测试库遗留活动任务阻塞迁移回滚。
        await real_db_session.execute(
            text("DELETE FROM ai_task WHERE task_id = :t"), {"t": task_id}
        )
        await real_db_session.commit()
