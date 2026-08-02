import pytest
from gsc_core import gauth


def _redirect(receiver, **params):
    """Drive the live loopback server the way Google's redirect would."""
    import urllib.request
    from urllib.parse import urlencode
    url = f"{receiver.redirect_uri}/?{urlencode(params)}"
    urllib.request.urlopen(url, timeout=5).read()


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
