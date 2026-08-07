"""Wire-level smoke test: does the server actually speak MCP?

Every other test in this suite calls gsc_* tool functions directly as plain
Python. None of them proves the server starts, registers its tools with
the SDK, and answers a real MCP client over a real transport. That gap
matters because the README makes exactly that claim to strangers.

`MCPServer.list_tools()` (the shape suggested in the task brief) was
considered and rejected: it calls straight into the in-process
`ToolManager`, bypassing JSON-RPC entirely, so it cannot detect a broken
transport, a bad initialization handshake, or a tool that fails to survive
serialization. `mcp.client.Client` handed the server object is used
instead — its in-memory transport runs the real server loop against a real
client session, so `client.list_tools()` travels through the same JSON-RPC
path a real client (Claude Desktop, an MCP Inspector, anything else) would
use. (mcp 1.x spelled this `create_connected_server_and_client_session`;
mcp 2.0 replaced it with the Client class.)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import types
from mcp.client import Client

EXPECTED = {
    "gsc_list_sites", "gsc_doctor", "gsc_check_status",
    "gsc_quota", "gsc_performance", "gsc_submit_sitemaps",
    "gsc_detect_browsers", "gsc_setup",
    # D8 — the operator's override of the detector's ranking.
    "gsc_use_browser",
    # Plan 3, browser-driven submission — registered in its final task.
    "gsc_request_indexing", "gsc_start_indexing_job",
    "gsc_job_status", "gsc_stop_job",
    # Plan 4, discovery and audit — registered in its final task.
    "gsc_find_unindexed", "gsc_audit",
}


async def _list_tools_over_the_wire() -> list[types.Tool]:
    from gsc_mcp import server

    async with Client(server.mcp) as client:
        result = await client.list_tools()
    return result.tools


def test_every_tool_is_registered_and_described(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    tools = asyncio.run(_list_tools_over_the_wire())
    names = {tool.name for tool in tools}
    # Equality, not a subset check: the claim this test makes is "exactly
    # these tools ship". A subset check (EXPECTED <= names) would pass
    # even if one more, unplanned tool were accidentally registered under
    # any name — including one from a later plan that leaked in early.
    # (Count-agnostic on purpose; a numbered version of this comment went
    # stale the first time the tool count grew.)
    assert names == EXPECTED
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


#: Spelled out because the README's Project status sentence is prose. Only
#: the counts this project could plausibly reach are listed; an unmapped
#: count fails with the reason rather than a KeyError.
_COUNT_WORDS = {12: "Twelve", 13: "Thirteen", 14: "Fourteen", 15: "Fifteen",
                16: "Sixteen", 17: "Seventeen", 18: "Eighteen"}


def test_the_readme_tool_count_matches_what_is_registered():
    """The README's own claim, checked against the registry above.

    Prose is the one place in this repo where a wrong statement ships
    silently: no test reads it, so a milestone that adds tools updates the
    table four lines below and leaves the count sentence stale. It has
    already happened once. EXPECTED is the authority, so this pins the
    sentence to it rather than to another hand-written number.
    """
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8")
    word = _COUNT_WORDS.get(len(EXPECTED))
    assert word, f"add {len(EXPECTED)} to _COUNT_WORDS, then fix the README"

    claim = [line for line in readme.splitlines()
             if "tools are registered on the server" in line]
    assert len(claim) == 1, "the README's tool-count sentence moved or split"
    assert claim[0].startswith(f"{word} tools are registered on the server"), (
        f"README claims a different tool count; {len(EXPECTED)} are registered"
    )


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
