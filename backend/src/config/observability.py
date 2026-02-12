"""Observability configuration — structured logging with correlation IDs."""

import logging
import json
import time
import uuid
from contextvars import ContextVar

# Per-request correlation ID
correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


class StructuredFormatter(logging.Formatter):
    """JSON log formatter that includes correlation_id, tenant_id, and timing."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": correlation_id.get(""),
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        for key in ("tenant_id", "project_id", "agent", "duration_ms", "step"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        return json.dumps(log_entry)


def setup_logging(debug: bool = False) -> None:
    """Configure structured JSON logging for the application."""
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]

    # Reduce noise from third-party libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def new_correlation_id() -> str:
    """Generate and set a new correlation ID for the current request."""
    cid = uuid.uuid4().hex[:12]
    correlation_id.set(cid)
    return cid


class Timer:
    """Simple context manager for timing code blocks."""

    def __init__(self) -> None:
        self.duration_ms: float = 0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: object) -> None:
        self.duration_ms = (time.perf_counter() - self._start) * 1000
