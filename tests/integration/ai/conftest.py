"""Real dependency fixtures for the AI phase-one integration checks.

These tests deliberately use the dedicated MySQL/Redis services from
``compose.ai-test.yml``.  They do not fall back to fakes or silently skip when
the services are unavailable: a red result must identify an infrastructure or
contract failure that can be fixed.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_DATABASE_URL = os.getenv(
    "AI_TEST_DATABASE_URL",
    "mysql+aiomysql://root:@127.0.0.1:3307/xuanshiai_ai_test",
)
TEST_REDIS_URL = os.getenv("AI_TEST_REDIS_URL", "redis://127.0.0.1:6380/5")
TEST_DATABASE_NAME = urlsplit(TEST_DATABASE_URL).path.lstrip("/")


@pytest.fixture(scope="session", autouse=True)
def _ai_test_schema_bootstrap() -> Iterator[None]:
    """Initialize the dedicated test schema once per session.

    ``initialize_database`` resolves the target via ``DATABASE_URL``; the env
    entries set here are session-stable and restored when the session ends.
    """
    bootstrap_names = {
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "ENVIRONMENT": "testing",
        "AUTO_INIT_DB": "false",
    }
    previous = {name: os.environ.get(name) for name in bootstrap_names}
    os.environ.update(bootstrap_names)
    try:
        from database_setup_marriage import initialize_database

        initialize_database()
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def ai_test_environment() -> Iterator[dict[str, str]]:
    """Point child worker processes at the isolated services for one test.

    Function-scoped so the AI feature switches never leak into unit tests that
    run later in the same pytest session (a session-scoped variant left
    ``AI_MASTER_ENABLED=true`` etc. in ``os.environ`` after the integration
    folder, flipping disabled-state assertions in unrelated suites).
    """

    names = {
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "ENVIRONMENT": "testing",
        "AUTO_INIT_DB": "false",
        "AI_MASTER_ENABLED": "true",
        "AI_PROFILE_ENABLED": "true",
        "AI_SEARCH_ENABLED": "true",
        "AI_COMPATIBILITY_SHADOW_ENABLED": "true",
        "AI_RECOMMEND_ENABLED": "true",
    }
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    settings_patch = pytest.MonkeyPatch()
    try:
        # ``app.core.config.settings`` is a process singleton created before
        # pytest fixtures run. Keep the parent process aligned with the child
        # worker environment so completion-time release gates are exercised.
        from app.core.config import settings

        settings_patch.setattr(settings, "database_url", TEST_DATABASE_URL)
        settings_patch.setattr(settings, "redis_url", TEST_REDIS_URL)
        settings_patch.setattr(settings, "environment", "testing")
        settings_patch.setattr(settings, "auto_init_db", False)
        settings_patch.setattr(settings, "ai_master_enabled", True)
        settings_patch.setattr(settings, "ai_profile_enabled", True)
        settings_patch.setattr(settings, "ai_search_enabled", True)
        settings_patch.setattr(settings, "ai_compatibility_shadow_enabled", True)
        settings_patch.setattr(settings, "ai_recommend_enabled", True)
        yield names
    finally:
        settings_patch.undo()
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest_asyncio.fixture
async def real_db_engine(ai_test_environment: dict[str, str]) -> AsyncIterator[AsyncEngine]:
    """Create a real SQLAlchemy engine against the test MySQL service."""

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("SELECT 1")
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def real_db_session(
    real_db_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(real_db_engine, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


# 集成测试共享同一测试库，失败用例不会执行清理，残留种子会污染后续用例的
# 候选空间（如 trilogy 失败留下的带投影用户让搜索测试多出候选）。每个用例前
# 全局清扫测试用户段（所有集成测试用户 id 均在 9_876_543_000–9_876_549_999），
# 使每个用例从干净状态开始，不依赖前序用例的清理成功。
@pytest_asyncio.fixture(autouse=True)
async def sweep_test_users(
    ai_test_environment: dict[str, str],
) -> None:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            for statement in (
                "DELETE FROM ai_search_result WHERE snapshot_id IN (SELECT snapshot_id FROM ai_search_snapshot WHERE user_id BETWEEN 9876543000 AND 9876549999)",
                "DELETE FROM ai_search_condition WHERE draft_id IN (SELECT draft_id FROM ai_search_draft WHERE user_id BETWEEN 9876543000 AND 9876549999)",
                "DELETE FROM ai_search_snapshot WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_search_draft WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_draft_field WHERE draft_id IN (SELECT draft_id FROM ai_profile_draft WHERE user_id BETWEEN 9876543000 AND 9876549999)",
                "DELETE FROM ai_profile_draft WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_build_invite WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_candidate WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_turn WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_session WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_summary WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_revision_field WHERE revision_id IN (SELECT id FROM ai_profile_revision WHERE user_id BETWEEN 9876543000 AND 9876549999)",
                "DELETE FROM ai_profile_revision WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_compatibility_snapshot WHERE viewer_user_id BETWEEN 9876543000 AND 9876549999 OR target_user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_recommendation_snapshot WHERE viewer_user_id BETWEEN 9876543000 AND 9876549999 OR target_user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_profile_projection_status WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_feature_projection WHERE subject_user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_task WHERE owner_user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_consent_operation WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM ai_consent_grant WHERE user_id BETWEEN 9876543000 AND 9876549999",
                # 删除擦洗产生的墓碑行（user_id=NULL + user_tombstone）不落在
                # 用户段过滤内；测试库可弃，按墓碑特征整体清除避免同秒键残留。
                "DELETE FROM ai_consent_grant WHERE user_id IS NULL AND user_tombstone IS NOT NULL",
                "DELETE FROM derivation_consumer_receipt WHERE event_id IN (SELECT event_id FROM derivation_outbox WHERE aggregate_id BETWEEN 9876543000 AND 9876549999)",
                "DELETE FROM derivation_outbox WHERE aggregate_id BETWEEN 9876543000 AND 9876549999",
                # G2-B outbox 锁测试用 g2b- 前缀事件（aggregate_id 在测试用户段
                # 之外）；失败中断时残留会污染后续 claim 计数，按前缀清扫。
                "DELETE FROM derivation_consumer_receipt WHERE event_id LIKE 'g2b-%'",
                "DELETE FROM derivation_outbox WHERE event_id LIKE 'g2b-%'",
                "DELETE FROM user_block WHERE user_id BETWEEN 9876543000 AND 9876549999 OR target_user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM user_revision_state WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM user_profile_completion WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM user_privacy WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM user_auth WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM user_profile WHERE user_id BETWEEN 9876543000 AND 9876549999",
                "DELETE FROM users WHERE id BETWEEN 9876543000 AND 9876549999",
            ):
                await conn.execute(text(statement))
    finally:
        await engine.dispose()
    yield


@pytest_asyncio.fixture
async def real_redis(ai_test_environment: dict[str, str]) -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def worker_env(ai_test_environment: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(ai_test_environment)
    env["PYTHONPATH"] = str(REPO_ROOT)
    return env
