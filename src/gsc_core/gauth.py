"""Google OAuth for gsc-mcp.

The client secret ships inside the distributed package, which Google accepts
for installed applications only when the flow uses PKCE. S256 only.

One scope: webmasters. The Sheets scope was dropped along with the Sheet, which
leaves a single sensitive scope to carry through OAuth verification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path
from urllib.parse import urlencode

import requests

from . import paths
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
    if len(state) < 16:
        raise ValueError(
            "state must be at least 16 characters of unguessable entropy; "
            "it is the only defence against a forged redirect"
        )
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


_EXPIRY_MARGIN_SECONDS = 60


class AuthRequired(RuntimeError):
    """No usable credentials. The caller should route the user to setup."""


def save_token(data: dict, path: Path | None = None) -> None:
    """Write the token file atomically, owner-readable only on POSIX."""
    target = path or paths.token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(temporary, 0o600)
    temporary.replace(target)


def load_token(path: Path | None = None) -> dict | None:
    target = path or paths.token_path()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _post_token(session, payload: dict) -> dict:
    client = session or requests
    response = client.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    body = response.json()
    if response.status_code != 200:
        raise RuntimeError(
            f"token endpoint returned {response.status_code}: "
            f"{body.get('error', response.text)}"
        )
    return body


def _with_expiry(body: dict) -> dict:
    expires_in = int(body.get("expires_in", 3599))
    stamped = dict(body)
    stamped["expires_at"] = (
        datetime.now(UTC) + timedelta(seconds=expires_in)
    ).isoformat()
    return stamped


def exchange_code(client_id: str, client_secret: str, code: str,
                  verifier: str, redirect_uri: str, *, session=None) -> dict:
    """Trade an authorization code for tokens. PKCE verifier is mandatory."""
    body = _post_token(session, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    return _with_expiry(body)


class TokenProvider:
    """Hands out access tokens, refreshing shortly before expiry.

    API modules take one of these rather than a raw string so that a 401
    partway through a long run refreshes once instead of ending the run.
    """

    def __init__(self, client_id: str, client_secret: str, *,
                 token_path: Path | None = None, session=None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._path = token_path or paths.token_path()
        self._session = session

    def access_token(self) -> str:
        stored = load_token(self._path)
        if not stored or "refresh_token" not in stored:
            raise AuthRequired("no stored credentials; run gsc_setup()")

        if self._is_fresh(stored):
            return stored["access_token"]

        refreshed = _with_expiry(_post_token(self._session, {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": stored["refresh_token"],
            "grant_type": "refresh_token",
        }))
        # Google omits refresh_token on refresh responses — carry it forward.
        refreshed.setdefault("refresh_token", stored["refresh_token"])
        save_token(refreshed, self._path)
        return refreshed["access_token"]

    def invalidate(self) -> None:
        """Drop the cached access token after a 401. Refresh token is kept."""
        stored = load_token(self._path)
        if not stored:
            return
        cleared = {k: v for k, v in stored.items() if k != "expires_at"}
        cleared["expires_at"] = datetime.now(UTC).isoformat()
        save_token(cleared, self._path)

    @staticmethod
    def _is_fresh(stored: dict) -> bool:
        if not stored.get("access_token") or not stored.get("expires_at"):
            return False
        try:
            expires_at = datetime.fromisoformat(stored["expires_at"])
        except ValueError:
            return False
        margin = timedelta(seconds=_EXPIRY_MARGIN_SECONDS)
        return datetime.now(UTC) < expires_at - margin
