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
