"""Localhost WebSocket bridge to the browser extension that drives the user's
real, logged-in Chromium profile.

Protocol (JSON text frames):

    client -> server   {"type":"pair_request","extension_id":...}   (first frame)
    server -> client   {"type":"pair_ok","token":...} | {"type":"pair_denied","reason":...}
    client -> server   {"type":"hello","token":...,"version":...}
    server -> client   {"type":"hello_ok"} | {"type":"hello_denied"}
    server -> client   {"type":"submit","id":...,"property":...,"url":...,"authuser":...}
    client -> server   {"type":"result","id":...,"outcome":...,"detail":...?}
    client -> server   {"type":"progress","id":...,"stage":...}   (informational)
    client -> server   {"type":"cancel"}                          (abort the run)
    either             {"type":"ping"} / {"type":"pong"}

Security: bound to 127.0.0.1 only. The first frame must be a hello carrying
the shared token, or a pair_request that is verified against the browser
profile before the token is handed over — see pairing.verify_pair_request.

This module is transport only. It knows nothing about quota, the database,
or what an outcome means; that is submit.py's job.

The head of this module is pure functions — the token store, the outcome
vocabulary, the frame helpers. `BridgeSession` and below is the live half:
one localhost server, one authenticated connection, one blocking RPC.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from websockets.sync.server import serve as ws_serve

from . import pairing, paths, runlog

log = runlog.get(__name__)

# The EXACT result vocabulary submit.py consumes. Anything else off the wire
# is coerced to "error" — the bridge never invents a status. "skipped" is in
# here because content.js emits it (probe-only runs, an aborted job); the
# private toolkit's copy of this set omits it, which turned a legitimate skip
# into an error and charged a quota slot for a URL that was never submitted.
KNOWN_OUTCOMES = frozenset({
    "submitted", "already_indexed", "quota_exceeded", "timeout",
    "captcha", "captcha_after_click", "auth_required",
    "account_mismatch", "rate_limited", "skipped", "error",
})


def token_path() -> Path:
    return paths.ensure_config_dir() / "bridge_token.txt"


def load_or_create_token() -> str:
    """The shared secret the extension proves itself with. Created on first use.

    Never logged, never returned from a tool, never put in an exception
    message: it is the one credential that lets a local process drive the
    user's signed-in browser session.
    """
    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    try:
        path.write_text(token, encoding="utf-8")
    except OSError as exc:
        # An OSError's own message embeds the token path, and that path
        # contains the user's account name. Only the TYPE name is logged,
        # and the replacement error carries no path at all.
        log.warning("could not save the bridge token (%s)", type(exc).__name__)
        raise RuntimeError(
            "could not save the bridge token — check that the gsc-mcp "
            "config directory exists and is writable") from None
    log.info("created a new bridge token; the extension will fetch it on "
             "its next pair request")
    return token


def make_submit(cmd_id: str, gsc_property: str, url: str, authuser: str) -> str:
    return json.dumps({"type": "submit", "id": cmd_id, "property": gsc_property,
                       "url": url, "authuser": authuser})


def parse_message(raw: object) -> dict | None:
    """A frame we can act on, or None. Never raises on malformed input."""
    try:
        message = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict) or "type" not in message:
        return None
    return message


def map_outcome(outcome: object) -> str:
    return outcome if outcome in KNOWN_OUTCOMES else "error"


class BridgeSession:
    """One run's bridge: a localhost WS server that exactly one authenticated
    extension connects to.

    submit() is called from the caller's synchronous loop and results arrive
    on the handler thread. websockets' sync connections permit send and recv
    from different threads, which is what makes that shape legal.
    """

    def __init__(self, port: int, token: str, *, connect_timeout: int = 60,
                 target: object | None = None) -> None:
        self.port = port
        self.token = token
        self.connect_timeout = connect_timeout
        # target is what makes self-pairing possible: verifying a pair_request
        # means looking the claimed ID up in the browser profile it names.
        # Without one, pairing is refused rather than waved through.
        self.target = target
        #: Set once the socket is bound and `self.port` is the REAL port.
        #: Callers start start() in a thread and wait on this instead of
        #: poll-connecting, which is both exact and free.
        self.ready = threading.Event()
        self._server: object | None = None
        self._conn: object | None = None
        self._conn_lock = threading.Lock()
        # Waited on by submit() and notified by the handler on every
        # (re)connection, so a resend wakes the instant a socket is live
        # rather than on the next poll tick.
        self._conn_cv = threading.Condition(self._conn_lock)
        self._connected = threading.Event()
        self._pending: dict[str, dict] = {}
        self._pending_lock = threading.Lock()
        self._stopped = False
        # Bumped on each (re)connection, so submit() can notice a bounce
        # mid-flight and re-send on the fresh connection.
        self._gen = 0

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """Blocking; run in a daemon thread. Binds 127.0.0.1 ONLY.

        Never 0.0.0.0 and never a hostname: this socket hands out commands
        that drive the user's signed-in browser, so it must not be
        reachable from anywhere but this machine.
        """
        with ws_serve(self._handler, "127.0.0.1", self.port) as server:
            self._server = server
            # Port 0 means "any free port"; publish the one actually bound
            # so callers and tests never have to guess or probe.
            try:
                self.port = int(server.socket.getsockname()[1])
            except (OSError, IndexError, TypeError, ValueError) as exc:
                log.debug("could not read the bound port (%s)",
                          type(exc).__name__)
            self.ready.set()
            server.serve_forever()

    def stop(self) -> None:
        self._stopped = True
        with self._conn_lock:
            conn, self._conn = self._conn, None
            # Cleared here, not left to the handler: stop() has just nulled
            # _conn, so the handler's `if self._conn is conn` guard is False
            # and it will not clear this itself. Without it
            # wait_for_extension() keeps answering True for a session that
            # is already shut down. Inside the lock, so a hello completing
            # right now cannot set _connected and then have it cleared out
            # from under it.
            self._connected.clear()
            self._conn_cv.notify_all()
        self._resolve_all("error")
        # The server may not have finished binding yet, in which case
        # self._server is still None and shutdown() would be skipped. A
        # short wait closes that window; it is bounded so stop() can never
        # hang a caller.
        self.ready.wait(5)
        for closer in (lambda: conn and conn.close(),
                       lambda: self._server and self._server.shutdown()):
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 — teardown must not raise
                log.debug("bridge teardown ignored %s", type(exc).__name__)

    def cancel(self) -> None:
        """Abort the in-flight run (the extension's 'Clear pending jobs').

        Marks the session stopped and resolves every waiter, so the caller's
        loop returns at once for the current and remaining URLs and unwinds.
        Unlike stop() it does not shut the server down — the owning run's
        teardown does that, which is what frees the port for a fresh run.

        It does, however, end the current CONNECTION. Setting _stopped makes
        the handler's read loop fall out within a second and websockets
        closes the socket, so the extension sees a disconnect and starts its
        reconnect backoff. That is harmless — the server is still listening
        and it will reattach — but it is visible in the extension's popup as
        a brief "reconnecting", and it is why cancel() is not a way to pause.
        """
        log.info("bridge: cancel requested — aborting the current run")
        self._stopped = True
        with self._conn_cv:
            self._conn_cv.notify_all()
        self._resolve_all("error")

    def _resolve_all(self, outcome: str) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter["outcome"] = outcome
            waiter["event"].set()

    def wait_for_extension(self, timeout: float | None = None) -> bool:
        return self._connected.wait(
            self.connect_timeout if timeout is None else timeout)

    # -- the one RPC ------------------------------------------------------
    def _wait_for_conn(self, grace: float) -> tuple[object | None, int]:
        """Block until a connection is live or `grace` seconds pass.

        Returns (conn, generation); conn is None if none appeared in time.
        """
        end = time.monotonic() + grace
        with self._conn_cv:
            while True:
                if self._stopped:
                    return None, self._gen
                if self._conn is not None:
                    return self._conn, self._gen
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None, self._gen
                self._conn_cv.wait(remaining)

    def submit(self, gsc_property: str, url: str, authuser: str, *,
               timeout: int = 1500, reconnect_grace: float = 120,
               max_resends: int = 3) -> str:
        """Send one submit command; block until the extension reports the
        outcome or `timeout` seconds pass.

        Resilient to brief WS drops: if no connection is live, or the
        connection bounces mid-flight, wait up to `reconnect_grace` for the
        extension to come back and RE-SEND, up to `max_resends` times,
        rather than failing the whole run. A blip pauses one URL; it does
        not end the batch.

        Returns the reported outcome, or one of two synthesised ones. The
        distinction between them is load-bearing and NOT cosmetic, because
        submit.py's disposition table charges a Search Console quota slot
        for one and not the other:

        * "timeout" — a submit command WAS put on the wire and no verdict
          came back in time. A click may well have reached Google, so the
          slot has to be assumed spent.
        * "error" — the URL could not be seen through to a verdict: no
          connection was live within `reconnect_grace`, the whole `timeout`
          expired before one appeared, the resends ran out, or the run was
          stopped or cancelled.

        The invariant that holds, and the ONLY one, is:

            "timeout"  =>  a submit frame reached the socket.

        The converse does NOT hold. "error" does not prove that nothing
        reached Google: stop(), cancel() and an exhausted resend count are
        all reachable after a send has already gone out, and a send that
        went out may have landed a click before the connection died — the
        same window the resend caveat below describes. Treating "error" as
        no-slot-spent is therefore a deliberate accepted risk, not a
        guarantee: it under-counts quota in a narrow, already-degraded case
        (a run the user aborted, or a connection that kept failing) rather
        than over-counting it on the common paths. The reverse bias would
        burn slots on every cancelled run.

        What "error" DOES guarantee is the direction that matters most: a
        URL that never left this machine is never reported as "timeout".
        Against a per-property budget of roughly eleven a day, charging a
        slot for a URL Google never saw is a bug the user pays for.
        Anything that changes these two returns has to change the
        disposition table with it.

        Caveat, deliberate: re-sending can submit the same URL twice in the
        rare case where a click landed in the instant before the drop,
        costing one extra quota slot. Not killing the batch on a blip is
        worth more than that slot.
        """
        cmd_id = uuid.uuid4().hex
        waiter: dict = {"event": threading.Event(), "outcome": None}
        with self._pending_lock:
            self._pending[cmd_id] = waiter
        deadline = time.monotonic() + timeout
        resends = 0
        # Whether this command ever reached the wire. It is the ONLY thing
        # that entitles an expiry to report "timeout" rather than "error" —
        # see the docstring: one spends a quota slot and the other does not.
        sent = False
        try:
            while True:
                # `timeout` is the ceiling on the WHOLE call, so the grace
                # period is clipped to whatever is left of it. Waiting the
                # full grace on every attempt overran `timeout` by up to
                # reconnect_grace x (max_resends + 1) — a 60s budget could
                # sit here for eight minutes.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._expired(timeout, sent)
                conn, gen = self._wait_for_conn(min(reconnect_grace, remaining))
                if conn is None:
                    if self._stopped:
                        return "error"
                    if time.monotonic() >= deadline:
                        return self._expired(timeout, sent)
                    log.warning("bridge: no extension connection within %ss "
                                "— giving up on this URL", reconnect_grace)
                    return "error"
                try:
                    conn.send(make_submit(cmd_id, gsc_property, url, authuser))
                    sent = True
                except Exception as exc:  # noqa: BLE001 — a drop, not a bug
                    log.warning("bridge: send failed (%s); awaiting reconnect",
                                type(exc).__name__)
                    if resends >= max_resends:
                        return "error"
                    resends += 1
                    continue

                while True:
                    if waiter["event"].wait(2.0):
                        return map_outcome(waiter["outcome"])
                    if self._stopped:
                        return "error"
                    if time.monotonic() >= deadline:
                        return self._expired(timeout, sent)
                    if self._connection_bounced(conn, gen):
                        # The connection this command was sent on is gone, so
                        # the in-page job died with the old worker; re-send
                        # once a fresh connection is live.
                        if resends >= max_resends:
                            return "error"
                        resends += 1
                        log.warning("bridge: connection bounced mid-submit — "
                                    "re-sending (resend %d)", resends)
                        break
        finally:
            with self._pending_lock:
                self._pending.pop(cmd_id, None)

    def _expired(self, timeout: int, sent: bool) -> str:
        """The `timeout` budget ran out. Which of the two that is depends
        entirely on whether anything was ever put on the wire."""
        if sent:
            log.warning("bridge: no result within %ss", timeout)
            return "timeout"
        log.warning("bridge: no extension connection within the %ss budget "
                    "— nothing was sent for this URL", timeout)
        return "error"

    def _connection_bounced(self, conn: object, gen: int) -> bool:
        """Has the connection a command was sent on gone away?

        Both halves matter. A generation change catches the extension
        coming BACK; `self._conn is not conn` catches it merely leaving,
        which is the commoner case and the one that used to leave a URL
        sitting in submit() for the whole `timeout` — up to 25 minutes on
        the default — instead of failing inside `reconnect_grace`.
        """
        if self._gen != gen:
            return True
        with self._conn_lock:
            return self._conn is not conn

    # -- pairing ----------------------------------------------------------
    def _handle_pair(self, conn: object, message: dict) -> None:
        """Answer a pair_request, then close either way: the extension saves
        the token and reconnects with an ordinary hello."""
        origin = None
        try:
            origin = conn.request.headers.get("Origin")
        except Exception as exc:  # noqa: BLE001 — an absent header is normal
            log.debug("bridge: no Origin header (%s)", type(exc).__name__)
        claimed = str(message.get("extension_id") or "")

        if self.target is None:
            allowed, reason = False, ("this bridge was started without a "
                                      "browser target — run gsc_setup first")
        else:
            allowed, reason = pairing.verify_pair_request(
                self.target.installed, self.target.profile, claimed, origin)

        if allowed:
            log.info("bridge: paired extension %s", claimed)
            reply = {"type": "pair_ok", "token": self.token}
        else:
            # `reason` is NOT logged. verify_pair_request's commonest denial
            # quotes extension_dir(), an absolute path under the user's home
            # — i.e. their account name — and this logger writes to a file.
            # The reason still travels on the wire, where the extension shows
            # it to the user who already owns that path; a log file is the
            # copy that outlives the session and gets pasted into bug
            # reports, so it gets the fixed string.
            log.warning("bridge: pair_request from %r DENIED "
                        "(reason sent to the extension, not logged)", claimed)
            reply = {"type": "pair_denied", "reason": reason}
        for step in (lambda: conn.send(json.dumps(reply)), conn.close):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 — the peer may be gone
                log.debug("bridge: pair reply step ignored %s",
                          type(exc).__name__)

    # -- connection handler (one per client; only the authed one is kept) --
    def _handler(self, conn: object) -> None:
        try:
            hello = parse_message(conn.recv(timeout=10))
        except Exception as exc:  # noqa: BLE001 — a client that never spoke
            log.debug("bridge: client sent no usable hello (%s)",
                      type(exc).__name__)
            return

        if hello and hello.get("type") == "pair_request":
            self._handle_pair(conn, hello)
            return

        if not self._token_matches(hello):
            try:
                conn.send(json.dumps({"type": "hello_denied"}))
            except Exception as exc:  # noqa: BLE001 — the peer may be gone
                log.debug("bridge: could not send hello_denied (%s)",
                          type(exc).__name__)
            # Neither the expected token nor the supplied one is logged,
            # not even a fragment or a length: this is the one credential
            # that lets a local process drive a signed-in browser.
            log.warning("bridge: rejected a connection (bad or missing token)")
            return

        try:
            conn.send(json.dumps({"type": "hello_ok"}))
        except Exception as exc:  # noqa: BLE001 — it hung up mid-handshake
            log.debug("bridge: could not acknowledge a hello (%s)",
                      type(exc).__name__)
            return
        with self._conn_cv:
            self._conn = conn
            self._gen += 1
            # Inside the lock, with the _conn publication it belongs to.
            # Set outside it, a handler parked in the gap while stop() runs
            # re-set this flag AFTER stop() had cleared it, leaving
            # wait_for_extension() answering True for a dead session. The
            # ordering is strictly _conn_lock -> Event and never the
            # reverse, so there is no deadlock to trade for it.
            self._connected.set()
            self._conn_cv.notify_all()
        log.info("bridge: extension connected")

        try:
            while not self._stopped:
                # BOUNDED, deliberately. An untimed recv() parks this thread
                # until the socket closes, and websockets' handler threads
                # are not daemons — so on any release before 17.0, where
                # Server.shutdown() only closes the LISTENING socket and does
                # not touch established connections, a superseded connection
                # would keep the interpreter alive after stop(). Polling
                # _stopped once a second makes teardown independent of which
                # websockets release is installed.
                try:
                    raw = conn.recv(timeout=1.0)
                except TimeoutError:
                    continue
                message = parse_message(raw)
                if message is None:
                    continue
                self._dispatch(conn, message)
        except Exception as exc:  # noqa: BLE001 — a closed socket ends the loop
            log.debug("bridge: connection loop ended (%s)", type(exc).__name__)
        finally:
            with self._conn_cv:
                if self._conn is conn:
                    self._conn = None
                    # Only clear the ready-signal while this connection is
                    # still the active one. A reconnecting extension may
                    # already have set it for the NEW connection; clearing
                    # unconditionally would hide that and stall
                    # wait_for_extension.
                    self._connected.clear()
                self._conn_cv.notify_all()
            log.info("bridge: extension disconnected")

    def _token_matches(self, hello: dict | None) -> bool:
        """A well-formed hello carrying the right token, in constant time."""
        if not hello or hello.get("type") != "hello":
            return False
        supplied = hello.get("token")
        if not isinstance(supplied, str):
            return False
        return secrets.compare_digest(supplied, self.token)

    def _dispatch(self, conn: object, message: dict) -> None:
        kind = message.get("type")
        if kind == "ping":
            conn.send(json.dumps({"type": "pong"}))
        elif kind == "progress":
            log.info("bridge progress [%s]: %s",
                     message.get("id", "?"), message.get("stage"))
        elif kind == "result":
            # `detail` carries the extension's diagnostics. Dropping it once
            # made a silent-success path impossible to audit.
            detail = message.get("detail")
            if detail:
                log.info("bridge result [%s]: %s :: %s", message.get("id", "?"),
                         message.get("outcome"), detail)
            with self._pending_lock:
                waiter = self._pending.pop(message.get("id"), None)
            if waiter:
                waiter["outcome"] = message.get("outcome")
                waiter["event"].set()
        elif kind == "cancel":
            self.cancel()


def _browser_running(target: object) -> bool:
    """Is the target browser's process already up?

    Split out as its own function purely so tests can stub it: the real
    check walks the process table, which no unit test should depend on.
    """
    exe_name = Path(target.installed.exe_path).name.lower()
    try:
        import psutil  # noqa: PLC0415 — optional; absence is not an error
    except ImportError:
        return False
    try:
        return any(p.name().lower() == exe_name
                   for p in psutil.process_iter(["name"]))
    except Exception as exc:  # noqa: BLE001 — a probe, never fatal
        log.debug("could not scan processes (%s)", type(exc).__name__)
        return False


def ensure_browser_open(target: object, *, auto_launch: bool) -> bool:
    """Launch the target browser if it is not running. Returns True if launched.

    Detached, so it outlives this process, and into the MAIN profile — the
    extension's storage (token, port) is per browser profile and does not
    travel between brands or profiles.
    """
    if not auto_launch or _browser_running(target):
        return False
    log.info("auto_launch_browser: starting %s", target.installed.brand.label)
    try:
        subprocess.Popen(
            [target.installed.exe_path],
            # Both flags are Windows-only; getattr keeps this importable and
            # runnable on POSIX, where a bare attribute access raises.
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            close_fds=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 — the user can open it themselves
        # Type name only: an OSError from Popen quotes the executable path,
        # which on Windows contains the user's account name.
        log.warning("could not launch %s (%s)",
                    target.installed.brand.label, type(exc).__name__)
        return False
    return True


@contextmanager
def bridge_session(target: object, cfg: dict) -> Iterator[BridgeSession]:
    """Start the bridge, wait for the extension, yield the session, always stop.

    Raises RuntimeError if the extension never connects, which the calling
    tool surfaces as a clean run error rather than a stack trace.
    """
    launched = ensure_browser_open(
        target, auto_launch=cfg.get("auto_launch_browser", True))
    base_wait = int(cfg.get("bridge_connect_timeout", 60))
    total_wait = base_wait * 2 if launched else base_wait

    session = BridgeSession(port=int(cfg.get("bridge_port", 8765)),
                            token=load_or_create_token(),
                            connect_timeout=total_wait,
                            target=target)
    thread = threading.Thread(target=session.start, daemon=True)
    thread.start()
    session.ready.wait(10)
    log.info("bridge listening on ws://127.0.0.1:%s (waiting up to %ss for "
             "the extension)", session.port, total_wait)
    try:
        # An awake extension connects within a second. One whose MV3 worker
        # has been evicted only wakes on its own 30s alarm — so if the fast
        # path misses, poke it by opening one of its own pages, which starts
        # the worker at once. connect.js closes that tab again.
        first_wait = 20 if launched else 8
        if not session.wait_for_extension(min(first_wait, total_wait)):
            poke = pairing.wake(target.installed, target.profile)
            log.info("extension asleep after %ss — %s", first_wait,
                     "waking it via its own page" if poke.get("ok")
                     else f"could not wake it: {poke.get('hint')}")
            if not session.wait_for_extension(max(total_wait - first_wait, 15)):
                raise RuntimeError(
                    f"the extension never connected — open "
                    f"{target.installed.brand.label} with the GSC MCP Bridge "
                    "extension enabled, then try again")
        yield session
    finally:
        session.stop()
        thread.join(10)
