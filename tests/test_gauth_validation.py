import pytest
from gsc_core import gauth


class _FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = "body"

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.authorization = None

    def get(self, url, headers=None, timeout=None):
        self.authorization = (headers or {}).get("Authorization")
        return self._response


def _redirect(receiver, **params):
    """Drive the live loopback server the way Google's redirect would.

    Mirrors tests/test_gauth_consent_split.py's helper: the handler writes
    the response body and only sets `_received` afterward in its `finally`,
    so a caller that calls poll() immediately after urlopen() can race the
    handler. Waiting briefly on the event makes the post-redirect state
    deterministic for every caller of this helper.
    """
    import urllib.request
    from urllib.parse import urlencode
    url = f"{receiver.redirect_uri}/?{urlencode(params)}"
    urllib.request.urlopen(url, timeout=5).read()
    receiver._received.wait(2)


def test_verify_token_returns_the_property_count():
    payload = {"siteEntry": [{"siteUrl": "https://example.com/"},
                             {"siteUrl": "sc-domain:example.net"}]}
    session = _FakeSession(_FakeResponse(200, payload))
    token = {"access_token": "at-1", "refresh_token": "rt-1"}
    assert gauth.verify_token(token, session=session) == 2


def test_verify_token_raises_when_the_token_sees_no_properties():
    session = _FakeSession(_FakeResponse(200, {"siteEntry": []}))
    token = {"access_token": "at-1", "refresh_token": "rt-1"}
    with pytest.raises(gauth.ConsentFailed):
        gauth.verify_token(token, session=session)


def test_verify_token_raises_on_a_non_200():
    session = _FakeSession(_FakeResponse(403, {}))
    token = {"access_token": "at-1", "refresh_token": "rt-1"}
    with pytest.raises(gauth.ConsentFailed):
        gauth.verify_token(token, session=session)


def test_verify_token_never_puts_the_access_token_in_the_exception():
    session = _FakeSession(_FakeResponse(403, {}))
    token = {"access_token": "super-secret-at", "refresh_token": "rt-1"}
    with pytest.raises(gauth.ConsentFailed) as caught:
        gauth.verify_token(token, session=session)
    assert "super-secret-at" not in str(caught.value)


def test_verify_token_rejects_a_grant_with_no_refresh_token():
    session = _FakeSession(_FakeResponse(200, {"siteEntry": [{"siteUrl": "x"}]}))
    with pytest.raises(gauth.ConsentFailed) as caught:
        gauth.verify_token({"access_token": "at-1"}, session=session)
    assert "refresh" in str(caught.value).lower()


def test_a_failing_verification_leaves_the_stored_token_untouched(tmp_path,
                                                                   monkeypatch):
    """The whole point of validate-before-save."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    good = {"access_token": "old", "refresh_token": "old-rt"}
    gauth.save_token(good)

    pending = gauth.start_consent("client-id-123")
    # Land a redirect so poll() yields a code.
    _redirect(pending.receiver, state=pending.state, code="auth-code-xyz")

    session = _FakeSession(_FakeResponse(200, {"siteEntry": []}))
    monkeypatch.setattr(gauth, "exchange_code",
                        lambda *a, **k: {"access_token": "new",
                                         "refresh_token": "new-rt"})
    with pytest.raises(gauth.ConsentFailed):
        gauth.finish_consent(pending, "secret", client_id="client-id-123",
                             session=session)
    assert gauth.load_token()["refresh_token"] == "old-rt"
