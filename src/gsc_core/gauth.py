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
from dataclasses import dataclass
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

# The one owner of this URL. api.py already imports from this module (it
# takes a TokenProvider), so importing api here would be a circular import —
# but api.py can and does import THIS constant (as api.SITES_URI = gauth.
# SITES_ENDPOINT) rather than restating the literal, which would have let
# the two drift.
SITES_ENDPOINT = "https://www.googleapis.com/webmasters/v3/sites"

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


def write_private_json(data: dict, target: Path) -> None:
    """Write JSON atomically to a file readable only by this user.

    The temp file is hardened immediately after creation, before any
    credential is written into it — writing first and hardening after would
    leave the secret unprotected on disk for the width of the write, on
    every save. The random temp name also stops two processes colliding and
    promoting a half-written file.

    Public rather than private because the token is not the only credential
    on disk: the shipped OAuth client is cached beside it, and a second
    implementation of "harden then write atomically" is exactly the kind of
    duplicate that drifts until only one of them still hardens.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    handle, temporary = tempfile.mkstemp(dir=target.parent,
                                         prefix=f".{target.stem}-",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            # Harden while the file is still empty: on Windows there is no
            # equivalent of POSIX 0600-at-creation, so writing first would put
            # the credential on disk unprotected. Inside the with-block so
            # the descriptor is closed exactly once even if this raises.
            _harden(Path(temporary))
            json.dump(data, stream, indent=2)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def save_token(data: dict, path: Path | None = None) -> None:
    """Write the token file atomically, readable only by this user."""
    write_private_json(data, path or paths.token_path())


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
            "Authorise again: run gsc_setup() and follow the step it "
            "returns."
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

    Refresh is SINGLE-FLIGHT. api.check_status() calls access_token() from
    every worker thread at once, so without this several workers hitting 401
    together would each invalidate and each refresh. Google rotates the
    refresh token on use: concurrent refreshes race to write the token file
    and the losers persist a refresh token Google has already retired,
    leaving the stored credential dead and the user re-authorising. The lock
    turns that into one refresh that the rest wait on and reuse.

    Because the lock is PER-INSTANCE, the guarantee only holds while one
    instance covers everything that might refresh concurrently. That is
    what deps.provider() is for: it hands every tool call the same
    provider rather than a fresh one, since concurrent tool calls are
    exactly where the rotated-token race lives.

    The lock is per-instance and per-process — it does not coordinate with a
    second gsc-mcp process sharing the token file. That is a narrower race
    (save_token() is already atomic via os.replace) and closing it needs a
    file lock, which is a different change from the one concurrency inside
    this process requires.
    """

    def __init__(self, client_id: str, client_secret: str, *,
                 token_path: Path | None = None, session=None) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._path = token_path or paths.token_path()
        self._session = session
        self._refresh_lock = threading.Lock()

    def _load(self) -> dict:
        stored = load_token(self._path)
        if not stored or "refresh_token" not in stored:
            raise AuthRequired(
                "No stored credentials. Run gsc_setup() and follow the step "
                "it returns to sign in.")
        return stored

    def access_token(self) -> str:
        stored = self._load()
        if self._is_fresh(stored):
            return stored["access_token"]

        with self._refresh_lock:
            # Re-read and re-check UNDER the lock. Whoever held it before us
            # has already written a fresh token, and refreshing again would
            # spend a refresh token Google rotated out from under us. This
            # second check is what makes the flight single rather than merely
            # serialised.
            stored = self._load()
            if self._is_fresh(stored):
                return stored["access_token"]
            return self._refresh(stored)

    def _refresh(self, stored: dict) -> str:
        """Exchange the refresh token. Callers hold _refresh_lock."""
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
        refresh. It does not drop the token; the refresh token is kept.

        Takes the same lock as access_token(): this is a read-modify-write of
        the token file, and running it against a refresh in flight would
        write back a stale body over the new credential.
        """
        with self._refresh_lock:
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


def verify_token(token: dict, *, session=None) -> int:
    """Prove a fresh token actually reaches Search Console. Returns the
    number of properties it can see.

    Three distinct failures, three distinct messages, because the fixes
    differ: no refresh token means re-consent and approve fully; a non-200
    (or a 200 with a body that is not JSON) means the grant or the API is
    wrong; zero properties almost always means the user signed in with the
    wrong Google account, which is otherwise indistinguishable from success.

    Never includes the access token in any message it raises — this
    exception text reaches an MCP client. It also never leaves `token` or
    `response` alive in a raising frame: both are the same live secret
    material `_post_token` deletes before it can raise, for the same reason
    — a --showlocals traceback or a locals-capturing log handler must not
    be able to dump the access token, the refresh token, or `response`'s own
    record of the Authorization header it sent.
    """
    if not token.get("refresh_token"):
        del token
        raise ConsentFailed(
            "Google returned no refresh token; approve every requested "
            "permission on the consent screen and try again")
    http = session or requests
    response = http.get(
        SITES_ENDPOINT,
        headers={"Authorization": f"Bearer {token['access_token']}"},
        timeout=30)
    if response.status_code != 200:
        status = response.status_code
        del token, response
        raise ConsentFailed(
            f"Search Console rejected the new token (HTTP {status})")
    try:
        body = response.json()
    except ValueError:
        del token, response
        raise ConsentFailed(
            "Search Console returned a response that was not valid JSON"
        ) from None
    count = len(body.get("siteEntry") or [])
    if count == 0:
        del token, response
        raise ConsentFailed(
            "the token works but sees no Search Console properties — you "
            "probably signed in with a different Google account than the "
            "one that owns your sites")
    return count


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
        self._started = False
        self._closed = False
        # Guards _started/_closed together with the calls that act on them.
        # Without it, start() setting _started True after _thread.start()
        # races a concurrent close(): close() can read _started as False,
        # skip shutdown(), and call server_close() on a socket the serving
        # thread is still selecting on — the thread never exits, and a
        # second close() can return early while the first is still
        # mid-shutdown(). start()/close() are public API held across
        # separate tool calls, so a second caller racing the first is a
        # real scenario, not a hypothetical one.
        self._lifecycle_lock = threading.Lock()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        """Begin serving. Separate from __enter__ so a caller that must
        outlive one stack frame — an MCP tool returning a consent URL and
        being called again later — can hold the receiver open across calls."""
        with self._lifecycle_lock:
            # _started is set only after _thread.start() returns, so a
            # RuntimeError out of start() (already started, interpreter
            # shutting down) leaves _started False and close() still knows
            # not to call shutdown() — hoisting the flag above the call
            # would silently rely on shutdown() having something to do.
            self._thread.start()
            self._started = True

    def close(self) -> None:
        """Idempotent, and safe to call on a receiver that never started.

        BaseServer.shutdown() sets a flag and then blocks, with no timeout,
        on an internal event that only serve_forever() sets on its way out.
        If the thread never started, nothing will ever set that event, and
        shutdown() would hang forever — a caller holding a PendingConsent it
        never finished must be able to close it unconditionally.
        """
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            if self._started:
                self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> "LoopbackReceiver":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def poll(self) -> str | None:
        """The authorization code, or None if the redirect has not landed.

        Never blocks. Raises ConsentFailed for a redirect that arrived and
        was bad — a state mismatch or a refused consent — because those are
        terminal, and a caller polling forever would never learn of them.
        """
        if not self._received.is_set():
            return None
        if self._error:
            raise ConsentFailed(self._error)
        if not self._code:
            raise ConsentFailed("redirect carried no authorization code")
        return self._code

    def wait(self, timeout: float = 300.0) -> str:
        if not self._received.wait(timeout):
            raise ConsentFailed("consent timed out; no redirect received")
        code = self.poll()
        assert code is not None  # _received is set, so poll returns or raises
        return code

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


@dataclass
class PendingConsent:
    """A consent round trip in flight, held between two tool calls.

    Carries a LIVE socket: whoever creates one owns closing it. The
    verifier is the PKCE secret and is never logged, never returned to an
    MCP client, and never placed in the auth URL.
    """
    receiver: LoopbackReceiver
    verifier: str
    redirect_uri: str
    auth_url: str
    state: str


def start_consent(client_id: str) -> PendingConsent:
    """Open the loopback receiver and build the consent URL. Non-blocking.

    The caller must eventually call receiver.close(), whether consent
    completes, fails, or is abandoned — an ephemeral port held open by a
    forgotten receiver leaks a thread for the life of the process.
    """
    verifier, challenge = pkce_pair()
    receiver = LoopbackReceiver()
    receiver.start()
    try:
        redirect_uri = receiver.redirect_uri
        auth_url = build_auth_url(client_id, redirect_uri, challenge,
                                  receiver.state)
    except BaseException:
        # Nothing below has run, so nothing else will close this socket.
        receiver.close()
        raise
    return PendingConsent(receiver=receiver, verifier=verifier,
                          redirect_uri=redirect_uri, auth_url=auth_url,
                          state=receiver.state)


def finish_consent(pending: PendingConsent, client_secret: str, *,
                   client_id: str, session=None,
                   verify: bool = True) -> dict | None:
    """Complete the round trip if the redirect has landed, else None.

    Does NOT close the receiver on the None path — the caller polls again.
    DOES close it on every terminal path, success or raise, because the
    port has no further purpose once a code has been consumed. That includes
    poll() itself raising: a state mismatch or a refused consent is just as
    terminal as a successful exchange, and leaving the socket open in that
    case would leak a port and a thread for the life of the process.
    """
    try:
        code = pending.receiver.poll()
    except BaseException:
        pending.receiver.close()
        raise
    if code is None:
        return None
    try:
        token = exchange_code(client_id, client_secret, code,
                              pending.verifier, pending.redirect_uri,
                              session=session)
        if verify:
            # Before save_token, deliberately: a grant that cannot reach
            # the API must not replace one that can.
            verify_token(token, session=session)
        save_token(token)
        return token
    finally:
        pending.receiver.close()


def run_consent_flow(client_id: str, client_secret: str, *,
                     open_browser: bool = True, session=None) -> dict:
    """Full consent round trip, blocking. Returns the saved token payload."""
    pending = start_consent(client_id)
    try:
        if open_browser:
            webbrowser.open(pending.auth_url)
        pending.receiver.wait()
    except BaseException:
        pending.receiver.close()
        raise
    token = finish_consent(pending, client_secret, client_id=client_id,
                           session=session)
    if token is None:  # pragma: no cover — wait() returned, so poll() will too
        raise ConsentFailed("consent completed but no code was available")
    return token
