import threading
import urllib.request
from urllib.parse import parse_qs, urlparse as _urlparse

import pytest

from gsc_core import gauth


def test_receiver_binds_loopback_only():
    """The security property is the bind address, not the string we format."""
    with gauth.LoopbackReceiver() as receiver:
        host, port = receiver._server.server_address[:2]
        assert host == "127.0.0.1"
        assert port > 0
        assert receiver.redirect_uri == f"http://127.0.0.1:{port}"


def test_receiver_generates_unpredictable_state():
    with gauth.LoopbackReceiver() as first:
        with gauth.LoopbackReceiver() as second:
            assert first.state != second.state
            assert len(first.state) >= 16


def test_receiver_captures_the_code():
    with gauth.LoopbackReceiver() as receiver:
        def visit():
            url = f"{receiver.redirect_uri}/?code=the-code&state={receiver.state}"
            urllib.request.urlopen(url, timeout=5).read()

        threading.Thread(target=visit, daemon=True).start()
        assert receiver.wait(timeout=10) == "the-code"


def test_receiver_rejects_mismatched_state():
    with gauth.LoopbackReceiver() as receiver:
        def visit():
            url = f"{receiver.redirect_uri}/?code=the-code&state=wrong"
            try:
                urllib.request.urlopen(url, timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=visit, daemon=True).start()
        with pytest.raises(gauth.ConsentFailed, match="state"):
            receiver.wait(timeout=10)


def test_receiver_surfaces_user_denial():
    with gauth.LoopbackReceiver() as receiver:
        def visit():
            url = f"{receiver.redirect_uri}/?error=access_denied&state={receiver.state}"
            try:
                urllib.request.urlopen(url, timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=visit, daemon=True).start()
        with pytest.raises(gauth.ConsentFailed, match="access_denied"):
            receiver.wait(timeout=10)


def test_receiver_times_out():
    with gauth.LoopbackReceiver() as receiver:
        with pytest.raises(gauth.ConsentFailed, match="timed out"):
            receiver.wait(timeout=0.5)


def test_consent_flow_sends_one_redirect_uri_to_both_calls(monkeypatch, tmp_path):
    """The token exchange must present the identical redirect_uri that was in
    the authorization URL. An earlier draft read it back after the socket had
    closed, which Google rejects.
    """
    seen = {}

    def fake_open(url):
        seen["auth_url"] = url
        parsed = _urlparse(url)
        query = parse_qs(parsed.query)
        target = query["redirect_uri"][0]
        state = query["state"][0]
        threading.Thread(
            target=lambda: urllib.request.urlopen(
                f"{target}/?code=the-code&state={state}", timeout=5
            ).read(),
            daemon=True,
        ).start()

    def fake_exchange(client_id, client_secret, code, verifier, redirect_uri,
                      *, session=None):
        seen["exchange_redirect_uri"] = redirect_uri
        seen["code"] = code
        return {"refresh_token": "r", "access_token": "a", "expires_at": "x"}

    monkeypatch.setattr(gauth.webbrowser, "open", fake_open)
    monkeypatch.setattr(gauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(gauth, "save_token", lambda data, path=None: None)

    token = gauth.run_consent_flow("cid", "secret")

    assert token["refresh_token"] == "r"
    assert seen["code"] == "the-code"
    auth_redirect = parse_qs(_urlparse(seen["auth_url"]).query)["redirect_uri"][0]
    assert seen["exchange_redirect_uri"] == auth_redirect


def test_a_favicon_request_does_not_break_consent(monkeypatch):
    """A browser probes /favicon.ico after navigating. That carries no state
    and must not be mistaken for a failed redirect.
    """
    with gauth.LoopbackReceiver() as receiver:
        urllib.request.urlopen(f"{receiver.redirect_uri}/favicon.ico",
                               timeout=5).read()
        threading.Thread(
            target=lambda: urllib.request.urlopen(
                f"{receiver.redirect_uri}/?code=the-code&state={receiver.state}",
                timeout=5,
            ).read(),
            daemon=True,
        ).start()
        assert receiver.wait(timeout=10) == "the-code"
