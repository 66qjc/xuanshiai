"""Application logging configuration and request correlation context."""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings


request_id_context: ContextVar[str] = ContextVar("request_id", default="-")


class RequestContextFilter(logging.Filter):
    """Add request correlation data to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        return True


def configure_logging(settings: Settings) -> None:
    """Configure console and rolling file logging once per process."""
    root_logger = logging.getLogger()
    if getattr(root_logger, "_xuanshi_configured", False):
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s %(levelname)s %(name)s "
            "request_id=%(request_id)s %(filename)s:%(lineno)d %(message)s"
        ),
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    context_filter = RequestContextFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        log_dir / "app.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger._xuanshi_configured = True

