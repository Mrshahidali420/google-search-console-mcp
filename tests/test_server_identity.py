"""What the server calls itself during the MCP handshake.

`serverInfo` is the only thing a client learns about this package before it
calls anything, and the version in it is what a bug report will quote. The
SDK supplies its own version when the server does not set one, so the
failure mode here is not a crash or a blank field -- it is a plausible,
wrong number that looks like ours. That is why these tests assert the value
matches __version__ AND that it is not the SDK's.

Read over the wire, not off an internal attribute: mcp 2.0's Client hands
back the very `serverInfo` an initialize response carried, which is the
thing these tests are about. (Under mcp 1.x this file reached into
`_mcp_server.create_initialization_options()` because FastMCP took no
version argument and the stamp itself was a private-attribute hack; both
ends of that are gone.)
"""
from __future__ import annotations

import asyncio
from importlib.metadata import version as pkg_version

from mcp.client import Client

import gsc_mcp
from gsc_mcp import server


def _server_info():
    """The serverInfo an initialize response actually carried."""
    async def handshake():
        async with Client(server.mcp) as client:
            return client.server_info
    return asyncio.run(handshake())


def test_the_handshake_reports_this_package_version():
    assert _server_info().version == gsc_mcp.__version__


def test_the_handshake_does_not_report_the_sdk_version():
    # The regression this file exists for: an unset version silently falls
    # through to the SDK's own number inside the lowlevel server. Asserting
    # equality with __version__ alone would not catch it if the two ever
    # coincided, and it would not explain what went wrong when it does.
    assert _server_info().version != pkg_version("mcp")


def test_the_handshake_reports_the_package_name():
    assert _server_info().name == "gsc-mcp"


def test_the_version_is_a_release_number_not_a_placeholder():
    # A single source of truth only helps if it holds a real value: the
    # packaging metadata reads this same attribute, so an empty or
    # placeholder string here becomes an unbuildable wheel.
    parts = gsc_mcp.__version__.split(".")
    assert len(parts) >= 2
    assert all(part and part[0].isdigit() for part in parts)
