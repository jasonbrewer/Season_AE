"""Shared logging setup.

Two things live here:

* :func:`configure_app_logging` — one-time console logging for the app process.
* :func:`run_log_handler` — a context manager that tees a single backup run into
  its own human-readable ``.log`` file next to that run's manifest.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOGGER_NAME = "footage_pipeline"
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_RUN_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the package logger, or a child of it."""
    if name is None:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def configure_app_logging(level: int = logging.INFO) -> logging.Logger:
    """Attach a console handler to the package logger exactly once."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT))
        logger.addHandler(handler)
        # The package logger is the root of our tree; don't double-log to root.
        logger.propagate = False
        _configured = True
    return logger


@contextmanager
def run_log_handler(log_path: Path, level: int = logging.INFO) -> Iterator[logging.Logger]:
    """Tee package log records into ``log_path`` for the duration of the block.

    Yields the package logger. The handler is always removed and closed on the
    way out, so a long-lived process does not accumulate file handles.
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    # A run log is worthless if the package logger was left at WARNING.
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_RUN_FORMAT, _DATE_FORMAT))
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        handler.flush()
        logger.removeHandler(handler)
        handler.close()
