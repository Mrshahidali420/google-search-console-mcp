"""Not one byte on stdout, from any tool, on any path.

stdout is the MCP JSON-RPC transport. A single stray character corrupts
every frame after it, and the failure reaches the user as an unrelated
parse error rather than as a bug report pointing at whatever printed. The
tools covered here are the local-only ones — they touch browser state
files, the filesystem and third-party libraries, none of which promise to
stay quiet — so they are the ones worth a standing guard.

The failure paths are tested as well as the happy ones: an exception
handler is exactly where a stray traceback print gets added.
"""
from __future__ import annotations

import pytest

from gsc_mcp import onboarding, tools_browsers


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    onboarding._reset_pending()
    yield
    onboarding._reset_pending()


def _boom(*args, **kwargs):
    raise RuntimeError("kaboom")


def test_gsc_detect_browsers_writes_nothing_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr(tools_browsers.profiles, "survey", lambda: [])
    tools_browsers.detect_browsers()
    assert capsys.readouterr().out == ""


def test_gsc_detect_browsers_writes_nothing_on_the_failure_path(monkeypatch,
                                                                capsys):
    monkeypatch.setattr(tools_browsers.profiles, "survey", _boom)
    assert tools_browsers.detect_browsers()["ok"] is False
    assert capsys.readouterr().out == ""


def test_gsc_setup_writes_nothing_to_stdout(monkeypatch, capsys):
    """The unconfigured path: no client, so no consent is started."""
    monkeypatch.delenv("GSC_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GSC_MCP_CLIENT_SECRET", raising=False)
    onboarding.setup(open_browser=False)
    assert capsys.readouterr().out == ""


def test_gsc_setup_writes_nothing_while_a_consent_is_pending(monkeypatch,
                                                             capsys):
    """The path that binds a loopback socket and builds a consent URL."""
    monkeypatch.setenv("GSC_MCP_CLIENT_ID", "client-id-123")
    monkeypatch.setenv("GSC_MCP_CLIENT_SECRET", "client-secret-456")
    onboarding.setup(open_browser=False)
    onboarding.setup(open_browser=False)
    assert capsys.readouterr().out == ""


def test_gsc_setup_writes_nothing_on_the_failure_path(monkeypatch, capsys):
    monkeypatch.setenv("GSC_MCP_CLIENT_ID", "client-id-123")
    monkeypatch.setenv("GSC_MCP_CLIENT_SECRET", "client-secret-456")
    monkeypatch.setattr(onboarding.gauth, "load_token",
                        lambda *a, **k: {"refresh_token": "rt",
                                         "access_token": "at"})
    monkeypatch.setattr(onboarding.gauth, "verify_token", lambda *a, **k: 3)
    monkeypatch.setattr(onboarding.profiles, "survey", _boom)
    assert onboarding.setup(open_browser=False)["ok"] is False
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Every registered tool, in one sweep
# ---------------------------------------------------------------------------

def registered_tools():
    """What the server actually exposes, read off the SDK's own tool registry.

    Not `dir(server)` filtered by a `gsc_` prefix. The prefix is a naming
    convention, not the registration mechanism: a tool registered under any
    other name would escape the sweep below and nobody would find out. The
    registry is the one place that knows what a client can call.
    """
    from gsc_mcp import server

    return {tool.name: tool.fn for tool in server.mcp._tool_manager.list_tools()}


#: Arguments that need no network and no real browser, one entry per
#: registered tool. Keyed by name so the assertion below can say WHICH tool
#: printed — a bare "something wrote to stdout" over eight calls is a bug
#: report nobody can act on — and so a newly registered tool fails
#: `test_the_sweep_covers_every_tool_the_server_registers` loudly rather
#: than being skipped.
_TOOL_ARGS = {
    "gsc_list_sites": ((), {}),
    "gsc_doctor": ((), {}),
    "gsc_check_status": ((["https://example.com/"],), {}),
    "gsc_quota": ((), {}),
    "gsc_performance": (("sc-domain:example.com",), {}),
    "gsc_submit_sitemaps": ((["https://example.com/sitemap.xml"],), {}),
    "gsc_detect_browsers": ((), {}),
    # Called with nothing to pin, which refuses before it writes: the sweep
    # runs against a throwaway home, but a tool that saved a config file as
    # a side effect of a stdout check would be doing it on the developer's
    # machine the first time the fixture changed.
    "gsc_use_browser": ((), {}),
    "gsc_setup": ((), {"open_browser": False}),
    "gsc_request_indexing": ((["https://example.com/a"],), {}),
    "gsc_start_indexing_job": ((["https://example.com/a"],), {}),
    "gsc_job_status": ((), {}),
    "gsc_stop_job": (("no-such-job",), {}),
    "gsc_find_unindexed": (("sc-domain:example.com",), {}),
    "gsc_audit": (("sc-domain:example.com",), {}),
}

_SWEPT = sorted(registered_tools())


@pytest.mark.parametrize("name", _SWEPT, ids=_SWEPT)
def test_every_registered_tool_writes_nothing_to_stdout(name, home, monkeypatch,
                                                        capsys):
    """The standing guard the whole milestone's definition of done names.

    Every tool runs unconfigured, which is the state a fresh install is in
    and the state most likely to send something down an error path. No
    OAuth client means no provider, so nothing here reaches the network.

    `home` is not decoration. Since the submission tools joined the sweep,
    a tool that got as far as the real store would find the developer's own
    properties, and gsc_start_indexing_job would answer by opening a real
    browser and spending real quota. Against a throwaway home there are no
    properties, so each one refuses at its own guard instead.
    """
    from gsc_mcp import server

    monkeypatch.delenv("GSC_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GSC_MCP_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_ID", "")
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_SECRET", "")
    # One patch covers both tools that survey: onboarding and
    # tools_browsers hold the same gsc_core.profiles module object.
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [])

    args, kwargs = _TOOL_ARGS[name]
    registered_tools()[name](*args, **kwargs)
    assert capsys.readouterr().out == "", f"{name} wrote to stdout"


def test_the_sweep_covers_every_tool_the_server_registers():
    """Guards the table above against the server growing a ninth tool.

    A stdout sweep that silently stops covering a new tool is worse than
    no sweep: it reads as a passing guard.
    """
    assert set(registered_tools()) == set(_TOOL_ARGS)


def test_the_sweep_would_see_a_tool_that_does_not_follow_the_naming_convention():
    """The reason this reads the registry and not `dir(server)`.

    A tool registered under a name without the `gsc_` prefix used to be
    invisible to the coverage check, so it never entered the sweep and its
    stray `print()` shipped. Registering one here proves discovery is by
    registration, not by spelling.
    """
    from gsc_mcp import server

    server.mcp.add_tool(lambda: {"ok": True}, name="helper_without_the_prefix")
    try:
        assert "helper_without_the_prefix" in registered_tools()
        with pytest.raises(AssertionError):
            test_the_sweep_covers_every_tool_the_server_registers()
    finally:
        server.mcp.remove_tool("helper_without_the_prefix")
    assert set(registered_tools()) == set(_TOOL_ARGS)
