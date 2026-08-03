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

#: All eight, called with arguments that need no network and no real
#: browser. Listed by name rather than by object so that the assertion
#: below can say WHICH tool printed — a bare "something wrote to stdout"
#: over eight calls is a bug report nobody can act on.
_ALL_TOOLS = [
    ("gsc_list_sites", lambda s: s.gsc_list_sites()),
    ("gsc_doctor", lambda s: s.gsc_doctor()),
    ("gsc_check_status", lambda s: s.gsc_check_status(["https://example.com/"])),
    ("gsc_quota", lambda s: s.gsc_quota()),
    ("gsc_performance", lambda s: s.gsc_performance("sc-domain:example.com")),
    ("gsc_submit_sitemaps",
     lambda s: s.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])),
    ("gsc_detect_browsers", lambda s: s.gsc_detect_browsers()),
    ("gsc_setup", lambda s: s.gsc_setup(open_browser=False)),
]


@pytest.mark.parametrize("name,call", _ALL_TOOLS, ids=[t[0] for t in _ALL_TOOLS])
def test_every_registered_tool_writes_nothing_to_stdout(name, call, monkeypatch,
                                                        capsys):
    """The standing guard the whole milestone's definition of done names.

    Every tool runs unconfigured, which is the state a fresh install is in
    and the state most likely to send something down an error path. No
    OAuth client means no provider, so nothing here reaches the network.
    """
    from gsc_mcp import server

    monkeypatch.delenv("GSC_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GSC_MCP_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_ID", "")
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_SECRET", "")
    # One patch covers both tools that survey: onboarding and
    # tools_browsers hold the same gsc_core.profiles module object.
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [])

    call(server)
    assert capsys.readouterr().out == "", f"{name} wrote to stdout"


def test_the_sweep_covers_every_tool_the_server_registers():
    """Guards the list above against the server growing a ninth tool.

    A stdout sweep that silently stops covering a new tool is worse than
    no sweep: it reads as a passing guard.
    """
    from gsc_mcp import server

    registered = {name for name in dir(server)
                  if name.startswith("gsc_") and callable(getattr(server, name))}
    assert registered == {name for name, _ in _ALL_TOOLS}
