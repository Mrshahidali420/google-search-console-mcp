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
import threading
import webbrowser
from datetime import datetime, timedelta, UTC
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse as _urlparse

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
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    icacls = os.path.join(system_root, "System32", "icacls.exe")
    if not os.path.isabs(icacls):
        log.warning("cannot restrict %s: SystemRoot is not an absolute path",
                    path.name)
        return
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
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            # Harden while the file is still empty: on Windows there is no
            # equivalent of POSIX 0600-at-creation, so writing first would put
            # the refresh token on disk unprotected. Inside the with-block so
            # the descriptor is closed exactly once even if this raises.
            _harden(Path(temporary))
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
    try:
        response = client.post(TOKEN_ENDPOINT, data=payload, timeout=30)
    except BaseException:
        # A network failure must not leave the payload — client secret,
        # authorization code, PKCE verifier, refresh token — live in the
        # raising frame for a show-locals traceback to dump.
        del payload
        raise
    # Drop the payload before anything else can raise: it holds the client
    # secret, the authorization code, the PKCE verifier and — on refresh — the
    # refresh token, and a show-locals traceback would carry all four into the
    # caller's logs.
    #
    # This scrubs our frame only. A requests exception also carries the same
    # urlencoded payload on .request.body, so callers must never log a raw
    # exception object raised from this path — log str(exc) or a fixed message.
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


_CONSENT_PAGE = b"""<!doctype html>
<meta charset="utf-8">
<title>gsc-mcp</title>
<body style="font-family:system-ui;padding:3rem;max-width:32rem">
<h1>Connected</h1>
<p>You can close this tab and return to your terminal.</p>
</body>"""


class ConsentFailed(RuntimeError):
    """The consent round trip did not produce a usable authorization code."""


class _LoopbackServer(HTTPServer):
    """An ephemeral port never needs SO_REUSEADDR, and allowing it would let
    another local process race to bind the same 127.0.0.1:port for the
    redirect."""

    allow_reuse_address = False


class LoopbackReceiver:
    """A one-shot local HTTP server that catches Google's redirect.

    Binds an ephemeral port because Google allows any port on 127.0.0.1 for
    installed apps, and a fixed port would collide with whatever else the user
    happens to be running.
    """

    def __init__(self) -> None:
        self.state = _b64url(secrets.token_bytes(24))
        self._code: str | None = None
        self._error: str | None = None
        self._received = threading.Event()
        self._server = _LoopbackServer(("127.0.0.1", 0), self._handler_class())
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "LoopbackReceiver":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout: float = 300.0) -> str:
        if not self._received.wait(timeout):
            raise ConsentFailed("consent timed out; no redirect received")
        if self._error:
            raise ConsentFailed(self._error)
        if not self._code:
            raise ConsentFailed("redirect carried no authorization code")
        return self._code

    def _handler_class(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            # Without this the stdlib blocks in readline() forever on a stalled
            # connection, and shutdown() never gets to check its flag.
            timeout = 10

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                query = parse_qs(_urlparse(self.path).query)
                state = (query.get("state") or [""])[0]
                error = (query.get("error") or [""])[0]
                code = (query.get("code") or [""])[0]

                # Anything that is not the callback — a browser's favicon
                # probe, a speculative connection — must not be mistaken for a
                # failed redirect, and a second request must not overwrite the
                # first result.
                if receiver._received.is_set() or not (state or error or code):
                    self.send_response(204)
                    self.end_headers()
                    return

                try:
                    if state != receiver.state:
                        receiver._error = "redirect state did not match"
                    elif error:
                        receiver._error = f"consent refused: {error[:200]}"
                    else:
                        receiver._code = code

                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(_CONSENT_PAGE)))
                    self.end_headers()
                    self.wfile.write(_CONSENT_PAGE)
                finally:
                    # Set even if the client aborted mid-write, or the flow
                    # stalls for the full timeout with a result already in hand.
                    receiver._received.set()

            def log_message(self, *args) -> None:
                """Silence stdlib's stderr access log."""

        return Handler


def run_consent_flow(client_id: str, client_secret: str, *,
                     open_browser: bool = True, session=None) -> dict:
    """Full consent round trip. Returns the stamped token payload and saves it."""
    verifier, challenge = pkce_pair()
    with LoopbackReceiver() as receiver:
        # Capture the URI while the socket is still bound — the token exchange
        # must send the identical redirect_uri, and reading it after __exit__
        # has called server_close() would be reading a dead socket.
        redirect_uri = receiver.redirect_uri
        url = build_auth_url(client_id, redirect_uri, challenge, receiver.state)
        log.info("opening consent page")
        if open_browser:
            webbrowser.open(url)
        code = receiver.wait()

    token = exchange_code(client_id, client_secret, code, verifier,
                          redirect_uri, session=session)
    save_token(token)
    return token
