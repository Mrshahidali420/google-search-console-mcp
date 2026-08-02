"""Google OAuth for gsc-mcp.

The client secret ships inside the distributed package, which Google accepts
for installed applications only when the flow uses PKCE. S256 only.

One scope: webmasters. The Sheets scope was dropped along with the Sheet, which
leaves a single sensitive scope to carry through OAuth verification.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

from . import runlog

log = runlog.get(__name__)

SCOPE = "https://www.googleapis.com/auth/webmasters"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

_VERIFIER_BYTES = 64


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    """A fresh (verifier, challenge). The verifier never leaves this machine."""
    verifier = _b64url(secrets.token_bytes(_VERIFIER_BYTES))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_auth_url(client_id: str, redirect_uri: str, challenge: str,
                   state: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"
