"""gsc-mcp: the MCP server surface over gsc_core.

Everything an AI agent can call lives here. gsc_core stays the engine — it
is importable without `mcp` installed, so a future CLI or desktop app can
depend on it without pulling in this package. Nothing under gsc_core may
import from gsc_mcp; the dependency only ever points one way.
"""
from __future__ import annotations
