"""gsc-mcp: the MCP server surface over gsc_core.

Everything an AI agent can call lives here. gsc_core stays the engine — it
is importable without `mcp` installed, so a future CLI or desktop app can
depend on it without pulling in this package. Nothing under gsc_core may
import from gsc_mcp; the dependency only ever points one way.
"""
from __future__ import annotations

# The single source of truth for this package's version. pyproject.toml
# reads it from here (tool.hatch.version), and server.py stamps it into the
# MCP handshake, so the number a client reports and the number on the wheel
# cannot drift apart. Keeping it in __init__ rather than in pyproject also
# means it is readable without the package being installed, which is how
# this repo is run during development.
__version__ = "0.1.0"
