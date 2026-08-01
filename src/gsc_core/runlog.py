"""Logging for gsc-mcp.

Hard rule: nothing here may write to stdout. The MCP transport frames
JSON-RPC on stdout, so one stray byte corrupts the session and the failure
surfaces as an unrelated client error. Handlers target stderr and a file.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import paths

ROOT_NAME = "gsc"
_MAX_BYTES = 2_000_000
_BACKUP_COUNT = 3

_configured = False


def init(level: int = logging.INFO) -> logging.Logger:
    """Attach handlers to the root gsc logger. Safe to call repeatedly."""
    global _configured
    logger = logging.getLogger(ROOT_NAME)
    if _configured:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    directory = paths.log_dir()
    directory.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        directory / "gsc.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )

    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(stderr_handler)
    _configured = True
    return logger


def get(name: str) -> logging.Logger:
    """A child logger. Modules call this at import; only entrypoints call init()."""
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def _reset_for_tests() -> None:
    """Drop handlers so a test can re-init against a fresh temp directory."""
    global _configured
    logger = logging.getLogger(ROOT_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    _configured = False
