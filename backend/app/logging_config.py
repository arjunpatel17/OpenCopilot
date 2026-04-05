"""Centralized logging configuration for production.

Provides structured JSON logging for machine parsing (Azure Monitor, etc.)
with request correlation IDs for tracing requests across services.
"""

import logging
import json
import sys
import time
import uuid
from contextvars import ContextVar

# Per-request correlation ID — set by middleware, readable from any async context
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter for production environments.

    Each log line is a single JSON object with consistent fields, making it
    easy to query in Azure Monitor, CloudWatch, Datadog, etc.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add correlation ID if available
        req_id = request_id_var.get("")
        if req_id:
            log_entry["request_id"] = req_id

        # Add extra fields passed via logger.info("msg", extra={...})
        for key in ("chat_id", "agent_name", "model", "session_id",
                     "duration_ms", "status_code", "method", "path",
                     "user", "error_type"):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val

        # Include exception info
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["error_type"] = record.exc_info[0].__name__
            log_entry["error"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structured JSON logging for the application.

    Call once at startup (in main.py lifespan or module level).
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove any existing handlers (e.g. from basicConfig)
    root.handlers.clear()

    # JSON handler to stdout (captured by Docker/Azure/cloud log collectors)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))
    root.addHandler(handler)

    # Set app loggers to DEBUG so we capture everything; the root level gates output
    logging.getLogger("app").setLevel(logging.DEBUG)

    # Quiet noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("azure").setLevel(logging.WARNING)
