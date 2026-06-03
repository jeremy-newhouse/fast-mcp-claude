"""Centralized logging configuration with structured output.

Provides JSON-structured logging suitable for log aggregation, plus a colored
console formatter for development. Includes correlation-id context vars and
a @timed decorator for performance tracing.
"""

import contextvars
import functools
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)

T = TypeVar("T")

SENSITIVE_LOG_FIELDS_EXACT = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "encryption_key",
    "mcp_api_key",
    "peer_api_key",
    "access_key",
    "secret_key",
    "private_key",
}

SENSITIVE_LOG_SUFFIXES = ("_password", "_secret", "_token", "_credential")


def _sanitize_log_value(key: str, value: Any) -> Any:
    """Redact known-sensitive field names."""
    key_lower = key.lower()
    if key_lower in SENSITIVE_LOG_FIELDS_EXACT:
        return "[REDACTED]"
    if key_lower.endswith(SENSITIVE_LOG_SUFFIXES):
        return "[REDACTED]"
    return value


class StructuredFormatter(logging.Formatter):
    """JSON-line formatter for production log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        correlation_id = correlation_id_var.get()
        if correlation_id:
            log_data["correlation_id"] = correlation_id

        standard_attrs = {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "exc_info",
            "exc_text",
            "thread",
            "threadName",
            "taskName",
            "message",
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                log_data[key] = _sanitize_log_value(key, value)

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, default=str)


class ConsoleFormatter(logging.Formatter):
    """Colored human-readable formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        timestamp = datetime.now().strftime("%H:%M:%S")
        message = f"{timestamp} {color}[{record.levelname:8}]{self.RESET} {record.name}: "
        message += record.getMessage()
        correlation_id = correlation_id_var.get()
        if correlation_id:
            message += f" [cid={correlation_id}]"
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return message


def setup_logging(
    level: str = "INFO",
    json_format: bool = False,
    stream: Any = None,
) -> None:
    """Configure application-wide logging."""
    if stream is None:
        stream = sys.stderr

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    handler = logging.StreamHandler(stream)
    handler.setLevel(numeric_level)

    if json_format:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())

    root_logger.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_correlation_id(correlation_id: str | None) -> None:
    correlation_id_var.set(correlation_id)


def get_correlation_id() -> str | None:
    return correlation_id_var.get()


def timed(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator that logs execution time at DEBUG level."""
    import asyncio

    logger = get_logger(func.__module__)

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = await func(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "%s completed in %.2fms",
                func.__name__,
                elapsed_ms,
                extra={"timing_ms": elapsed_ms, "function": func.__name__},
            )
            return result
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "%s failed after %.2fms",
                func.__name__,
                elapsed_ms,
                extra={"timing_ms": elapsed_ms, "function": func.__name__},
            )
            raise

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "%s completed in %.2fms",
                func.__name__,
                elapsed_ms,
                extra={"timing_ms": elapsed_ms, "function": func.__name__},
            )
            return result
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "%s failed after %.2fms",
                func.__name__,
                elapsed_ms,
                extra={"timing_ms": elapsed_ms, "function": func.__name__},
            )
            raise

    if asyncio.iscoroutinefunction(func):
        return async_wrapper  # type: ignore[return-value]
    return sync_wrapper  # type: ignore[return-value]
