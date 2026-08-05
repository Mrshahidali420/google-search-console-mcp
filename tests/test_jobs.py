"""Background submission jobs: the worker, the registry and the stop path.

Every test here pins a CONSEQUENCE for the operator, because a job spends a
real and unrecoverable resource without anyone watching. The four that
matter most:

* a job that ends must leave a TERMINAL row — a worker that dies with the
  row still marked running is a job that never finishes and a registry
  slot that never frees, and "one job at a time" then means "no job ever
  again" until the process restarts;
* the stored error and every log line carry the exception TYPE NAME only,
  because an unauthored message routinely carries a filesystem path
  holding the operator's account name;
* the guard against a second job is ATOMIC — check-then-act would let two
  racing callers both open a row and both drive the single browser tab;
* a stop lands promptly and BEFORE the next reservation, so it costs no
  quota slot.

NOTHING HERE SLEEPS FOR REAL. The gap between two sends is 130-180 seconds
live; the worker is handed an interruptible gap and the tests either
replace the run outright or assert on the gap function directly. Every
wait is an Event or a join with a timeout, so a wedged worker reddens a
test instead of hanging the suite.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable

import pytest

from _logcheck import capturing, logged_text
from gsc_core import store, submit
from gsc_mcp import jobs

PROPERTY = "sc-domain:example.com"

CFG: dict = {"submit_delay_range": [0, 0], "property_slots": 11,
             "account_slots": None, "daily_reserve": 0,
             "stop_on_throttle": True, "authuser": "0"}

# The shapes a leak would take: an OS account name in a path, and an
# address. Neither may appear in a stored error or in a log record.
SECRET_PATH = "C:/Users/secret-operator/thing"
SECRET_EMAIL = "owner@example.net"

_JOIN_TIMEOUT = 10.0


@pytest.fixture(autouse=True)
def _clean_registry():
    """No job may outlive its test, whatever the test did to the registry."""
    yield
    for job_id in list(jobs._threads):
        jobs.stop(job_id)
        jobs.join(job_id, timeout=_JOIN_TIMEOUT)
    jobs._threads.clear()
    jobs._stop_events.clear()
    # The bridge claim too: a claim left standing by a test that failed
    # part-way would refuse every submission in every test after it, and
    # the failure would land on an innocent test.
    jobs._holder = None


def _await_terminal(job_id: str) -> None:
    assert jobs.join(job_id, timeout=_JOIN_TIMEOUT), \
        f"job {job_id} did not finish within {_JOIN_TIMEOUT}s"


def _read(job_id: str) -> dict:
    with store.session() as conn:
        job = store.get_job(conn, job_id)
    assert job is not None
    return job


def _attempt(url: str, outcome: str) -> submit.Attempt:
    return submit.Attempt(url, PROPERTY, outcome, outcome == "submitted")


def _fake_execute(outcomes: list[str], stop_reason: str | None = None):
    """A run that reports scripted outcomes without a browser or a socket.

    It drives the caller's on_progress exactly as submit.run does, so the
    progress-recording code under test is the real one.
    """
    def execute(conn, job_id, urls, cfg, stop_event, on_progress):
        attempts = []
        for url, outcome in zip(urls, outcomes):
            attempt = _attempt(url, outcome)
            attempts.append(attempt)
            on_progress(attempt)
        return submit.RunResult(attempts, stop_reason is not None, stop_reason)
    return execute


def _boom_execute():
    """A run that dies the way a real one would: with a path in the text."""
    def execute(*args, **kwargs):
        raise RuntimeError(SECRET_PATH)
    return execute


def _blocking_execute(entered: threading.Event | None = None):
    """A run that sits still until it is asked to stop, then reports so."""
    def execute(conn, job_id, urls, cfg, stop_event, on_progress):
        if entered is not None:
            entered.set()
        assert stop_event.wait(_JOIN_TIMEOUT), "the worker was never stopped"
        return submit.RunResult([], True, "stopped_by_user")
    return execute


# --- the happy path ----------------------------------------------------------

def test_a_job_runs_to_completion_and_records_progress(monkeypatch, home):
    monkeypatch.setattr(jobs, "_execute",
                        _fake_execute(["submitted", "submitted"]))
    job_id = jobs.start(["https://example.com/a", "https://example.com/b"], CFG)
    _await_terminal(job_id)

    job = _read(job_id)
    assert job["state"] == "completed"
    assert job["progress"]["done"] == 2
    assert job["progress"]["total"] == 2
    assert [row["url"] for row in job["progress"]["results"]] == [
        "https://example.com/a", "https://example.com/b"]
    assert job["finished_at"] is not None


def test_progress_is_visible_while_the_job_is_still_running(monkeypatch, home):
    """Polling mid-run is the whole point: progress must be written as it
    happens, not assembled once at the end."""
    seen = threading.Event()
    release = threading.Event()

    def execute(conn, job_id, urls, cfg, stop_event, on_progress):
        attempt = _attempt(urls[0], "submitted")
        on_progress(attempt)
        seen.set()
        assert release.wait(_JOIN_TIMEOUT)
        return submit.RunResult([attempt], False, None)

    monkeypatch.setattr(jobs, "_execute", execute)
    job_id = jobs.start(["https://example.com/a"], CFG)
    assert seen.wait(_JOIN_TIMEOUT)

    midway = _read(job_id)
    assert midway["state"] == "running"
    assert midway["progress"]["done"] == 1
    assert jobs.is_running(job_id) is True

    release.set()
    _await_terminal(job_id)
    assert jobs.is_running(job_id) is False


def test_the_urls_are_recorded_on_the_job_row(monkeypatch, home):
    """The row must say what was asked for, not only what has happened —
    a job read back after a restart has no other record of its input."""
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)
    assert _read(job_id)["params"]["urls"] == ["https://example.com/a"]


# --- one at a time -----------------------------------------------------------

def test_a_second_job_is_refused_while_one_is_live(monkeypatch, home):
    monkeypatch.setattr(jobs, "_execute", _blocking_execute())
    first = jobs.start(["https://example.com/a"], CFG)

    with pytest.raises(jobs.AlreadyRunning) as caught:
        jobs.start(["https://example.com/b"], CFG)
    assert first in str(caught.value)

    jobs.stop(first)
    _await_terminal(first)


def test_the_refused_second_job_leaves_no_row_behind(monkeypatch, home):
    """A refusal must cost nothing. A row written before the guard would
    show up in job_status as a job that never ran."""
    monkeypatch.setattr(jobs, "_execute", _blocking_execute())
    first = jobs.start(["https://example.com/a"], CFG)
    with pytest.raises(jobs.AlreadyRunning):
        jobs.start(["https://example.com/b"], CFG)

    with store.session() as conn:
        assert [job["id"] for job in store.list_jobs(conn)] == [first]

    jobs.stop(first)
    _await_terminal(first)


def test_two_racing_starts_yield_exactly_one_job(monkeypatch, home):
    """The guard is atomic, not check-then-act.

    Two threads reach start() together. A check outside the lock — or a
    create_job outside it — leaves two rows and two workers fighting over
    one browser tab and one bridge port.
    """
    monkeypatch.setattr(jobs, "_execute", _blocking_execute())
    ready = threading.Barrier(2)
    started: list[str] = []
    refused: list[Exception] = []
    guard = threading.Lock()

    def race() -> None:
        ready.wait(_JOIN_TIMEOUT)
        try:
            job_id = jobs.start(["https://example.com/a"], CFG)
        except jobs.AlreadyRunning as exc:
            with guard:
                refused.append(exc)
        else:
            with guard:
                started.append(job_id)

    racers = [threading.Thread(target=race) for _ in range(2)]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(_JOIN_TIMEOUT)
        assert not racer.is_alive()

    assert len(started) == 1 and len(refused) == 1
    with store.session() as conn:
        assert len(store.list_jobs(conn)) == 1

    jobs.stop(started[0])
    _await_terminal(started[0])


def test_a_job_cannot_start_while_a_synchronous_run_holds_the_bridge(home):
    """The guard covers both entry points, not only jobs.

    Without this, a job started during a gsc_request_indexing call binds a
    port that is already taken, inside a daemon thread where the OSError is
    invisible — and the job dies reporting an extension problem it does not
    have.
    """
    with jobs.claim_bridge():
        with pytest.raises(jobs.AlreadyRunning) as caught:
            jobs.start(["https://example.com/a"], CFG)
        # Nothing about the refusal names anything but the tool.
        assert "gsc_request_indexing" in str(caught.value)
        assert SECRET_EMAIL not in str(caught.value)
        # And it cost nothing: no row, no worker.
        with store.session() as conn:
            assert store.list_jobs(conn) == []
        assert jobs._threads == {}


def test_a_synchronous_run_cannot_claim_the_bridge_while_a_job_is_live(
        monkeypatch, home):
    monkeypatch.setattr(jobs, "_execute", _blocking_execute())
    first = jobs.start(["https://example.com/a"], CFG)

    with pytest.raises(jobs.AlreadyRunning) as caught:
        with jobs.claim_bridge():
            pytest.fail("the synchronous run took a bridge a job was holding")
    assert first in str(caught.value)

    jobs.stop(first)
    _await_terminal(first)
    # Released with the job: the next synchronous run gets straight through.
    with jobs.claim_bridge():
        pass


def test_the_claim_is_released_even_when_the_held_block_raises(home):
    with pytest.raises(ValueError):
        with jobs.claim_bridge():
            raise ValueError("the run blew up")
    with jobs.claim_bridge():
        pass


def test_a_dead_worker_cannot_wedge_the_bridge_permanently(home):
    """A claim is not a latch. If a worker were ever to die without running
    its finally, the next caller must still be able to submit — one refusal
    is a bug, every submission until a restart is a broken install."""
    jobs._holder = "a-job-that-is-no-longer-running"
    with jobs.claim_bridge():
        pass


# Contenders per round of the cross-entry-point race — one job start and
# this many synchronous claims — and how many rounds of it.
#
# Measured against the mutant rather than guessed, because the window a
# check-then-act claim opens is a few bytecodes wide and stating the
# intent is not the same as catching it. Against a deliberately
# check-then-act claim_bridge: one job versus one sync claim at a plain
# Barrier was caught in 0 runs out of 10; the shape below — spin line-up,
# five sync contenders, a short thread-switch interval, this many rounds —
# was caught in 10 out of 10. It costs about 1.5 seconds.
_RACE_SYNC_RACERS = 5
_RACE_ROUNDS = 250


def _race_once(round_number: int) -> str:
    """One contended round. Returns the winner: a job id, or "sync".

    Every contender arrives at the same Barrier, and the winner holds its
    claim until every other contender has reported — so a loser is a loser
    because it was refused, never because the winner had already let go.
    """
    # The main thread is a party too, so it can flip `go` only once every
    # contender is already spinning on it. A Barrier alone is not enough:
    # its waiters wake one at a time off a condition variable, and the first
    # one through finishes its whole claim before the rest are scheduled —
    # which is exactly how a check-then-act claim survives an unaided race.
    # The bounded spin below keeps every contender RUNNABLE at the moment
    # the flag flips. It never sleeps and it is bounded by the barrier that
    # precedes it.
    ready = threading.Barrier(_RACE_SYNC_RACERS + 2)
    go = [False]
    release = threading.Event()
    settled = threading.Semaphore(0)
    won: list[str] = []
    refused: list[str] = []
    guard = threading.Lock()

    def line_up() -> None:
        ready.wait(_JOIN_TIMEOUT)
        while not go[0]:
            pass

    def run_job() -> None:
        line_up()
        try:
            job_id = jobs.start([f"https://example.com/{round_number}"], CFG)
        except jobs.AlreadyRunning:
            with guard:
                refused.append("job")
        else:
            with guard:
                won.append(job_id)
        settled.release()

    def run_sync() -> None:
        line_up()
        try:
            with jobs.claim_bridge():
                with guard:
                    won.append("sync")
                settled.release()
                assert release.wait(_JOIN_TIMEOUT)
        except jobs.AlreadyRunning:
            with guard:
                refused.append("sync")
            settled.release()

    racers = [threading.Thread(target=run_job)]
    racers += [threading.Thread(target=run_sync)
               for _ in range(_RACE_SYNC_RACERS)]
    for racer in racers:
        racer.start()
    ready.wait(_JOIN_TIMEOUT)   # everyone is spinning on `go`
    go[0] = True
    # A rendezvous, not a poll: every contender reports before the winner
    # is let go, so the claim really was contended.
    for _ in racers:
        assert settled.acquire(timeout=_JOIN_TIMEOUT), "a racer never settled"
    release.set()
    for racer in racers:
        racer.join(_JOIN_TIMEOUT)
        assert not racer.is_alive()

    assert len(won) == 1, f"round {round_number}: won={won} refused={refused}"
    assert len(refused) == len(racers) - 1
    return won[0]


def test_a_job_and_synchronous_runs_racing_yield_exactly_one_winner(
        monkeypatch, home):
    """Test-and-set, not check-then-act.

    Every contender arrives together at a Barrier. An implementation that
    looked at the flag and then set it lets more than one through, and they
    drive one browser tab through one port between them.
    """
    monkeypatch.setattr(jobs, "_execute", _blocking_execute())
    # A Barrier releases its waiters together, but the first one through
    # normally finishes its whole claim before the rest are even scheduled —
    # which is how a check-then-act claim passes an unaided race. Shortening
    # the interpreter's thread-switch interval for the duration puts real
    # preemption inside the window instead of hoping for it. Restored by the
    # fixture below whatever this test does.
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    job_wins = 0
    try:
        for round_number in range(_RACE_ROUNDS):
            winner = _race_once(round_number)
            if winner != "sync":
                job_wins += 1
                jobs.stop(winner)
                _await_terminal(winner)
            # The round settled completely: nothing holds the bridge, so
            # the next round starts where this one did.
            assert jobs._holder is None
    finally:
        sys.setswitchinterval(previous)

    # One row per round the job won, and not one more: a refused start must
    # leave nothing behind even when it lost by a hair.
    with store.session() as conn:
        assert len(store.list_jobs(conn)) == job_wins


def test_a_new_job_may_start_once_the_previous_one_has_finished(monkeypatch,
                                                                home):
    """The registry frees its slot. Otherwise "one at a time" degrades into
    "one ever" for the life of the process."""
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    first = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(first)

    second = jobs.start(["https://example.com/b"], CFG)
    _await_terminal(second)
    assert second != first
    assert _read(second)["state"] == "completed"


# --- stopping ----------------------------------------------------------------

def test_stop_moves_the_job_to_stopped_user(monkeypatch, home):
    monkeypatch.setattr(jobs, "_execute", _blocking_execute())
    job_id = jobs.start(["https://example.com/a"], CFG)
    assert jobs.stop(job_id) is True
    _await_terminal(job_id)
    assert _read(job_id)["state"] == "stopped_user"


def test_stopping_an_unknown_job_is_false_not_an_exception(home):
    assert jobs.stop("no-such-job") is False


def test_stopping_a_finished_job_is_false(monkeypatch, home):
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)
    assert jobs.stop(job_id) is False


def test_the_gap_between_sends_ends_the_moment_a_stop_arrives(home):
    """The stop must land INSIDE the multi-minute gap.

    submit.run re-checks should_stop after the gap and before reserve(), so
    a gap that ignores the signal costs a quota slot and fires one more
    submission after the user said stop. A plain time.sleep does exactly
    that; this gap is the event's own wait.
    """
    event = threading.Event()
    gap = jobs._interruptible_sleep(event)

    event.set()
    finished = threading.Event()

    def wait_out_a_full_gap() -> None:
        gap(3600.0)          # a real sleep would outlive the whole suite
        finished.set()

    thread = threading.Thread(target=wait_out_a_full_gap, daemon=True)
    thread.start()
    assert finished.wait(_JOIN_TIMEOUT), "the gap ignored the stop signal"


def test_the_gap_still_waits_when_no_stop_has_arrived(home):
    """The other half: an interruptible gap that never waits is no pacing at
    all, and pacing is what keeps the account off a throttle."""
    import time as real_time
    gap = jobs._interruptible_sleep(threading.Event())
    before = real_time.monotonic()
    gap(0.05)
    # A loose floor on purpose: Windows' timer granularity is ~15.6ms, and
    # what is being pinned is "it waited", not "it waited precisely".
    assert real_time.monotonic() - before >= 0.03


def test_the_run_is_given_the_interruptible_gap_and_the_stop_signal(monkeypatch,
                                                                   home):
    """The wiring itself. submit.run binds the real time.sleep as a DEFAULT
    argument, so a worker that passes no sleep gets a gap no stop can
    shorten — and the tests above would never notice."""
    from contextlib import contextmanager

    captured: dict = {}
    inside = threading.Event()
    release = threading.Event()

    class _Sender:
        def submit(self, property: str, url: str, authuser: str) -> str:
            raise AssertionError("no URL should have been sent")

    @contextmanager
    def fake_session(chosen, cfg):
        yield _Sender()

    def fake_run(conn, sender, urls, **kwargs):
        captured.update(kwargs)
        inside.set()
        assert release.wait(_JOIN_TIMEOUT)
        return submit.RunResult([], True, "stopped_by_user")

    monkeypatch.setattr(jobs.target, "resolve", lambda *a, **k: object())
    monkeypatch.setattr(jobs.bridge, "bridge_session", fake_session)
    monkeypatch.setattr(jobs.submit, "run", fake_run)

    job_id = jobs.start(["https://example.com/a"], CFG)
    assert inside.wait(_JOIN_TIMEOUT)

    assert captured["job_id"] == job_id
    assert captured["account"] == "default"        # never an address
    assert captured["should_stop"]() is False

    # Both the stop check AND the gap must be bound to THIS job's signal.
    assert jobs.stop(job_id) is True
    assert captured["should_stop"]() is True
    returned = threading.Event()

    def wait_out_a_full_gap() -> None:
        captured["sleep"](3600.0)
        returned.set()

    threading.Thread(target=wait_out_a_full_gap, daemon=True).start()
    assert returned.wait(_JOIN_TIMEOUT), "the gap was not the job's own signal"

    release.set()
    _await_terminal(job_id)


# --- shutdown ----------------------------------------------------------------

def _wedged_execute(entered: threading.Event, release: threading.Event
                    ) -> Callable[..., submit.RunResult]:
    """A run that does NOT answer a stop — a send already in flight.

    The case shutdown must survive without hanging: the worker is inside a
    browser round-trip, the stop signal lands where nothing is looking at
    it, and shutdown has to give up and say so.
    """
    def execute(conn: sqlite3.Connection, job_id: str, urls: list[str],
                cfg: dict, stop_event: threading.Event,
                on_progress: Callable[[submit.Attempt], None]
                ) -> submit.RunResult:
        entered.set()
        assert release.wait(_JOIN_TIMEOUT), "the wedged worker was never freed"
        return submit.RunResult([], False, None)
    return execute


def _shutdown_off_thread(timeout: float) -> bool:
    """jobs.shutdown() run where an unbounded join reddens instead of hanging.

    A shutdown that joined without a bound would sit there for as long as
    the worker does — which live is hours of paced sends. Here it would
    simply hang the suite, so the call is made on its own thread and the
    test fails if it has not returned.
    """
    outcome: list[bool] = []

    def call() -> None:
        outcome.append(jobs.shutdown(timeout))

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    thread.join(_JOIN_TIMEOUT)
    assert not thread.is_alive(), "shutdown did not respect its timeout"
    return outcome[0]


def test_shutdown_stops_a_running_job_and_waits_for_it_to_settle(monkeypatch,
                                                                 home):
    """The point of the whole thing: a client disconnect must not abandon a
    worker mid-run. The stop lands between URLs, so it costs no slot, and
    the run itself — not a reconcile sweep at the next startup — writes the
    terminal state."""
    entered = threading.Event()
    monkeypatch.setattr(jobs, "_execute", _blocking_execute(entered))
    job_id = jobs.start(["https://example.com/a"], CFG)
    assert entered.wait(_JOIN_TIMEOUT)

    assert _shutdown_off_thread(_JOIN_TIMEOUT) is True
    assert jobs.is_running(job_id) is False
    assert _read(job_id)["state"] == "stopped_user"

    # And the bridge is free — proved by taking it, not by reading a flag.
    with jobs.claim_bridge():
        pass


def test_shutdown_reports_false_when_the_worker_does_not_settle_in_time(
        monkeypatch, home):
    """The honest answer, bounded. A worker inside a send cannot be hurried,
    and waiting it out would hang the process the operator is trying to
    close; what must not happen is a lie about it having settled."""
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(jobs, "_execute", _wedged_execute(entered, release))
    job_id = jobs.start(["https://example.com/a"], CFG)
    assert entered.wait(_JOIN_TIMEOUT)

    assert _shutdown_off_thread(0.05) is False
    assert jobs.is_running(job_id) is True

    # The claim went back even so: a claim held by a worker nobody is
    # waiting for any more would refuse every submission until a restart,
    # and _SYNC has no liveness derivation to heal it.
    with jobs.claim_bridge():
        pass

    release.set()
    _await_terminal(job_id)


def test_shutdown_does_not_release_a_claim_that_is_no_longer_the_jobs(
        monkeypatch, home):
    """The release is CONDITIONAL, and it has to be.

    The losing race is real: the worker settles during the join, a
    gsc_request_indexing call takes the bridge in that instant, and
    shutdown then reaches its release loop. An unconditional clear there
    hands that live synchronous run a second driver for the one Chrome tab
    — two submissions of the same URL, two quota slots nobody gets back.

    The claim is planted rather than raced for, because the window is a few
    bytecodes wide and a test that waits for it to occur naturally is a
    test that mostly does not run. Everything else is real: a live worker,
    so the release loop is genuinely reached, and the refusal is proved by
    trying to take the bridge rather than by reading the flag.
    """
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(jobs, "_execute", _wedged_execute(entered, release))
    job_id = jobs.start(["https://example.com/a"], CFG)
    assert entered.wait(_JOIN_TIMEOUT)

    with jobs._lock:
        jobs._holder = jobs._SYNC

    assert _shutdown_off_thread(0.05) is False

    # Still refused, and refused as the SYNCHRONOUS run's claim: _SYNC has
    # no liveness derivation behind it, so a shutdown that cleared it would
    # not be healed by anything short of restarting the process.
    with pytest.raises(jobs.AlreadyRunning) as caught:
        with jobs.claim_bridge():
            pytest.fail("shutdown gave away a bridge it did not hold")
    assert "gsc_request_indexing" in str(caught.value)

    release.set()
    _await_terminal(job_id)


def test_shutdown_with_no_job_running_is_a_harmless_no_op(home):
    """The ordinary case — the server closes with nothing in flight."""
    assert _shutdown_off_thread(_JOIN_TIMEOUT) is True
    with jobs.claim_bridge():
        pass


def test_shutdown_after_a_job_has_already_finished_is_a_no_op(monkeypatch,
                                                              home):
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)

    assert _shutdown_off_thread(_JOIN_TIMEOUT) is True
    assert _read(job_id)["state"] == "completed"    # not rewritten
    with jobs.claim_bridge():
        pass


def test_shutdown_does_not_take_the_bridge_from_a_synchronous_run(home):
    """It releases the claims of the workers it joined, and nothing else.

    A synchronous run holds the bridge from the calling thread and gives it
    back in its own finally. Clearing it from under that thread would let a
    job start onto the browser tab it is still driving.
    """
    with jobs.claim_bridge():
        assert _shutdown_off_thread(_JOIN_TIMEOUT) is True
        with pytest.raises(jobs.AlreadyRunning):
            jobs.start(["https://example.com/a"], CFG)


def test_shutdown_writes_nothing_to_stdout(monkeypatch, home, capsys):
    monkeypatch.setattr(jobs, "_execute", _blocking_execute())
    jobs.start(["https://example.com/a"], CFG)
    assert _shutdown_off_thread(_JOIN_TIMEOUT) is True
    assert capsys.readouterr().out == ""


# --- the throttled and crashing ends ----------------------------------------

@pytest.mark.parametrize("reason", ["rate_limited", "quota_exceeded",
                                    "no_quota"])
def test_a_throttled_run_lands_in_stopped_throttled(monkeypatch, home, reason):
    """Three different ways to run out of road, one state: the operator's
    next move is the same for all three — wait for the rolling window."""
    monkeypatch.setattr(jobs, "_execute",
                        _fake_execute(["skipped"], stop_reason=reason))
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)
    assert _read(job_id)["state"] == "stopped_throttled"


@pytest.mark.parametrize("reason", ["rate_limited", "quota_exceeded",
                                    "no_quota"])
def test_the_stop_reason_survives_onto_the_job_row(monkeypatch, home, reason):
    """All three collapse onto stopped_throttled, so the state cannot say
    which happened. "no_quota" stops at the gate and records no attempt at
    all, leaving the row the only place the cause can be read."""
    monkeypatch.setattr(jobs, "_execute",
                        _fake_execute([], stop_reason=reason))
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)
    assert _read(job_id)["stop_reason"] == reason


def test_a_run_that_ends_on_its_own_records_no_stop_reason(monkeypatch, home):
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)
    assert _read(job_id)["stop_reason"] is None


def test_a_crashing_worker_fails_the_job_rather_than_leaving_it_running(
        monkeypatch, home):
    def explode(*args, **kwargs):
        raise RuntimeError(f"{SECRET_PATH} for {SECRET_EMAIL}")

    monkeypatch.setattr(jobs, "_execute", explode)
    with capturing(jobs.log) as records:
        job_id = jobs.start(["https://example.com/a"], CFG)
        _await_terminal(job_id)

    job = _read(job_id)
    assert job["state"] == "failed"
    assert job["error"] == "RuntimeError"
    # The stored error is user-visible AND on disk: type name only.
    assert "secret-operator" not in job["error"]
    assert SECRET_EMAIL not in job["error"]

    text = logged_text(records)
    assert "secret-operator" not in text and SECRET_EMAIL not in text
    assert "RuntimeError" in text          # live-capture canary


def test_a_crashing_worker_frees_the_registry_slot(monkeypatch, home):
    """A crash that leaks the slot locks the operator out of every future
    job until the process restarts."""
    monkeypatch.setattr(jobs, "_execute", _boom_execute())
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)
    assert jobs.is_running(job_id) is False

    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    second = jobs.start(["https://example.com/b"], CFG)
    _await_terminal(second)
    assert _read(second)["state"] == "completed"


class _ConnectFailure(OSError):
    """The shape of a full disk or an unwritable GSC_MCP_HOME."""


def _connect_failing_in_the_worker(monkeypatch) -> None:
    """store.connect() raises once, and only for the worker's own attempt.

    Keyed on the calling thread rather than on a call count, so it fails
    exactly the acquisition under test: start()'s row insert still lands,
    and the store stays reachable for the failure to be recorded in — which
    is what makes "the job reaches failed" an assertion about the
    implementation rather than about the fake.
    """
    real = store.connect
    tripped = threading.Event()

    def connect(*args, **kwargs):
        if threading.current_thread().name.startswith("gsc-job-") \
                and not tripped.is_set():
            tripped.set()
            raise _ConnectFailure(f"{SECRET_PATH} is full ({SECRET_EMAIL})")
        return real(*args, **kwargs)

    monkeypatch.setattr(jobs.store, "connect", connect)


def test_a_worker_that_cannot_open_its_connection_fails_the_job(monkeypatch,
                                                                home):
    """The row must not be left pending with no error, forever.

    store.connect() is the first thing the worker does. Outside the try it
    takes the whole worker down with it: nothing records "failed", nothing
    frees the registry, and reconcile_jobs() sweeps only "running" — so a
    full disk means gsc_start_indexing_job answers ok with a job id that
    reports pending and no error for the life of the installation.
    """
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    _connect_failing_in_the_worker(monkeypatch)

    with capturing(jobs.log) as records:
        job_id = jobs.start(["https://example.com/a"], CFG)
        _await_terminal(job_id)

    job = _read(job_id)
    assert job["state"] == "failed"
    # Type name only: the message carries the path that could not be written.
    assert job["error"] == "_ConnectFailure"
    assert "secret-operator" not in job["error"]
    assert SECRET_EMAIL not in job["error"]

    text = logged_text(records)
    assert "secret-operator" not in text and SECRET_EMAIL not in text
    assert "_ConnectFailure" in text                # live-capture canary


def test_a_worker_that_cannot_open_its_connection_frees_the_registry(
        monkeypatch, home):
    """The other half of the same defect: a lost worker holds the one-job
    slot, so the operator can never start another job either."""
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    _connect_failing_in_the_worker(monkeypatch)
    first = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(first)

    assert jobs.is_running(first) is False
    assert jobs._threads == {} and jobs._stop_events == {}

    second = jobs.start(["https://example.com/b"], CFG)
    _await_terminal(second)
    assert _read(second)["state"] == "completed"


def test_a_store_that_never_opens_still_lets_the_worker_die_quietly(monkeypatch,
                                                                    home):
    """The unrecoverable case: nothing can be written, including the
    failure. What must still hold is that the thread ends and the registry
    is reclaimed, rather than the process losing its only job slot."""
    monkeypatch.setattr(jobs, "_execute", _fake_execute(["submitted"]))
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)          # a clean run first, so a row exists

    def always_fails(*args, **kwargs):
        raise _ConnectFailure(SECRET_PATH)

    monkeypatch.setattr(jobs.store, "connect", always_fails)
    with capturing(jobs.log) as records:
        # start() opens its own connection through store.session(); the
        # failure there is the caller's to see, and it must leave nothing
        # behind either.
        with pytest.raises(_ConnectFailure):
            jobs.start(["https://example.com/b"], CFG)

    assert jobs._threads == {} and jobs._stop_events == {}
    assert SECRET_PATH not in logged_text(records)


class _SpawnFailure(RuntimeError):
    """A thread that cannot be started — an OS resource limit, in practice."""


def test_a_thread_that_will_not_start_leaves_nothing_behind(monkeypatch, home):
    """The registry entry is written before start(). If start() raises, that
    entry is never reclaimed and the row wedges pending exactly as above —
    with the added insult that the one-job guard sees a dead thread and the
    row never settles."""
    class _DeadThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise _SpawnFailure("no threads left")

        def is_alive(self) -> bool:
            return False

    monkeypatch.setattr(jobs.threading, "Thread", _DeadThread)
    with pytest.raises(_SpawnFailure):
        jobs.start(["https://example.com/a"], CFG)

    assert jobs._threads == {} and jobs._stop_events == {}
    with store.session() as conn:
        wedged = store.list_jobs(conn)
    assert [job["state"] for job in wedged] == ["failed"]
    assert wedged[0]["error"] == "_SpawnFailure"


def test_a_missing_browser_fails_the_job_by_a_named_type(monkeypatch, home):
    """The stored error IS the message here, so the type name has to say
    something. "RuntimeError" would tell the operator nothing."""
    monkeypatch.setattr(jobs.target, "resolve", lambda *a, **k: None)
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)

    job = _read(job_id)
    assert job["state"] == "failed"
    assert job["error"] == "NoBrowser"


# --- connections -------------------------------------------------------------

def test_the_worker_does_not_share_the_callers_connection(monkeypatch, home):
    """store.tx()'s re-entrancy is CONNECTION-scoped. A worker running on
    the caller's connection would nest transactions across two threads and
    the inner RELEASE would commit nothing durably."""
    seen: list[int] = []

    def execute(conn, job_id, urls, cfg, stop_event, on_progress):
        seen.append(id(conn))
        return submit.RunResult([], False, None)

    monkeypatch.setattr(jobs, "_execute", execute)
    with store.session() as caller:
        job_id = jobs.start(["https://example.com/a"], CFG)
        _await_terminal(job_id)
        assert seen and seen[0] != id(caller)


def test_the_job_is_readable_from_another_connection_while_it_runs(monkeypatch,
                                                                   home):
    """The worker's writes must be committed, not held in an open
    transaction — job_status reads on a different connection entirely."""
    seen = threading.Event()
    release = threading.Event()

    def execute(conn, job_id, urls, cfg, stop_event, on_progress):
        on_progress(_attempt(urls[0], "submitted"))
        seen.set()
        assert release.wait(_JOIN_TIMEOUT)
        return submit.RunResult([], False, None)

    monkeypatch.setattr(jobs, "_execute", execute)
    with store.session() as reader:
        job_id = jobs.start(["https://example.com/a"], CFG)
        assert seen.wait(_JOIN_TIMEOUT)
        assert store.get_job(reader, job_id)["progress"]["done"] == 1
        release.set()
    _await_terminal(job_id)


def test_a_job_writes_nothing_to_stdout(monkeypatch, home, capsys):
    """stdout is the MCP JSON-RPC transport, and a worker thread writing to
    it corrupts frames for a client that never called it."""
    monkeypatch.setattr(jobs, "_execute", _boom_execute())
    job_id = jobs.start(["https://example.com/a"], CFG)
    _await_terminal(job_id)
    assert capsys.readouterr().out == ""
