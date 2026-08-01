"""Where gsc-mcp keeps its files, per platform.

Nothing else in the package hardcodes a path. Set GSC_MCP_HOME to override
everything at once — the test suite and the agency shell both rely on that.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "gsc-mcp"
ENV_OVERRIDE = "GSC_MCP_HOME"


def config_dir() -> Path:
    """The root directory holding config, token, database and logs."""
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override)

    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"

    return root / APP_NAME


def ensure_config_dir() -> Path:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def db_path() -> Path:
    return config_dir() / "state.db"


def token_path() -> Path:
    return config_dir() / "token.json"


def config_path() -> Path:
    return config_dir() / "config.json"


def log_dir() -> Path:
    return config_dir() / "logs"
