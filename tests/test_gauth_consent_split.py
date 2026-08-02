import threading

import pytest
from gsc_core import gauth


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

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


def _redirect(receiver, **params):
    """Drive the live loopback server the way Google's redirect would.

    The handler writes the HTTP response body and only sets `_received`
    afterward, in its `finally`. `wfile` is unbuffered, so `urlopen(...
    ).read()` can return to this caller before that flag is set -- a caller
    that immediately calls `poll()` would then race the handler and can
    observe `None` even though the redirect has already been written and
    read. Waiting on the event here (briefly -- the handler sets it
    microseconds after the write completes) makes every caller of this
    helper see the post-redirect state deterministically.
    """
    import urllib.request
    from urllib.parse import urlencode
    url = f"{receiver.redirect_uri}/?{urlencode(params)}"
    urllib.request.urlopen(url, timeout=5).read()
    receiver._received.wait(2)


def test_poll_returns_none_before_the_redirect_arrives():
    receiver = gauth.LoopbackReceiver()
    receiver.start()
    try:
        assert receiver.poll() is None
    finally:
        receiver.close()


def test_poll_returns_the_code_after_the_redirect_arrives():
    receiver = gauth.LoopbackReceiver()
    receiver.start()
    try:
        _redirect(receiver, state=receiver.state, code="auth-code-xyz")
        assert receiver.poll() == "auth-code-xyz"
    finally:
        receiver.close()


def test_poll_raises_on_state_mismatch():
    receiver = gauth.LoopbackReceiver()
    receiver.start()
    try:
        _redirect(receiver, state="not-the-state", code="auth-code-xyz")
        with pytest.raises(gauth.ConsentFailed):
            receiver.poll()
    finally:
        receiver.close()


def test_start_consent_returns_a_url_without_blocking():
    pending = gauth.start_consent("client-id-123")
    try:
        assert pending.auth_url.startswith(gauth.AUTH_ENDPOINT)
        assert pending.state in pending.auth_url
        assert pending.redirect_uri.startswith("http://127.0.0.1:")
    finally:
        pending.receiver.close()


def test_finish_consent_returns_none_while_still_waiting():
    pending = gauth.start_consent("client-id-123")
    try:
        assert gauth.finish_consent(pending, "client-secret",
                                client_id="client-id-123") is None
    finally:
        pending.receiver.close()


def test_the_auth_url_never_contains_the_pkce_verifier():
    """The verifier is the whole point of PKCE — it must stay local."""
    pending = gauth.start_consent("client-id-123")
    try:
        assert pending.verifier not in pending.auth_url
    finally:
        pending.receiver.close()


def test_close_before_start_returns_instead_of_hanging():
    """BaseServer.shutdown() blocks with no timeout on an event only
    serve_forever() ever sets. A receiver that was created (so start_consent
    could fail before start()) but never started must not deadlock close().

    Run on a background thread with a hard join timeout: a regression here
    hangs, and a hung thread must fail the test rather than the whole suite.
    """
    receiver = gauth.LoopbackReceiver()
    closer = threading.Thread(target=receiver.close, daemon=True)
    closer.start()
    closer.join(timeout=5)
    assert not closer.is_alive(), "close() hung on a never-started receiver"


def test_close_is_idempotent():
    """A second close() must be a genuine no-op, not just harmless.

    Without the _closed guard this still neither raises nor hangs -- a
    started server's shutdown()/server_close() tolerate a repeat call -- so
    asserting only "no exception" cannot fail and proves nothing. Spying on
    the underlying calls proves the guard actually short-circuits the
    second close() rather than merely surviving it.
    """
    receiver = gauth.LoopbackReceiver()
    receiver.start()

    shutdown_calls = []
    server_close_calls = []
    real_shutdown = receiver._server.shutdown
    real_server_close = receiver._server.server_close

    def spy_shutdown():
        shutdown_calls.append(1)
        real_shutdown()

    def spy_server_close():
        server_close_calls.append(1)
        real_server_close()

    receiver._server.shutdown = spy_shutdown
    receiver._server.server_close = spy_server_close

    receiver.close()
    receiver.close()

    assert shutdown_calls == [1], "shutdown() must run exactly once"
    assert server_close_calls == [1], "server_close() must run exactly once"


def test_finish_consent_closes_the_receiver_when_poll_raises():
    """Important-1 regression: a state mismatch or refused consent is a
    terminal outcome. finish_consent's own docstring promises the receiver
    closes on every terminal path, success or raise -- verify the socket and
    serving thread actually go away, not just that ConsentFailed propagates.
    """
    pending = gauth.start_consent("client-id-123")
    _redirect(pending.receiver, state="not-the-state", code="auth-code-xyz")

    with pytest.raises(gauth.ConsentFailed):
        gauth.finish_consent(pending, "client-secret",
                             client_id="client-id-123")

    assert pending.receiver._closed
    pending.receiver._thread.join(timeout=5)
    assert not pending.receiver._thread.is_alive(), (
        "the serving thread is still alive; the receiver was not closed")


def test_finish_consent_saves_the_token_and_closes_the_receiver_on_success(
        monkeypatch):
    """The main path: a landed code is exchanged, the token is saved, and
    the receiver is closed. Previously untested."""
    saved = {}
    monkeypatch.setattr(gauth, "save_token",
                        lambda token, path=None: saved.update(token))
    # This test's own FakeSession only fakes .post (the token exchange);
    # verify_token's own behaviour is covered separately in
    # tests/test_gauth_validation.py, so bypass it here rather than teach
    # this session to also fake .get.
    monkeypatch.setattr(gauth, "verify_token", lambda token, session=None: 1)

    pending = gauth.start_consent("client-id-123")
    _redirect(pending.receiver, state=pending.state, code="auth-code-xyz")

    session = FakeSession({"access_token": "a-token", "refresh_token": "r",
                           "expires_in": 3599})
    result = gauth.finish_consent(pending, "client-secret",
                                  client_id="client-id-123", session=session)

    assert result["access_token"] == "a-token"
    assert saved["access_token"] == "a-token"
    assert session.calls[0]["data"]["code"] == "auth-code-xyz"
    assert session.calls[0]["data"]["code_verifier"] == pending.verifier

    assert pending.receiver._closed
    pending.receiver._thread.join(timeout=5)
    assert not pending.receiver._thread.is_alive(), (
        "the serving thread is still alive; the receiver was not closed")
