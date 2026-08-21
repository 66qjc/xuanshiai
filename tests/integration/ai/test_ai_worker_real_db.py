"""Real-process worker and Redis checks for the AI phase-one baseline."""

from __future__ import annotations

import re
import subprocess
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.ai_schema import AI_TABLES
from tests.integration.ai.conftest import REPO_ROOT, TEST_REDIS_URL


@pytest.mark.asyncio
async def test_real_redis_is_visible_across_independent_connections(
    real_redis: Redis,
) -> None:
    key = f"ai-integration:{uuid.uuid4().hex}"
    other = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await real_redis.set(key, "ready", ex=60)
        assert await other.get(key) == "ready"
    finally:
        await real_redis.delete(key)
        await other.aclose()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows 下 spawn 独立 worker 子进程继承事件循环句柄失败"
        "（WinError 6/50）；跨进程 claim 验证改在 Linux 容器内执行："
        "docker compose -f compose.ai-test.yml run --rm worker-a "
        "python -m pytest tests/integration/ai/test_ai_worker_real_db.py -q"
    ),
)
@pytest.mark.asyncio
async def test_two_independent_worker_processes_claim_one_real_task(
    ai_test_environment: dict[str, str],
    worker_env: dict[str, str],
) -> None:
    base_url = ai_test_environment["DATABASE_URL"]
    parsed = urlsplit(base_url)
    database_name = f"ai_worker_{uuid.uuid4().hex[:12]}"
    admin_url = urlunsplit((parsed.scheme, parsed.netloc, "/mysql", "", ""))
    worker_url = urlunsplit((parsed.scheme, parsed.netloc, f"/{database_name}", "", ""))
    admin_engine = create_async_engine(admin_url)
    worker_engine = create_async_engine(worker_url)

    async with admin_engine.begin() as connection:
        await connection.exec_driver_sql(f"CREATE DATABASE `{database_name}`")
    async with worker_engine.begin() as connection:
        await connection.exec_driver_sql(AI_TABLES["ai_task"])

    task_id = f"it-{uuid.uuid4().hex}"
    try:
        factory = async_sessionmaker(worker_engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text(
                    "INSERT INTO ai_task "
                    "(task_id, owner_user_id, task_type, scene, idempotency_key, request_digest, "
                    "status, attempt_count, max_attempts, created_at, updated_at) "
                    "VALUES (:task_id, :owner_user_id, 'integration_no_handler', 'integration', "
                    ":idempotency_key, :request_digest, 'queued', 0, 1, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
                ),
                {
                    "task_id": task_id,
                    "owner_user_id": 9_800_000_001,
                    "idempotency_key": f"it-{uuid.uuid4().hex}",
                    "request_digest": "0" * 64,
                },
            )
            await session.commit()

        worker_env = {**worker_env, "DATABASE_URL": worker_url}
        command = [
            sys.executable,
            "-m",
            "app.workers.ai_worker",
            "--once",
            "--batch-size",
            "1",
        ]
        processes = [
            subprocess.Popen(  # noqa: ASYNC220 - 必须用真实 OS 进程验证跨进程 claim
                command,
                cwd=REPO_ROOT,
                env=worker_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outputs: list[tuple[int, str, str]] = []
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                outputs.append((process.returncode, stdout, stderr))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)

        assert all(
            code == 0 for code, _, _ in outputs
        ), "worker process failed: " + repr(outputs)
        claimed = [
            int(match.group(1))
            for _, stdout, _ in outputs
            if (match := re.search(r"claimed=(\d+)", stdout))
        ]
        assert sorted(claimed) == [0, 1], outputs

        async with worker_engine.connect() as connection:
            result = await connection.execute(
                text("SELECT status FROM ai_task WHERE task_id = :task_id"),
                {"task_id": task_id},
            )
            assert result.scalar_one() == "failed"
    finally:
        await worker_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.exec_driver_sql(f"DROP DATABASE `{database_name}`")
        await admin_engine.dispose()
