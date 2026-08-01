"""FastAPI application factory and process-level configuration."""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from uuid import uuid4

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from app.api.router import OPENAPI_TAGS, api_router
from app.core.config import settings
from app.core.logging import configure_logging, request_id_context

configure_logging(settings)
logger = logging.getLogger(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


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
    logger.info(
        "application_starting environment=%s version=%s",
        settings.environment,
        settings.app_version,
    )
    await initialize_database_on_startup()
    yield
    logger.info("application_stopping")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def log_requests(request: Request, call_next) -> Response:
        """Log request boundaries without recording bodies or credentials."""
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else uuid4().hex
        )
        context_token = request_id_context.set(request_id)
        started_at = time.perf_counter()
        client_host = request.client.host if request.client else "-"
        logger.info(
            "request_started method=%s path=%s client=%s",
            request.method,
            request.url.path,
            client_host,
        )
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.exception(
                "request_failed method=%s path=%s status=500 duration_ms=%.2f client=%s",
                request.method,
                request.url.path,
                duration_ms,
                client_host,
            )
            raise
        else:
            duration_ms = (time.perf_counter() - started_at) * 1000
            status_code = response.status_code
            log_method = (
                logger.error
                if status_code >= 500
                else logger.warning
                if status_code >= 400
                else logger.info
            )
            log_method(
                "request_completed method=%s path=%s status=%s duration_ms=%.2f client=%s content_length=%s",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
                client_host,
                response.headers.get("content-length", "-"),
            )
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_context.reset(context_token)

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
