import json
import threading
from datetime import datetime, timedelta, UTC

import pytest

from gsc_core import gauth
from gsc_mcp import deps


def test_oauth_client_prefers_environment_variables(monkeypatch):
    # Both env and embedded are set to DIFFERENT non-empty values, so the
    # assertion below can only pass if the environment actually wins the
    # precedence, not merely because the embedded side happens to be
    # empty (which it is everywhere before D1 populates it — the gap this
    # test exists to pin starts to matter exactly when that changes).
    monkeypatch.setenv(deps.CLIENT_ID_ENV, "env-id")
    monkeypatch.setenv(deps.CLIENT_SECRET_ENV, "env-secret")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "embedded-id")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "embedded-secret")
    assert deps.oauth_client() == ("env-id", "env-secret")


def test_oauth_client_raises_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv(deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "")
    with pytest.raises(deps.NotConfigured):
        deps.oauth_client()


def test_no_real_client_secret_is_committed():
    """A public repo must never carry a live OAuth secret."""
    assert deps.EMBEDDED_CLIENT_SECRET == ""


def test_connection_is_a_fresh_object_each_call(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    with deps.connection() as first, deps.connection() as second:
        assert first is not second


# ------------------------------------------------------- the shared provider
#
# Google rotates the refresh token on use. Concurrent refreshes each spend
# the same one, and every loser persists a refresh token Google has already
# retired -- the stored credential ends up dead and the user re-authorises.
# TokenProvider's lock stops that, but only across calls it can actually see,
# which means one instance has to cover them all.

def _signed_in(tmp_path, monkeypatch):
    """A configured client and a stale-but-refreshable token on disk."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    monkeypatch.setenv(deps.CLIENT_ID_ENV, "id")
    monkeypatch.setenv(deps.CLIENT_SECRET_ENV, "secret")
    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "access_token": "stale", "refresh_token": "rt",
        "expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    }), encoding="utf-8")
    return token


def test_provider_is_shared_across_calls(tmp_path, monkeypatch):
    _signed_in(tmp_path, monkeypatch)
    assert deps.provider() is deps.provider()


def test_concurrent_tool_calls_trigger_one_refresh_not_n(tmp_path, monkeypatch):
    """The guarantee TokenProvider documents, exercised where it matters.

    Five tool calls refreshing at once must spend the refresh token ONCE.
    With a provider constructed per call this was five refreshes, four of
    them writing back a credential Google had already rotated away.
    """
    _signed_in(tmp_path, monkeypatch)

    # list.append is atomic under the GIL, so no lock is needed here -- and
    # must not be used: a lock held across access_token() would be re-entered
    # by the refresh below and deadlock the test rather than fail it.
    refreshed_with: list[str] = []

    def fake_post_token(_session, payload):
        refreshed_with.append(payload["refresh_token"])
        return {"access_token": "fresh", "expires_in": 3599}

    monkeypatch.setattr(gauth, "_post_token", fake_post_token)

    callers = 5
    ready = threading.Barrier(callers)
    tokens: list[str] = []

    def one_tool_call() -> None:
        # Each "tool call" asks deps for a provider exactly as a tool does.
        active = deps.provider()
        ready.wait()   # every caller arrives at access_token() together
        tokens.append(active.access_token())

    threads = [threading.Thread(target=one_tool_call) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(refreshed_with) == 1, (
        f"{len(refreshed_with)} refreshes for {callers} concurrent calls; "
        "every one after the first spends a rotated refresh token")
    assert tokens == ["fresh"] * callers


def test_a_changed_client_gets_a_different_provider(tmp_path, monkeypatch):
    """The cache is keyed on what the provider was built from, so a test (or
    a redeployed server) that changes the client does not keep the old one."""
    _signed_in(tmp_path, monkeypatch)
    first = deps.provider()
    monkeypatch.setenv(deps.CLIENT_ID_ENV, "other-id")
    assert deps.provider() is not first


def test_a_changed_token_path_gets_a_different_provider(tmp_path, monkeypatch):
    _signed_in(tmp_path, monkeypatch)
    first = deps.provider()
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path / "elsewhere"))
    assert deps.provider() is not first
