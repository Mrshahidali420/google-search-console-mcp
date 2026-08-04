import json
import os
import stat
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

from gsc_core import gauth


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def post(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data})
        return FakeResponse(self.payload, self.status)


def test_save_and_load_token_round_trip(tmp_path):
    target = tmp_path / "token.json"
    gauth.save_token({"refresh_token": "r1", "access_token": "a1"}, target)
    assert gauth.load_token(target) == {"refresh_token": "r1",
                                        "access_token": "a1"}


def test_load_token_returns_none_when_absent(tmp_path):
    assert gauth.load_token(tmp_path / "nothing.json") is None


def test_load_token_returns_none_on_corrupt_file(tmp_path):
    target = tmp_path / "token.json"
    target.write_text("{ not json", encoding="utf-8")
    assert gauth.load_token(target) is None


def test_exchange_code_sends_the_verifier(tmp_path):
    session = FakeSession({"access_token": "a", "refresh_token": "r",
                           "expires_in": 3599})
    result = gauth.exchange_code(
        client_id="cid", client_secret="secret", code="the-code",
        verifier="the-verifier", redirect_uri="http://127.0.0.1:1",
        session=session,
    )
    sent = session.calls[0]["data"]
    assert sent["code_verifier"] == "the-verifier"
    assert sent["grant_type"] == "authorization_code"
    assert result["refresh_token"] == "r"


def test_exchange_code_raises_on_error_status():
    session = FakeSession({"error": "invalid_grant"}, status=400)
    with pytest.raises(RuntimeError, match="invalid_grant"):
        gauth.exchange_code("cid", "secret", "code", "verifier",
                            "http://127.0.0.1:1", session=session)


def test_provider_returns_cached_token_before_expiry(tmp_path):
    target = tmp_path / "token.json"
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    gauth.save_token(
        {"refresh_token": "r", "access_token": "cached", "expires_at": future},
        target,
    )
    session = FakeSession({"access_token": "refreshed", "expires_in": 3599})
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)
    assert provider.access_token() == "cached"
    assert session.calls == []


def test_provider_refreshes_when_token_expired(tmp_path):
    target = tmp_path / "token.json"
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    gauth.save_token(
        {"refresh_token": "r", "access_token": "stale", "expires_at": past},
        target,
    )
    session = FakeSession({"access_token": "refreshed", "expires_in": 3599})
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)
    assert provider.access_token() == "refreshed"
    assert session.calls[0]["data"]["grant_type"] == "refresh_token"


def test_provider_refresh_preserves_refresh_token(tmp_path):
    target = tmp_path / "token.json"
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    gauth.save_token(
        {"refresh_token": "keep-me", "access_token": "stale",
         "expires_at": past},
        target,
    )
    session = FakeSession({"access_token": "refreshed", "expires_in": 3599})
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)
    provider.access_token()
    assert gauth.load_token(target)["refresh_token"] == "keep-me"


def test_invalidate_forces_a_refresh(tmp_path):
    target = tmp_path / "token.json"
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    gauth.save_token(
        {"refresh_token": "r", "access_token": "cached", "expires_at": future},
        target,
    )
    session = FakeSession({"access_token": "refreshed", "expires_in": 3599})
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)
    provider.access_token()
    provider.invalidate()
    assert provider.access_token() == "refreshed"


def test_provider_raises_when_no_token_stored(tmp_path):
    provider = gauth.TokenProvider("cid", "secret",
                                   token_path=tmp_path / "absent.json")
    with pytest.raises(gauth.AuthRequired):
        provider.access_token()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_token_file_is_owner_readable_only(tmp_path):
    target = tmp_path / "token.json"
    gauth.save_token({"refresh_token": "r"}, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_no_world_readable_window_during_write(tmp_path, monkeypatch):
    """The temp file must be 0600 from creation, not chmod'd afterwards."""
    seen = []
    real_fdopen = os.fdopen

    def spy(handle, *args, **kwargs):
        for candidate in tmp_path.iterdir():
            seen.append(stat.S_IMODE(candidate.stat().st_mode))
        return real_fdopen(handle, *args, **kwargs)

    monkeypatch.setattr(gauth.os, "fdopen", spy)
    gauth.save_token({"refresh_token": "r"}, tmp_path / "token.json")
    assert seen and all(mode == 0o600 for mode in seen)


def test_leaves_no_temp_file_behind(tmp_path):
    gauth.save_token({"refresh_token": "r"}, tmp_path / "token.json")
    assert [p.name for p in tmp_path.iterdir()] == ["token.json"]


def test_revoked_refresh_token_raises_auth_required(tmp_path):
    target = tmp_path / "token.json"
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    gauth.save_token({"refresh_token": "r", "access_token": "stale",
                      "expires_at": past}, target)
    session = FakeSession({"error": "invalid_grant"}, status=400)
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)
    with pytest.raises(gauth.AuthRequired):
        provider.access_token()


def test_non_json_error_body_raises_runtime_error(tmp_path):
    class HtmlResponse:
        status_code = 502
        text = "<html>Bad Gateway</html>"

        def json(self):
            raise ValueError("not json")

    class HtmlSession:
        def post(self, url, data=None, timeout=None):
            return HtmlResponse()

    with pytest.raises(RuntimeError, match="502"):
        gauth.exchange_code("cid", "secret", "code", "verifier",
                            "http://127.0.0.1:1", session=HtmlSession())


def test_error_message_never_carries_the_response_body():
    class LeakyResponse:
        status_code = 400
        text = "SECRET-BODY-CONTENT"

        def json(self):
            return {"unexpected": "shape"}

    class LeakySession:
        def post(self, url, data=None, timeout=None):
            return LeakyResponse()

    with pytest.raises(RuntimeError) as caught:
        gauth.exchange_code("cid", "secret", "code", "verifier",
                            "http://127.0.0.1:1", session=LeakySession())
    assert "SECRET-BODY-CONTENT" not in str(caught.value)


def test_naive_expires_at_degrades_to_not_fresh(tmp_path):
    target = tmp_path / "token.json"
    naive = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    gauth.save_token({"refresh_token": "r", "access_token": "stale",
                      "expires_at": naive.isoformat()}, target)
    session = FakeSession({"access_token": "refreshed", "expires_in": 3599})
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)
    assert provider.access_token() == "refreshed"


def test_save_token_hardens_the_file_before_writing(tmp_path, monkeypatch):
    """Runs on every platform. The POSIX mode assertions are skipped on
    Windows, so without this, deleting _harden entirely goes unnoticed here.
    """
    calls = []
    real_harden = gauth._harden

    def spy(path):
        calls.append((Path(path).name, Path(path).exists(),
                      Path(path).stat().st_size))
        return real_harden(path)

    monkeypatch.setattr(gauth, "_harden", spy)
    gauth.save_token({"refresh_token": "r"}, tmp_path / "token.json")

    assert calls, "_harden was never called"
    name, existed, size = calls[0]
    assert name.startswith(".token-")
    assert existed
    assert size == 0, "hardening must happen before the token is written"


def test_refresh_with_no_access_token_raises_and_does_not_persist(tmp_path):
    """Low fix: a 200 body missing access_token must raise before the stale
    token on disk is overwritten with an unusable one."""
    target = tmp_path / "token.json"
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    gauth.save_token({"refresh_token": "r", "access_token": "stale",
                      "expires_at": past}, target)
    session = FakeSession({})
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)
    with pytest.raises(RuntimeError, match="no access_token"):
        provider.access_token()
    assert gauth.load_token(target)["access_token"] == "stale"


def test_concurrent_access_token_calls_refresh_exactly_once(tmp_path):
    """api.check_status() calls access_token() from every worker at once.

    Google rotates the refresh token on use, so two threads refreshing
    concurrently means the loser persists a token Google has already retired
    and the stored credential is dead. Refresh must be single-flight.
    """
    import threading

    target = tmp_path / "token.json"
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    gauth.save_token(
        {"refresh_token": "r", "access_token": "stale", "expires_at": past},
        target,
    )

    # The Barrier is what gives this test its teeth: eight threads released
    # into access_token() at once. No artificial delay in the token endpoint
    # is needed, and adding one would be a real sleep in a suite that must
    # not have any -- measured 12/12 red without the lock and 12/12 green
    # with it, on the barrier alone.
    session = FakeSession({"access_token": "refreshed", "expires_in": 3599})
    provider = gauth.TokenProvider("cid", "secret", token_path=target,
                                   session=session)

    workers = 8
    ready = threading.Barrier(workers)
    tokens: list[str] = []
    failures: list[BaseException] = []

    def worker():
        try:
            ready.wait(timeout=5)
            tokens.append(provider.access_token())
        except BaseException as exc:  # noqa: BLE001 — reported below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not failures
    assert len(session.calls) == 1, "the refresh must be single-flight"
    assert tokens == ["refreshed"] * workers
    assert gauth.load_token(target)["refresh_token"] == "r"


# --- the sign-in advice must name something that exists (B5) -----------------
#
# Both AuthRequired paths told the user to "run gsc_setup()". No such tool is
# registered -- it is a Plan 3 entry on the roadmap -- so the one message a
# signed-out user is guaranteed to see sent them to a dead end. These pin the
# advice to tools that exist today; a future gsc_setup can update them.

_REAL_TOOLS = {"gsc_doctor", "gsc_list_sites", "gsc_check_status",
               "gsc_quota", "gsc_performance", "gsc_submit_sitemaps",
               "gsc_detect_browsers", "gsc_setup", "gsc_use_browser",
               "gsc_request_indexing", "gsc_start_indexing_job",
               "gsc_job_status", "gsc_stop_job",
               "gsc_find_unindexed", "gsc_audit"}


def _named_tools(message: str) -> set[str]:
    import re
    return set(re.findall(r"gsc_[a-z_]+", message))


def test_the_missing_credentials_message_names_only_real_tools(tmp_path):
    provider = gauth.TokenProvider("cid", "secret",
                                   token_path=tmp_path / "absent.json")
    with pytest.raises(gauth.AuthRequired) as caught:
        provider.access_token()
    named = _named_tools(str(caught.value))
    assert named <= _REAL_TOOLS, f"names a tool that does not exist: {named}"
    assert named, "the message should still point somewhere actionable"


def test_the_rejected_credentials_message_names_only_real_tools(tmp_path):
    target = tmp_path / "token.json"
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    gauth.save_token({"refresh_token": "r", "access_token": "stale",
                      "expires_at": past}, target)
    provider = gauth.TokenProvider(
        "cid", "secret", token_path=target,
        session=FakeSession({"error": "invalid_grant"}, status=400))
    with pytest.raises(gauth.AuthRequired) as caught:
        provider.access_token()
    named = _named_tools(str(caught.value))
    assert named <= _REAL_TOOLS, f"names a tool that does not exist: {named}"
    assert named


def test_the_advice_matches_the_tools_the_server_actually_registers():
    """_REAL_TOOLS above is a hand-written list, and a hand-written list
    drifts. This pins it to the real registration, so adding gsc_setup for
    real makes the messages above legal automatically -- and removing a tool
    the advice names makes them fail."""
    import inspect

    from gsc_mcp import server

    registered = {name for name, obj in vars(server).items()
                  if name.startswith("gsc_")
                  and (inspect.isfunction(obj) or hasattr(obj, "fn"))}
    assert _REAL_TOOLS == registered
