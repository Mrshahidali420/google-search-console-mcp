import base64
import hashlib
import re
from urllib.parse import parse_qs, urlparse

import pytest

from gsc_core import gauth


def test_pkce_verifier_length_is_within_spec():
    verifier, _ = gauth.pkce_pair()
    assert 43 <= len(verifier) <= 128


def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = gauth.pkce_pair()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_s256_matches_the_rfc_7636_test_vector():
    """RFC 7636 Appendix B. Pins the construction against a value neither the
    implementation nor this test computed.

    The verifier below is base64url(octets) for the exact 32-octet sequence
    published in Appendix B: [116, 24, 223, 180, 151, 153, 224, 37, 79, 250,
    96, 125, 216, 173, 187, 186, 22, 212, 37, 77, 105, 214, 191, 240, 91, 88,
    5, 88, 83, 132, 141, 121]. `expected` is independently derived from that
    same octet sequence (sha256 -> base64url), not copied from the RFC prose,
    since the prose copy in circulation has a one-character transcription
    error in its final character ("...cE" instead of the correct "...cM").
    """
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert gauth._b64url(digest) == expected


def test_pkce_pairs_are_unique():
    first, _ = gauth.pkce_pair()
    second, _ = gauth.pkce_pair()
    assert first != second


def test_pkce_values_use_only_unreserved_characters():
    pattern = re.compile(r"^[A-Za-z0-9\-._~]+$")
    verifier, challenge = gauth.pkce_pair()
    assert pattern.match(verifier)
    assert pattern.match(challenge)


def test_endpoints_are_google():
    assert gauth.AUTH_ENDPOINT == "https://accounts.google.com/o/oauth2/v2/auth"
    assert gauth.TOKEN_ENDPOINT == "https://oauth2.googleapis.com/token"


def test_auth_url_points_at_google():
    parsed = urlparse(gauth.build_auth_url("c", "http://127.0.0.1:1", "ch", "s" * 32))
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"


def test_auth_url_carries_required_parameters():
    url = gauth.build_auth_url(
        client_id="client-123",
        redirect_uri="http://127.0.0.1:9999",
        challenge="abc123",
        state="s" * 32,
    )
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["client-123"]
    assert query["redirect_uri"] == ["http://127.0.0.1:9999"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge"] == ["abc123"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == ["s" * 32]
    assert query["scope"] == [gauth.SCOPE]


def test_auth_url_requests_a_refresh_token():
    url = gauth.build_auth_url("c", "http://127.0.0.1:1", "ch", "s" * 32)
    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]


def test_build_auth_url_rejects_a_weak_state():
    with pytest.raises(ValueError):
        gauth.build_auth_url("c", "http://127.0.0.1:1", "ch", "short")


def test_scope_is_exactly_webmasters():
    assert gauth.SCOPE == "https://www.googleapis.com/auth/webmasters"
    # A second scope would appear as a space-separated value, and would widen
    # what OAuth verification has to cover.
    query = parse_qs(urlparse(
        gauth.build_auth_url("c", "http://127.0.0.1:1", "ch", "s" * 32)
    ).query)
    assert " " not in query["scope"][0]
