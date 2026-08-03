"""What the server calls itself during the MCP handshake.

`serverInfo` is the only thing a client learns about this package before it
calls anything, and the version in it is what a bug report will quote. The
SDK supplies its own version when the server does not set one, so the
failure mode here is not a crash or a blank field -- it is a plausible,
wrong number that looks like ours. That is why these tests assert the value
matches __version__ AND that it is not the SDK's.
"""
from __future__ import annotations

from importlib.metadata import version as pkg_version

import gsc_mcp
from gsc_mcp import server


def _server_info_version() -> str:
    """The version an initialize response would actually carry."""
    return server.mcp._mcp_server.create_initialization_options().server_version


def test_the_handshake_reports_this_package_version():
    assert _server_info_version() == gsc_mcp.__version__


def test_the_handshake_does_not_report_the_sdk_version():
    # The regression this file exists for: FastMCP takes no version
    # argument, so an unset version silently falls through to
    # importlib.metadata.version("mcp") inside the SDK. Asserting equality
    # with __version__ alone would not catch it if the two ever coincided,
    # and it would not explain what went wrong when it does.
    assert _server_info_version() != pkg_version("mcp")


def test_the_handshake_reports_the_package_name():
    assert server.mcp._mcp_server.name == "gsc-mcp"


def test_the_version_is_a_release_number_not_a_placeholder():
    # A single source of truth only helps if it holds a real value: the
    # packaging metadata reads this same attribute, so an empty or
    # placeholder string here becomes an unbuildable wheel.
    parts = gsc_mcp.__version__.split(".")
    assert len(parts) >= 2
    assert all(part and part[0].isdigit() for part in parts)
