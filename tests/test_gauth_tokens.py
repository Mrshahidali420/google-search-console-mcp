import json
from datetime import datetime, timedelta, UTC

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
