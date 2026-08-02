import base64
import hashlib
from urllib.parse import parse_qs, urlparse

from gsc_core import gauth


def test_pkce_verifier_length_is_within_spec():
    verifier, _ = gauth.pkce_pair()
    assert 43 <= len(verifier) <= 128


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = gauth.pkce_pair()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_pkce_pairs_are_unique():
    first, _ = gauth.pkce_pair()
    second, _ = gauth.pkce_pair()
    assert first != second


def test_pkce_values_are_url_safe():
    verifier, challenge = gauth.pkce_pair()
    for value in (verifier, challenge):
        assert "=" not in value
        assert "+" not in value
        assert "/" not in value


def test_auth_url_carries_required_parameters():
    url = gauth.build_auth_url(
        client_id="client-123",
        redirect_uri="http://127.0.0.1:9999",
        challenge="abc123",
        state="xyz",
    )
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["client-123"]
    assert query["redirect_uri"] == ["http://127.0.0.1:9999"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge"] == ["abc123"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["xyz"]
    assert query["scope"] == [gauth.SCOPE]


def test_auth_url_requests_a_refresh_token():
    url = gauth.build_auth_url("c", "http://127.0.0.1:1", "ch", "st")
    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]


def test_scope_is_exactly_webmasters():
    assert gauth.SCOPE == "https://www.googleapis.com/auth/webmasters"
    assert "spreadsheets" not in gauth.SCOPE
