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
import subprocess
import sys
import tempfile
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


_EXPIRY_MARGIN_SECONDS = 300


class AuthRequired(RuntimeError):
    """No usable credentials. The caller should route the user to setup."""


def _harden(path: Path) -> None:
    """Restrict a file to the current user. Best effort — never fatal.

    POSIX uses chmod. On Windows chmod is a no-op, so we break ACL inheritance
    and grant only the current user, which is the nearest equivalent available
    without a keyring dependency. icacls is resolved to an absolute path
    under SystemRoot rather than found on PATH: CreateProcess searches the
    current working directory before System32, so a bare "icacls" would run
    an executable planted in the MCP server's CWD.
    """
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            log.warning("could not restrict permissions on %s", path.name)
        return

    user = os.environ.get("USERNAME")
    if not user:
        log.warning("cannot restrict %s: USERNAME unset", path.name)
        return
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    icacls = os.path.join(system_root, "System32", "icacls.exe")
    try:
        subprocess.run(
            [icacls, str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
            check=True, capture_output=True, timeout=15,
        )
    except subprocess.CalledProcessError as exc:
        # exc's default __str__ embeds the full argv, including the
        # absolute token path — log only the file name and exit code.
        log.warning("could not restrict permissions on %s: icacls exited %s",
                    path.name, exc.returncode)
    except (OSError, subprocess.SubprocessError):
        # Covers a missing icacls.exe and a timed-out run. TimeoutExpired's
        # __str__ also embeds the full argv, so nothing from the exception
        # itself is logged here either.
        log.warning("could not restrict permissions on %s", path.name)


def save_token(data: dict, path: Path | None = None) -> None:
    """Write the token file atomically, readable only by this user.

    The temp file is hardened immediately after creation, before the refresh
    token is written into it — writing first and hardening after would leave
    the credential unprotected on disk for the width of the write, on every
    refresh. The random temp name also stops two processes colliding and
    promoting a half-written file.
    """
    target = path or paths.token_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".token-",
                                         suffix=".tmp")
    try:
        # Harden BEFORE writing, not after: on Windows mkstemp gives no
        # equivalent of POSIX 0600-at-creation, so writing first would put
        # the refresh token on disk unprotected for the width of the write.
        _harden(Path(temporary))
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_token(path: Path | None = None) -> dict | None:
    target = path or paths.token_path()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


_REAUTH_ERRORS = frozenset({"invalid_grant", "invalid_client"})


def _post_token(session, payload: dict) -> dict:
    client = session or requests
    response = client.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    # Drop the payload before anything can raise: it holds the client secret,
    # the authorization code, the PKCE verifier and — on refresh — the refresh
    # token, and a show-locals traceback would carry all four into the
    # caller's logs.
    del payload
    return _decode_token_response(response)


def _decode_token_response(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code == 200:
        if "access_token" not in body:
            raise RuntimeError(
                "token endpoint returned 200 with no access_token"
            )
        return body

    error = body.get("error") or f"HTTP {response.status_code}"
    if error in _REAUTH_ERRORS:
        raise AuthRequired(
            f"Google rejected the stored credentials ({error}). "
            "Run gsc_setup() to authorise again."
        )
    detail = body.get("error_description", "")
    raise RuntimeError(
        f"token endpoint returned {response.status_code}: {error} {detail}".strip()
    )


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
        """Back-date expires_at after a 401, forcing the next call to
        refresh. It does not drop the token; the refresh token is kept."""
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
        margin = timedelta(seconds=_EXPIRY_MARGIN_SECONDS)
        try:
            expires_at = datetime.fromisoformat(stored["expires_at"])
            return datetime.now(UTC) < expires_at - margin
        except (ValueError, TypeError):
            return False
