"""FastAPI application factory and process-level configuration."""

import asyncio
import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.router import OPENAPI_TAGS, api_router
from app.core.config import settings

logger = logging.getLogger(__name__)


async def initialize_database_on_startup() -> None:
    """Run the existing synchronous, idempotent initializer outside the event loop."""
    if not settings.auto_init_db:
        logger.info("AUTO_INIT_DB=false，跳过数据库自动初始化")
        return
    logger.info("正在执行数据库自动初始化...")
    try:
        from database_setup_marriage import initialize_database

        await asyncio.to_thread(initialize_database)
    except Exception as exc:
        logger.exception("数据库自动初始化失败")
        raise RuntimeError("数据库自动初始化失败，请检查 DATABASE_URL 和 MySQL 服务") from exc
    logger.info("数据库自动初始化完成")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await initialize_database_on_startup()
    yield


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        root_path="/api",  # ← 添加这一行
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    # ... 其余代码保持不变 ...

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
    application.mount("/storage/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")
    application.include_router(api_router, prefix=settings.api_prefix)

    @application.get("/", tags=["系统"])
    async def root() -> dict[str, str]:
        """Return a small service discovery response."""
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs" if settings.docs_enabled else "disabled",
        }

    return application


app = create_app()
