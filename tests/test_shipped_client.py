"""The first-run download of the shipped OAuth client.

Two things these tests care about beyond "does it work". First, that every
failure mode arrives as FetchFailed carrying a message safe to show a user
— the whole point of downloading from a URL we control is that the thing
on the other end may one day not be what we expect. Second, that the
client secret never reaches a log record or an exception message, which is
the same rule the token path is held to.
"""
from __future__ import annotations

import json
import stat
import sys

import pytest
import requests

from gsc_core import paths
from gsc_mcp import deps, shipped_client

# Assembled rather than written whole so this file does not trip the
# committed-secret scan in test_embedded_client.py.
SECRET = "GOCSPX" + "-testonlynotreal"
CLIENT_ID = "1234-abc.apps.googleusercontent.com"
GOOD_BODY = json.dumps({"client_id": CLIENT_ID, "client_secret": SECRET})


class FakeResponse:
    """The slice of requests.Response that _download actually uses."""

    def __init__(self, body="", status=200, url=shipped_client.CLIENT_URL,
                 history=()):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status_code = status
        self.url = url
        self.history = history
        self.chunks_read = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            self.chunks_read += 1
            yield self.body[start:start + chunk_size]


class FakeHop:
    def __init__(self, url):
        self.url = url


@pytest.fixture()
def served(monkeypatch):
    """Serve a canned response, and hand the test the recorded request."""
    def install(response):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(shipped_client.requests, "get", fake_get)
        return calls

    return install


# ---------------------------------------------------------------------------
# The URL itself
# ---------------------------------------------------------------------------

def test_url_is_https():
    assert shipped_client.CLIENT_URL.startswith("https://")


def test_url_hangs_off_a_fixed_tag_not_latest():
    """A `latest` URL would break every first run after a release that
    forgot to re-upload the asset. The dedicated tag decouples the two."""
    assert "/releases/download/client/" in shipped_client.CLIENT_URL
    assert "/latest/" not in shipped_client.CLIENT_URL


# ---------------------------------------------------------------------------
# Reading the cache
# ---------------------------------------------------------------------------

def test_cached_is_empty_when_no_file_exists(home):
    assert shipped_client.cached() == ("", "")


def test_cached_returns_a_saved_client(home):
    shipped_client._cache(CLIENT_ID, SECRET)
    assert shipped_client.cached() == (CLIENT_ID, SECRET)


def test_cached_survives_a_corrupt_file(home):
    paths.ensure_config_dir()
    paths.client_path().write_text("{not json", encoding="utf-8")
    assert shipped_client.cached() == ("", "")


def test_cached_survives_a_file_of_the_wrong_shape(home):
    paths.ensure_config_dir()
    paths.client_path().write_text('["a", "list"]', encoding="utf-8")
    assert shipped_client.cached() == ("", "")


def test_cached_rejects_non_string_fields(home):
    paths.ensure_config_dir()
    paths.client_path().write_text(
        json.dumps({"client_id": 5, "client_secret": None}), encoding="utf-8")
    assert shipped_client.cached() == ("", "")


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

def test_fetch_returns_and_caches_the_client(home, served):
    served(FakeResponse(GOOD_BODY))
    assert shipped_client.fetch_and_cache() == (CLIENT_ID, SECRET)
    # Cached, so the next run never touches the network.
    assert shipped_client.cached() == (CLIENT_ID, SECRET)


def test_fetch_uses_a_timeout(home, served):
    """No timeout means a hung server hangs gsc_setup forever."""
    calls = served(FakeResponse(GOOD_BODY))
    shipped_client.fetch_and_cache()
    assert calls[0][1]["timeout"] > 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
def test_cached_client_is_owner_readable_only(home, served):
    served(FakeResponse(GOOD_BODY))
    shipped_client.fetch_and_cache()
    assert stat.S_IMODE(paths.client_path().stat().st_mode) == 0o600


def test_fetch_leaves_no_temp_file_behind(home, served):
    served(FakeResponse(GOOD_BODY))
    shipped_client.fetch_and_cache()
    assert [p.name for p in home.iterdir()] == ["client.json"]


# ---------------------------------------------------------------------------
# Every way it can fail
# ---------------------------------------------------------------------------

def test_network_failure_becomes_fetch_failed(home, served):
    served(requests.ConnectionError("connect to https://user:pw@proxy failed"))
    with pytest.raises(shipped_client.FetchFailed) as caught:
        shipped_client.fetch_and_cache()
    # The underlying message can carry the URL and proxy credentials from
    # the environment; ours must not repeat it.
    assert "proxy" not in str(caught.value)


def test_network_failure_does_not_chain_the_original(home, served):
    """`from None`: a chained traceback would print the requests message,
    which is the thing the check above exists to keep out of the log."""
    served(requests.ConnectionError("connect to https://user:pw@proxy failed"))
    with pytest.raises(shipped_client.FetchFailed) as caught:
        shipped_client.fetch_and_cache()
    assert caught.value.__cause__ is None


def test_http_error_status_becomes_fetch_failed(home, served):
    served(FakeResponse("Not Found", status=404))
    with pytest.raises(shipped_client.FetchFailed, match="404"):
        shipped_client.fetch_and_cache()


def test_an_insecure_redirect_hop_is_refused(home, served):
    """requests follows https -> http happily, which would put the client
    secret on the wire in the clear."""
    served(FakeResponse(GOOD_BODY,
                        history=(FakeHop("http://cdn.example.invalid/c.json"),)))
    with pytest.raises(shipped_client.FetchFailed, match="insecure"):
        shipped_client.fetch_and_cache()


def test_an_insecure_final_url_is_refused(home, served):
    served(FakeResponse(GOOD_BODY, url="http://cdn.example.invalid/c.json"))
    with pytest.raises(shipped_client.FetchFailed, match="insecure"):
        shipped_client.fetch_and_cache()


def test_a_secure_redirect_chain_is_allowed(home, served):
    """GitHub really does redirect release assets to object storage."""
    served(FakeResponse(
        GOOD_BODY,
        url="https://objects.githubusercontent.com/c.json",
        history=(FakeHop("https://github.com/o/r/releases/download/client/c.json"),),
    ))
    assert shipped_client.fetch_and_cache() == (CLIENT_ID, SECRET)


def test_an_oversized_body_is_refused(home, served):
    served(FakeResponse("x" * (shipped_client._MAX_BYTES + 100)))
    with pytest.raises(shipped_client.FetchFailed, match="larger"):
        shipped_client.fetch_and_cache()


def test_an_oversized_body_stops_being_read_early(home, served):
    """The cap has to bite WHILE reading. Checking len(response.content)
    would mean the whole body was already in memory before it was judged."""
    response = FakeResponse("x" * (shipped_client._MAX_BYTES * 50))
    served(response)
    with pytest.raises(shipped_client.FetchFailed):
        shipped_client.fetch_and_cache()
    assert response.chunks_read < shipped_client._MAX_BYTES * 50 / 1024


def test_a_non_json_body_becomes_fetch_failed(home, served):
    served(FakeResponse("<html>captive portal</html>"))
    with pytest.raises(shipped_client.FetchFailed, match="JSON"):
        shipped_client.fetch_and_cache()


def test_undecodable_bytes_become_fetch_failed(home, served):
    served(FakeResponse(b"\xff\xfe\x00garbage"))
    with pytest.raises(shipped_client.FetchFailed):
        shipped_client.fetch_and_cache()


def test_a_json_body_of_the_wrong_shape_is_refused(home, served):
    served(FakeResponse('"a string"'))
    with pytest.raises(shipped_client.FetchFailed, match="shape"):
        shipped_client.fetch_and_cache()


def test_missing_fields_are_refused(home, served):
    served(FakeResponse(json.dumps({"client_id": CLIENT_ID})))
    with pytest.raises(shipped_client.FetchFailed, match="missing"):
        shipped_client.fetch_and_cache()


def test_empty_fields_are_refused(home, served):
    """The generator refuses to write an empty value, but a hand-edited
    asset is exactly the case this download cannot assume away."""
    served(FakeResponse(json.dumps({"client_id": CLIENT_ID,
                                    "client_secret": ""})))
    with pytest.raises(shipped_client.FetchFailed, match="empty"):
        shipped_client.fetch_and_cache()


def test_a_client_id_that_is_not_googles_is_refused(home, served):
    """Without this the bad value caches, and the failure surfaces much
    later as an opaque invalid_client from Google mid-consent."""
    served(FakeResponse(json.dumps({"client_id": "<!DOCTYPE html>",
                                    "client_secret": SECRET})))
    with pytest.raises(shipped_client.FetchFailed, match="Google client id"):
        shipped_client.fetch_and_cache()


def test_a_rejected_response_is_never_cached(home, served):
    served(FakeResponse(json.dumps({"client_id": "nope",
                                    "client_secret": SECRET})))
    with pytest.raises(shipped_client.FetchFailed):
        shipped_client.fetch_and_cache()
    assert not paths.client_path().exists()
    assert shipped_client.cached() == ("", "")


def test_a_fetched_value_is_never_echoed_in_the_error(home, served):
    """Failure messages are shown to the user through gsc_setup. Echoing
    the body would put arbitrary fetched text into their client."""
    served(FakeResponse(json.dumps({"client_id": "PWNED-MARKER",
                                    "client_secret": SECRET})))
    with pytest.raises(shipped_client.FetchFailed) as caught:
        shipped_client.fetch_and_cache()
    assert "PWNED-MARKER" not in str(caught.value)
    assert SECRET not in str(caught.value)


# ---------------------------------------------------------------------------
# Caching is best-effort
# ---------------------------------------------------------------------------

def test_a_cache_write_failure_still_returns_the_client(home, served,
                                                        monkeypatch):
    """A read-only home should mean "download again next time", not a
    broken setup: the caller already holds a usable client."""
    def explode(data, target):
        raise OSError(f"read-only: {target}")

    monkeypatch.setattr(shipped_client.gauth, "write_private_json", explode)
    served(FakeResponse(GOOD_BODY))
    assert shipped_client.fetch_and_cache() == (CLIENT_ID, SECRET)


def test_a_cache_write_failure_logs_no_path_or_secret(home, served,
                                                      monkeypatch, caplog):
    def explode(data, target):
        raise OSError(f"read-only: {target}")

    monkeypatch.setattr(shipped_client.gauth, "write_private_json", explode)
    served(FakeResponse(GOOD_BODY))
    with caplog.at_level("DEBUG", logger="gsc_mcp.shipped_client"):
        shipped_client.fetch_and_cache()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET not in logged
    assert str(home) not in logged
    assert "OSError" in logged


def test_the_secret_never_reaches_a_log_record(home, served, caplog):
    served(FakeResponse(GOOD_BODY))
    with caplog.at_level("DEBUG"):
        shipped_client.fetch_and_cache()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET not in logged


# ---------------------------------------------------------------------------
# How deps resolves it
# ---------------------------------------------------------------------------

def test_oauth_client_uses_the_cached_client(home, monkeypatch):
    monkeypatch.delenv(deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(deps, "_cached_client", lambda: (CLIENT_ID, SECRET))
    assert deps.oauth_client() == (CLIENT_ID, SECRET)


def test_the_environment_beats_the_cached_client(home, monkeypatch):
    monkeypatch.setenv(deps.CLIENT_ID_ENV, "env-id")
    monkeypatch.setenv(deps.CLIENT_SECRET_ENV, "env-secret")
    monkeypatch.setattr(deps, "_cached_client", lambda: (CLIENT_ID, SECRET))
    assert deps.oauth_client() == ("env-id", "env-secret")


def test_the_embedded_client_beats_the_cached_client(home, monkeypatch):
    """A release build carries a client; a stale cache from an earlier
    source checkout must not shadow it."""
    monkeypatch.delenv(deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "built-id")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "built-secret")
    monkeypatch.setattr(deps, "_cached_client", lambda: (CLIENT_ID, SECRET))
    assert deps.oauth_client() == ("built-id", "built-secret")


def test_half_an_environment_falls_through_whole(home, monkeypatch):
    """One variable exported and one forgotten is the common mistake.
    Pairing the set half with a cached half would build a client that
    cannot authenticate and fail deep inside the OAuth flow."""
    monkeypatch.setenv(deps.CLIENT_ID_ENV, "env-id")
    monkeypatch.delenv(deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(deps, "_cached_client", lambda: (CLIENT_ID, SECRET))
    assert deps.oauth_client() == (CLIENT_ID, SECRET)


def test_oauth_client_never_reaches_the_network(home, monkeypatch):
    """It is called by provider() on every tool call and three times by
    gsc_doctor. A round trip here would let a DNS timeout hang any tool."""
    def forbidden(*args, **kwargs):
        raise AssertionError("oauth_client() must not perform a request")

    monkeypatch.setattr(shipped_client.requests, "get", forbidden)
    monkeypatch.delenv(deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(deps, "_cached_client", shipped_client.cached)
    with pytest.raises(deps.NotConfigured):
        deps.oauth_client()
