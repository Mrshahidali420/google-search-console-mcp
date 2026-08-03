"""Wire-level smoke test: does the server actually speak MCP?

Every other test in this suite calls gsc_* tool functions directly as plain
Python. None of them proves the server starts, registers its tools with
FastMCP, and answers a real MCP client over a real transport. That gap
matters because the README makes exactly that claim to strangers.

`FastMCP.list_tools()` (the shape suggested in the task brief) was
considered and rejected: it calls straight into the in-process
`ToolManager`, bypassing JSON-RPC entirely, so it cannot detect a broken
transport, a bad initialization handshake, or a tool that fails to survive
serialization. `mcp.shared.memory.create_connected_server_and_client_session`
is used instead — it runs the real `Server.run()` loop against a real
`ClientSession` over in-memory streams, so `client_session.list_tools()`
travels through the same JSON-RPC path a real client (Claude Desktop, an
MCP Inspector, anything else) would use. No subprocess/stdio fallback was
needed: this transport is available in the installed mcp 1.29.0.
"""
from __future__ import annotations

import asyncio

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

EXPECTED = {
    "gsc_list_sites", "gsc_doctor", "gsc_check_status",
    "gsc_quota", "gsc_performance", "gsc_submit_sitemaps",
    "gsc_detect_browsers", "gsc_setup",
    # Plan 3, browser-driven submission — registered in its final task.
    "gsc_request_indexing", "gsc_start_indexing_job",
    "gsc_job_status", "gsc_stop_job",
}


async def _list_tools_over_the_wire() -> list[types.Tool]:
    from gsc_mcp import server

    async with create_connected_server_and_client_session(server.mcp) as session:
        result = await session.list_tools()
    return result.tools


def test_every_tool_is_registered_and_described(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    tools = asyncio.run(_list_tools_over_the_wire())
    names = {tool.name for tool in tools}
    # Equality, not a subset check: the claim this test makes is "exactly
    # these tools ship". A subset check (EXPECTED <= names) would pass
    # even if a seventh, unplanned tool were accidentally registered under
    # any name — including one from a later plan that leaked in early.
    assert names == EXPECTED
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def test_no_tool_description_names_a_tool_that_does_not_exist(tmp_path,
                                                              monkeypatch):
    """B5's defect, one layer out. gauth's sign-in advice pointed at
    gsc_setup(), which is not registered; the guard added for that only
    reads gauth's messages, while two tool docstrings were pointing at
    gsc_request_indexing the same way -- and a docstring is worse, because
    it travels over the wire as the tool's `description` and is the text an
    assistant plans against. Told to "call gsc_request_indexing instead",
    an assistant either invents a call that fails or picks the nearest
    thing, which for a URL-indexing request is the one tool the same
    paragraph is warning it off.

    Reading the descriptions back over the transport rather than the
    docstrings from source is deliberate: it is the delivered text that
    matters, and this catches a description supplied any other way too.
    """
    import re

    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    tools = asyncio.run(_list_tools_over_the_wire())
    names = {tool.name for tool in tools}

    for tool in tools:
        mentioned = set(re.findall(r"gsc_[a-z_]+", tool.description))
        unknown = mentioned - names
        assert not unknown, (
            f"{tool.name}'s description names {sorted(unknown)}, which "
            f"no registered tool provides")
