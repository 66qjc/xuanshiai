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
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine, async_sessionmaker, create_async_engine


REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_DATABASE_URL = os.getenv(
    "AI_TEST_DATABASE_URL",
    "mysql+aiomysql://root:@127.0.0.1:3307/xuanshiai_ai_test",
)
TEST_REDIS_URL = os.getenv("AI_TEST_REDIS_URL", "redis://127.0.0.1:6380/5")
TEST_DATABASE_NAME = urlsplit(TEST_DATABASE_URL).path.lstrip("/")


@pytest.fixture(scope="session", autouse=True)
def ai_test_environment() -> Iterator[dict[str, str]]:
    """Point bootstrap and child worker processes at the isolated services."""

    names = {
        "DATABASE_URL": TEST_DATABASE_URL,
        "REDIS_URL": TEST_REDIS_URL,
        "ENVIRONMENT": "testing",
        "AUTO_INIT_DB": "false",
        "AI_MASTER_ENABLED": "true",
        "AI_PROFILE_ENABLED": "true",
        "AI_SEARCH_ENABLED": "true",
        "AI_COMPATIBILITY_SHADOW_ENABLED": "true",
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
        from database_setup_marriage import initialize_database

        initialize_database()
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
