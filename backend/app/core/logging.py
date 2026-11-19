"""
Structured logging setup for Atlas.

When LOG_FORMAT=json  → JSON records (timestamp, level, logger, message,
                         correlation_id, exc_info on errors).
When LOG_FORMAT=text  → human-readable plain text (local dev convenience).

Level is driven by LOG_LEVEL; overridden to DEBUG when APP_DEBUG=true.
Secrets and PII are never emitted — this module sets root-level config only;
callers are responsible for not passing sensitive values to log statements.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config.settings import Settings


class _CorrelationFilter(logging.Filter):
    """Injects correlation_id from the request context into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        from backend.app.core.correlation import get_correlation_id

        record.correlation_id = get_correlation_id()  # type: ignore[attr-defined]
        return True


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict[str, object] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", ""),
        }
        if record.exc_info:
            log_obj["exc_info"] = self.formatException(record.exc_info)
        elif record.exc_text:
            log_obj["exc_info"] = record.exc_text
        return json.dumps(log_obj)


def setup_logging(settings: Settings) -> None:
    """Configure the root logger.  Call once at application startup."""
    level_name = "DEBUG" if settings.APP_DEBUG else settings.LOG_LEVEL.upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)

    if settings.LOG_FORMAT == "json":
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s (%(correlation_id)s) %(message)s"
            )
        )

    handler.addFilter(_CorrelationFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Silence noisy third-party loggers at WARNING unless DEBUG
    if not settings.APP_DEBUG:
        for noisy in ("uvicorn.access", "uvicorn.error", "fastapi"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
