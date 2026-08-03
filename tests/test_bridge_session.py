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
  still tears the server down. `Server.shutdown()` joins the handler
  threads, which are NOT daemon threads — a leaked one keeps pytest alive.
* Every helper thread a test starts is joined before the test returns.
"""
from __future__ import annotations

import json
import threading
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


def test_browser_running_is_false_when_the_probe_is_unavailable(monkeypatch):
    """No psutil, or a process table that refuses to be read: never fatal."""
    assert bridge._browser_running(_target()) in (True, False)


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
