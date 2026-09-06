"""Logging setup.

Railway aggregates stdout, so we log a single, greppable line per event and make
sure the Bearer token can never leak into the log stream even if some library
decides to echo a request header back at us.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Iterable

_SECRET_PATTERNS: list[re.Pattern[str]] = []


def register_secret(secret: str | None) -> None:
    """Register a value that must always be masked in log output."""
    if not secret or len(secret) < 8:
        return
    _SECRET_PATTERNS.append(re.compile(re.escape(secret)))
    # Bearer tokens are often logged with only part of the value.
    _SECRET_PATTERNS.append(re.compile(re.escape(secret[:24])))


class _RedactingFilter(logging.Filter):
    """Replaces any registered secret with ``***`` in the formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _SECRET_PATTERNS:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = message
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("***REDACTED***", redacted)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(level: str = "INFO", noisy_loggers: Iterable[str] = ()) -> None:
    """Configure root logging once, at process start."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)-22s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Third-party chatter that is not useful in production.
    for name in ("httpx", "httpx2", "httpcore", "mcp.client.streamable_http", *noisy_loggers):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
