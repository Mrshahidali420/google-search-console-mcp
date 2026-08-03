"""The live bridge: a localhost WebSocket server and the one submit RPC.

Every test here drives a REAL `websockets` client against a REAL server, so
the frame vocabulary pinned below is the same one the shipped extension
speaks. A mock on either side would pass while the wire disagreed.

Determinism rules this file follows, because a hanging test in CI is worse
than a failing one:

* Port 0. The OS picks a free port and `BridgeSession` reports the one it
  actually bound, so several CI jobs on one machine cannot collide. Picking
  a port with a bound-then-closed probe socket leaves a window in which
  another process takes it.
* `session.ready` rather than a connect-poll loop. The server sets it once
  the socket is bound; waiting on it is exact and costs no retries.
* The fixture stops the session in a `finally`, so a failing assertion
  still tears the server down, and joins the server thread. `websockets`
  handler threads are NOT daemon threads, and only 17.0 makes
  `Server.shutdown()` close established connections as well as the
  listening socket — which is why the read loop polls with a bounded
  `recv()` rather than trusting shutdown to unblock it.
* Every helper thread a test starts is joined before the test returns.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from websockets.sync.client import connect as ws_connect

from _logcheck import Captured

from gsc_core import bridge

TOKEN = "test-token-not-a-real-secret"

#: Long enough that a loaded CI box never trips it, short enough that a
#: genuine deadlock fails the run instead of stalling it.
SETTLE = 5


@pytest.fixture
def session():
    sess = bridge.BridgeSession(port=0, token=TOKEN, connect_timeout=SETTLE)
    thread = threading.Thread(target=sess.start, daemon=True)
    thread.start()
    assert sess.ready.wait(SETTLE), "the server never bound its socket"
    try:
        yield sess
    finally:
        sess.stop()
        thread.join(SETTLE)


def _hello(sess) -> object:
    conn = ws_connect(f"ws://127.0.0.1:{sess.port}")
    conn.send(json.dumps({"type": "hello", "token": TOKEN, "version": "1.5.0"}))
    assert json.loads(conn.recv())["type"] == "hello_ok"
    return conn


def _responder(conn, reply) -> threading.Thread:
    """Answer exactly one command, in a thread the caller joins."""
    def run() -> None:
        command = json.loads(conn.recv())
        conn.send(json.dumps(reply(command)))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------- the handshake

def test_a_wrong_token_is_denied_and_never_becomes_the_live_connection(session):
    conn = ws_connect(f"ws://127.0.0.1:{session.port}")
    conn.send(json.dumps({"type": "hello", "token": "wrong"}))
    assert json.loads(conn.recv())["type"] == "hello_denied"
    conn.close()
    assert session.wait_for_extension(0.5) is False


def test_a_denied_hello_never_logs_either_side_of_the_comparison(session):
    with Captured(bridge.log) as records:
        conn = ws_connect(f"ws://127.0.0.1:{session.port}")
        conn.send(json.dumps({"type": "hello", "token": "wrong"}))
        assert json.loads(conn.recv())["type"] == "hello_denied"
        conn.close()
        assert session.wait_for_extension(0.5) is False
    assert records, "a rejected connection should have been logged at all"
    assert TOKEN not in records.text
    assert "wrong" not in records.text


def test_a_first_frame_that_is_not_json_is_dropped_without_raising(session):
    conn = ws_connect(f"ws://127.0.0.1:{session.port}")
    conn.send("not json at all")
    assert json.loads(conn.recv())["type"] == "hello_denied"
    conn.close()
    assert session.wait_for_extension(0.5) is False


def test_the_bound_port_is_reported_back(session):
    assert session.port > 0


# -------------------------------------------------------------------- submit

def test_a_submit_round_trips_its_outcome(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    thread = _responder(conn, lambda c: {"type": "result", "id": c["id"],
                                         "outcome": "submitted"})
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=10) == "submitted"
    thread.join(SETTLE)
    conn.close()


def test_the_submit_frame_carries_the_fields_the_extension_reads(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    seen: list[dict] = []

    def reply(command: dict) -> dict:
        seen.append(command)
        return {"type": "result", "id": command["id"], "outcome": "submitted"}

    thread = _responder(conn, reply)
    session.submit("sc-domain:example.net", "https://example.net/x", "3",
                   timeout=10)
    thread.join(SETTLE)
    assert seen[0]["type"] == "submit"
    assert seen[0]["property"] == "sc-domain:example.net"
    assert seen[0]["url"] == "https://example.net/x"
    assert seen[0]["authuser"] == "3"
    conn.close()


def test_an_invented_outcome_is_coerced_to_error(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    thread = _responder(conn, lambda c: {"type": "result", "id": c["id"],
                                         "outcome": "everything is fine"})
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=10) == "error"
    thread.join(SETTLE)
    conn.close()


def test_a_result_for_an_unknown_id_does_not_resolve_the_waiter(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    thread = _responder(conn, lambda c: {"type": "result", "id": "someone-elses",
                                         "outcome": "submitted"})
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=3) == "timeout"
    thread.join(SETTLE)
    conn.close()


def test_no_result_within_the_timeout_reports_timeout(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=2, reconnect_grace=1) == "timeout"
    conn.close()


def test_submit_with_no_extension_gives_up_after_the_grace_period(session):
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=30, reconnect_grace=1) == "error"


def test_submit_never_overruns_its_own_timeout(session):
    """`timeout` is the ceiling on the WHOLE call, not on one attempt.

    With the grace period longer than the timeout, waiting the full grace
    before consulting the deadline overran the budget by up to
    reconnect_grace x (max_resends + 1) — here, 6s against a 1s ceiling.
    """
    started = time.monotonic()
    session.submit("sc-domain:example.com", "https://example.com/a",
                   "0", timeout=1, reconnect_grace=6)
    elapsed = time.monotonic() - started
    assert elapsed < 3, f"submit overran its 1s timeout by {elapsed:.2f}s"


def test_a_url_that_was_never_sent_is_never_reported_as_a_timeout(session):
    """The quota-charging distinction, pinned.

    submit.py's disposition table charges a Search Console slot for
    "timeout" (a click plausibly reached Google) and nothing for "error".
    A URL that never left this machine because no extension ever connected
    must therefore report "error". Against a per-property budget of about
    eleven a day, getting this backwards spends real slots on URLs Google
    never saw.
    """
    # The deadline expires while waiting for a connection: the grace period
    # is deliberately the longer of the two, so this exits via the budget.
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=1, reconnect_grace=6) == "error"
    # And the same via the grace period, with the budget intact.
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=30, reconnect_grace=1) == "error"


def test_a_send_that_raised_and_then_expired_is_an_error_not_a_timeout(session):
    """`sent` must be written AFTER conn.send() returns, not before.

    A connection that is live but whose send() raises never put a frame on
    the wire. Moving `sent = True` one line earlier passes every other test
    in this file and turns this case into "timeout" — which charges a quota
    slot for a URL Google never saw. The send is slowed so the budget
    expires before the resend count does, forcing the expiry path rather
    than the resends-exhausted one.
    """
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    def _slow_failing_send(_frame: str) -> None:
        time.sleep(0.3)
        raise OSError("the socket went away")

    session._conn.send = _slow_failing_send
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=1, reconnect_grace=6,
                          max_resends=50) == "error"
    conn.close()


def test_a_sent_command_whose_verdict_never_arrives_is_a_timeout(session):
    """The other half of the same distinction: this one DOES spend a slot."""
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True
    thread = _responder(conn, lambda c: {"type": "progress", "id": c["id"],
                                         "stage": "navigating"})
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=2, reconnect_grace=10) == "timeout"
    thread.join(SETTLE)
    conn.close()


def test_progress_and_ping_frames_do_not_disturb_an_in_flight_submit(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    def run() -> None:
        command = json.loads(conn.recv())
        conn.send(json.dumps({"type": "progress", "id": command["id"],
                              "stage": "navigating"}))
        conn.send(json.dumps({"type": "ping"}))
        assert json.loads(conn.recv())["type"] == "pong"
        conn.send(json.dumps({"type": "result", "id": command["id"],
                              "outcome": "already_indexed"}))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=15) == "already_indexed"
    thread.join(SETTLE)
    conn.close()


def test_a_reconnect_mid_flight_resends_on_the_new_connection(session):
    first = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    resent: list[dict] = []

    def flow() -> None:
        first.recv()          # take the command, answer nothing
        first.close()         # the worker died with the connection
        second = _hello(session)
        resent.append(json.loads(second.recv()))
        second.send(json.dumps({"type": "result", "id": resent[0]["id"],
                                "outcome": "submitted"}))

    thread = threading.Thread(target=flow, daemon=True)
    thread.start()
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=30, reconnect_grace=10) == "submitted"
    thread.join(SETTLE)
    assert resent, "the command must be re-sent on the fresh connection"


def test_the_resends_run_out_rather_than_looping_forever(session):
    """A connection that bounces without ever answering must end the URL.

    max_resends=0 is the degenerate case, and it is the one that proves the
    counter is consulted at all: without it a permanently flapping
    extension would keep one URL in submit() until the caller's timeout.
    """
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    def flow() -> None:
        conn.recv()
        conn.close()

    thread = threading.Thread(target=flow, daemon=True)
    thread.start()
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=30, reconnect_grace=5,
                          max_resends=0) == "error"
    thread.join(SETTLE)


# ----------------------------------------------------------- stop and cancel

def test_stop_resolves_an_in_flight_waiter_instead_of_hanging(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True
    stopper = threading.Timer(0.5, session.stop)
    stopper.start()
    # timeout=600 — if stop() did not resolve the waiter this test would hang
    # for ten minutes rather than fail, which is the bug it exists to catch.
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=600, reconnect_grace=5) == "error"
    stopper.join(SETTLE)
    conn.close()


def test_cancel_resolves_waiters_and_stops_the_run(session):
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True
    canceller = threading.Timer(0.5, session.cancel)
    canceller.start()
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=600, reconnect_grace=5) == "error"
    canceller.join(SETTLE)
    conn.close()


def test_a_cancel_frame_from_the_extension_aborts_the_run(session):
    """The extension's 'Clear pending jobs' button, over the wire."""
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True

    def flow() -> None:
        conn.recv()
        conn.send(json.dumps({"type": "cancel"}))

    thread = threading.Thread(target=flow, daemon=True)
    thread.start()
    assert session.submit("sc-domain:example.com", "https://example.com/a",
                          "0", timeout=600, reconnect_grace=5) == "error"
    thread.join(SETTLE)
    conn.close()


def test_stop_is_safe_to_call_twice(session):
    session.stop()
    session.stop()


def test_a_stopped_session_no_longer_reports_an_extension(session):
    """stop() nulls _conn, so the handler cannot clear this signal itself.

    Left to the handler, wait_for_extension() keeps answering True for a
    session that is already shut down, and the caller's next loop iteration
    walks into a dead socket.
    """
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True
    session.stop()
    assert session.wait_for_extension(0.1) is False
    conn.close()


class _GatedEvent(threading.Event):
    """An Event whose set() parks until the test lets it through.

    The only way to hold a handler thread at one exact statement. It
    records when it reaches set() and when clear() runs, so the test
    sequences on those rather than on sleeps.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.cleared = threading.Event()

    def set(self) -> None:
        self.entered.set()
        self.gate.wait(SETTLE)
        super().set()

    def clear(self) -> None:
        self.cleared.set()
        super().clear()


def test_a_hello_landing_as_stop_runs_leaves_the_session_looking_dead():
    """The publish-then-signal window, held open on purpose.

    `_conn` and `_connected` have to be published together. While
    `_connected.set()` sat outside the lock, a handler parked in the gap
    when stop() ran re-set the flag after stop() had cleared it, and
    wait_for_extension() then answered True for a session whose socket was
    already closed — the caller's next URL walks straight into it.
    """
    sess = bridge.BridgeSession(port=0, token=TOKEN, connect_timeout=SETTLE)
    connected = _GatedEvent()
    sess._connected = connected
    server = threading.Thread(target=sess.start, daemon=True)
    server.start()
    assert sess.ready.wait(SETTLE)
    try:
        conn = _hello(sess)
        assert connected.entered.wait(SETTLE), "the handler never signalled"

        stopper = threading.Thread(target=sess.stop, daemon=True)
        stopper.start()
        # Unguarded, stop() clears straight away and this returns at once;
        # guarded, it is blocked on the lock the handler holds and this
        # waits out its bound. Either way the gate opens next, so the
        # assertion — not the timing — is what tells the two apart.
        connected.cleared.wait(1.0)
        connected.gate.set()
        stopper.join(SETTLE)

        assert sess.wait_for_extension(0.05) is False
        conn.close()
    finally:
        connected.gate.set()
        sess.stop()
        server.join(SETTLE)


def test_stopping_does_not_log_the_misleading_reconnect_warning(session):
    """"no extension connection within Ns" is a diagnosis, not a shutdown.

    Emitting it when the operator stopped the run sends whoever reads the
    log hunting a WebSocket fault that never happened.
    """
    conn = _hello(session)
    assert session.wait_for_extension(SETTLE) is True
    with Captured(bridge.log) as records:
        stopper = threading.Timer(0.5, session.stop)
        stopper.start()
        assert session.submit("sc-domain:example.com", "https://example.com/a",
                              "0", timeout=600, reconnect_grace=5) == "error"
        stopper.join(SETTLE)
    assert "no extension connection" not in records.text
    conn.close()


# ------------------------------------------------------------------- pairing

def test_pairing_is_refused_when_no_target_was_supplied(session):
    conn = ws_connect(f"ws://127.0.0.1:{session.port}")
    conn.send(json.dumps({"type": "pair_request", "extension_id": "a" * 32}))
    reply = json.loads(conn.recv())
    assert reply["type"] == "pair_denied"
    assert TOKEN not in json.dumps(reply)


def test_a_verified_pair_request_hands_the_token_over(monkeypatch):
    monkeypatch.setattr(bridge.pairing, "verify_pair_request",
                        lambda *a, **k: (True, "installed in this profile"))
    sess = bridge.BridgeSession(port=0, token=TOKEN,
                                target=SimpleNamespace(installed=object(),
                                                       profile=object()))
    thread = threading.Thread(target=sess.start, daemon=True)
    thread.start()
    assert sess.ready.wait(SETTLE)
    try:
        conn = ws_connect(f"ws://127.0.0.1:{sess.port}")
        conn.send(json.dumps({"type": "pair_request",
                              "extension_id": "a" * 32}))
        reply = json.loads(conn.recv())
        assert reply == {"type": "pair_ok", "token": TOKEN}
    finally:
        sess.stop()
        thread.join(SETTLE)


def test_a_denied_pair_request_never_leaks_the_token(monkeypatch):
    monkeypatch.setattr(bridge.pairing, "verify_pair_request",
                        lambda *a, **k: (False, "not that extension"))
    sess = bridge.BridgeSession(port=0, token=TOKEN,
                                target=SimpleNamespace(installed=object(),
                                                       profile=object()))
    thread = threading.Thread(target=sess.start, daemon=True)
    thread.start()
    assert sess.ready.wait(SETTLE)
    try:
        with Captured(bridge.log) as records:
            conn = ws_connect(f"ws://127.0.0.1:{sess.port}")
            conn.send(json.dumps({"type": "pair_request",
                                  "extension_id": "a" * 32}))
            reply = conn.recv()
        assert TOKEN not in reply
        assert "not that extension" in reply
        assert records, "a denied pairing should have been logged at all"
        assert TOKEN not in records.text
    finally:
        sess.stop()
        thread.join(SETTLE)


def test_a_denial_reason_carrying_a_home_path_never_reaches_the_log(monkeypatch):
    """The commonest denial quotes extension_dir() — an absolute path.

    On Windows that path contains the user's account name, and this
    logger writes to a file that outlives the session and gets pasted
    into bug reports. The reason still goes to the extension, which shows
    it to the one person who already knows the path.
    """
    reason = ("that is not the extension in this profile — load the one in "
              "C:\\Users\\a-real-person\\AppData\\Roaming\\gsc-mcp\\extension")
    monkeypatch.setattr(bridge.pairing, "verify_pair_request",
                        lambda *a, **k: (False, reason))
    sess = bridge.BridgeSession(port=0, token=TOKEN,
                                target=SimpleNamespace(installed=object(),
                                                       profile=object()))
    thread = threading.Thread(target=sess.start, daemon=True)
    thread.start()
    assert sess.ready.wait(SETTLE)
    try:
        with Captured(bridge.log) as records:
            conn = ws_connect(f"ws://127.0.0.1:{sess.port}")
            conn.send(json.dumps({"type": "pair_request",
                                  "extension_id": "a" * 32}))
            reply = conn.recv()
        assert records, "a denied pairing should have been logged at all"
        assert "a-real-person" not in records.text
        assert "AppData" not in records.text
        # The extension is still told why, or the user cannot act on it.
        assert "a-real-person" in reply
    finally:
        sess.stop()
        thread.join(SETTLE)


# ------------------------------------------------------ ensure_browser_open

def _target(exe_path: str = "C:/x/chrome.exe"):
    return SimpleNamespace(
        installed=SimpleNamespace(exe_path=exe_path,
                                  brand=SimpleNamespace(label="Test Browser")),
        profile=SimpleNamespace(directory="Default"))


def _explode(*args, **kwargs):
    raise AssertionError("must not launch")


def _explode_with_path(*args, **kwargs):
    raise OSError("C:/Users/a-real-person/chrome.exe")


def test_ensure_browser_open_does_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr(bridge.subprocess, "Popen", _explode)
    assert bridge.ensure_browser_open(_target(), auto_launch=False) is False


def test_ensure_browser_open_skips_a_running_browser(monkeypatch):
    monkeypatch.setattr(bridge, "_browser_running", lambda target: True)
    monkeypatch.setattr(bridge.subprocess, "Popen", _explode)
    assert bridge.ensure_browser_open(_target(), auto_launch=True) is False


def test_ensure_browser_open_launches_detached(monkeypatch):
    monkeypatch.setattr(bridge, "_browser_running", lambda target: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(bridge.subprocess, "Popen",
                        lambda argv, **k: calls.append(argv))
    assert bridge.ensure_browser_open(_target(), auto_launch=True) is True
    assert calls == [["C:/x/chrome.exe"]]


def test_ensure_browser_open_survives_a_launch_failure(monkeypatch):
    """The OSError message embeds a path — i.e. the user's account name."""
    monkeypatch.setattr(bridge, "_browser_running", lambda target: False)
    monkeypatch.setattr(bridge.subprocess, "Popen", _explode_with_path)
    with Captured(bridge.log) as records:
        assert bridge.ensure_browser_open(_target(), auto_launch=True) is False
    assert records, "a failed launch should have been logged at all"
    assert "a-real-person" not in records.text


def test_browser_running_is_false_when_psutil_is_absent(monkeypatch):
    """psutil is optional and deliberately undeclared: absence is not an error."""
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert bridge._browser_running(_target()) is False


def test_browser_running_is_false_when_the_process_table_refuses(monkeypatch):
    """A probe, never fatal — and the failure must not name a path."""
    fake = SimpleNamespace(process_iter=_explode_with_path)
    monkeypatch.setitem(sys.modules, "psutil", fake)
    with Captured(bridge.log) as records:
        assert bridge._browser_running(_target()) is False
    assert "a-real-person" not in records.text


def test_browser_running_matches_on_the_executable_name(monkeypatch):
    """Case-insensitively, and on the BASENAME — not the full path."""
    running = SimpleNamespace(name=lambda: "CHROME.EXE")
    fake = SimpleNamespace(process_iter=lambda attrs=None: [running])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert bridge._browser_running(_target()) is True
    assert bridge._browser_running(_target("D:/other/firefox.exe")) is False


# ---------------------------------------------------------- bridge_session

def test_bridge_session_yields_a_live_session_and_always_stops_it(monkeypatch):
    monkeypatch.setattr(bridge, "ensure_browser_open",
                        lambda target, *, auto_launch: False)
    monkeypatch.setattr(bridge, "load_or_create_token", lambda: TOKEN)
    monkeypatch.setattr(bridge.BridgeSession, "wait_for_extension",
                        lambda self, timeout=None: True)
    seen: list[bridge.BridgeSession] = []

    with bridge.bridge_session(_target(), {"bridge_port": 0,
                                           "bridge_connect_timeout": 1}) as sess:
        seen.append(sess)
        assert sess.ready.wait(SETTLE)
    assert seen and seen[0]._stopped is True


def test_bridge_session_raises_a_readable_error_when_nothing_connects(monkeypatch):
    monkeypatch.setattr(bridge, "ensure_browser_open",
                        lambda target, *, auto_launch: False)
    monkeypatch.setattr(bridge, "load_or_create_token", lambda: TOKEN)
    monkeypatch.setattr(bridge.pairing, "wake",
                        lambda installed, profile: {"ok": False,
                                                    "hint": "no extension"})
    monkeypatch.setattr(bridge.BridgeSession, "wait_for_extension",
                        lambda self, timeout=None: False)
    with pytest.raises(RuntimeError) as excinfo:
        with bridge.bridge_session(_target(), {"bridge_port": 0,
                                               "bridge_connect_timeout": 1}):
            pass
    assert "never connected" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value)
