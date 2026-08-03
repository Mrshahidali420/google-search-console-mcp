"""Background submission jobs: one at a time, on its own thread and its own
database connection.

ONE AT A TIME is not a simplification. The bridge binds a fixed localhost
port and drives a single browser tab in the user's real profile; two
concurrent jobs would fight over both, and their quota reservations would
serialise into each other's BEGIN IMMEDIATE for hours. The guard is taken
under a lock that also covers the row insert, so two callers racing into
start() cannot both come out of it with a job.

PRIVACY. A job's `error` column is written to disk AND handed back by
gsc_job_status, so it carries the exception TYPE NAME and nothing else: an
unauthored message routinely contains a filesystem path with the operator's
account name in it. That is also why the two failures this module raises
itself have names worth reading — the type name is the whole message.
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable

from gsc_core import bridge, runlog, store, submit

from . import target

log = runlog.get(__name__)

#: Job states with a live worker behind them, or about to have one.
ACTIVE_STATES = frozenset({"pending", "running"})

# Three different ways to run out of road, one state. The operator's next
# move is the same for all three — wait for the rolling window to move —
# and quota_exceeded/no_quota are not "failures" in any sense the operator
# can act on differently.
_THROTTLED = frozenset({"rate_limited", "quota_exceeded", "no_quota"})


class AlreadyRunning(RuntimeError):
    """Another submission job is live. Its id is in the message."""


class NoBrowser(RuntimeError):
    """No Chromium browser profile could be resolved to drive.

    Named rather than a bare RuntimeError because the job's stored error is
    the exception's TYPE NAME — "NoBrowser" tells the operator where to
    look, "RuntimeError" tells them nothing.
    """


_lock = threading.Lock()
_stop_events: dict[str, threading.Event] = {}
_threads: dict[str, threading.Thread] = {}


def is_running(job_id: str) -> bool:
    """Whether a worker for this job is alive IN THIS PROCESS.

    Deliberately not a read of the job row: a row still marked running
    after a crash is stale until the next startup reconcile, and this is
    the only thing that knows the difference.
    """
    with _lock:
        thread = _threads.get(job_id)
    return thread is not None and thread.is_alive()


def join(job_id: str, timeout: float | None = None) -> bool:
    """Wait for a job's worker to finish. True if nothing is left running.

    True for an unknown or already-finished job: the worker clears itself
    from the registry only AFTER writing its terminal state, so a job that
    is no longer registered has already settled.
    """
    with _lock:
        thread = _threads.get(job_id)
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def start(urls: list[str], cfg: dict) -> str:
    """Create the job row and its worker thread. Returns the new job id.

    The liveness check, the row insert and the thread start all happen
    under one lock. Splitting them would be a check-then-act race: two
    callers would each see no live job, each insert a row, and each start a
    worker onto the one browser tab.
    """
    job_id = uuid.uuid4().hex
    with _lock:
        live = [existing for existing, thread in _threads.items()
                if thread.is_alive()]
        if live:
            raise AlreadyRunning(
                f"submission job {live[0]} is still running; stop it with "
                "gsc_stop_job or wait for it to finish")

        # Inside the lock on purpose, so a refused start leaves no row: an
        # orphan row would surface in gsc_job_status as a job that never ran.
        with store.session() as conn:
            store.create_job(conn, job_id, {"urls": urls, "total": len(urls)})

        stop_event = threading.Event()
        thread = threading.Thread(target=_worker,
                                  args=(job_id, urls, cfg, stop_event),
                                  name=f"gsc-job-{job_id[:8]}", daemon=True)
        _stop_events[job_id] = stop_event
        _threads[job_id] = thread
        try:
            thread.start()
        except Exception as exc:  # noqa: BLE001 — reclaim, then re-raise
            # The registry entry is written before start() so that nothing
            # can slip between the two. If start() then fails (an OS thread
            # limit), that entry is a slot nothing will ever free and the
            # row is a job nothing will ever run: undo both. The caller
            # still sees the exception — its tool reports it by type name.
            _stop_events.pop(job_id, None)
            _threads.pop(job_id, None)
            _record_failure(job_id, None, type(exc).__name__)
            raise
    return job_id


def stop(job_id: str) -> bool:
    """Signal a live job to stop between URLs. False if it is not live.

    Between URLs, not mid-URL: a submission already sent has spent its slot
    and its row must settle with the real outcome. Killing it there would
    leave an open row for reconcile() to charge blind.

    "Between URLs" still means promptly, though. The gap between two sends
    is 130-180 seconds and the worker spends nearly all its life inside
    one, so the gap itself is this event's wait (see _interruptible_sleep):
    once the signal is set the worker leaves the gap immediately, and
    submit.run re-checks should_stop after the gap and BEFORE reserve(), so
    the stop costs no quota slot.

    Setting the signal is not instant in one case: start() holds _lock
    across a BEGIN IMMEDIATE row insert, so a stop racing a start can wait
    on that lock for as long as the store's busy timeout (30s) under write
    contention. Nothing is lost when it does — the running job keeps
    pacing, and the stop lands in the same gap it would have — but this is
    not a lock-free call and should not be sold as one.
    """
    with _lock:
        event = _stop_events.get(job_id)
        thread = _threads.get(job_id)
    if event is None or thread is None or not thread.is_alive():
        return False
    event.set()
    return True


def _interruptible_sleep(stop_event: threading.Event
                         ) -> Callable[[float], None]:
    """The pacing gap between two sends, cut short by a stop.

    time.sleep would be wrong twice over: a stop arriving one second into a
    three-minute gap would go unseen for the rest of it, and submit.run
    binds the real time.sleep as a DEFAULT argument, so a worker that
    passes no sleep at all gets a gap nothing can shorten.
    """
    def sleep(seconds: float) -> None:
        stop_event.wait(seconds)
    return sleep


def _record_failure(job_id: str, conn: sqlite3.Connection | None,
                    error: str) -> None:
    """Settle a job at "failed", on whatever connection there is.

    `conn` is None when the worker never got one — the very failure this
    most needs to record. A fresh short-lived session is opened for that
    case, because the alternative is a row that stays pending with no
    error: reconcile_jobs() sweeps only "running", so nothing downstream
    would ever correct it and the operator would be told, permanently, that
    a job that never started is about to.

    `error` is a type name. Never a message: this lands on disk and is
    returned by gsc_job_status, and the message of a store failure is a
    filesystem path naming the operator.
    """
    try:
        if conn is not None:
            store.update_job(conn, job_id, state="failed", error=error)
            return
        with store.session() as fresh:
            store.update_job(fresh, job_id, state="failed", error=error)
    except Exception as exc:  # noqa: BLE001 — nothing left to try
        # An unreachable store cannot be told its job failed. Say so in the
        # log and let the next startup reconcile do what it can.
        log.warning("could not record the failure of job %s (%s)",
                    job_id, type(exc).__name__)


def _worker(job_id: str, urls: list[str], cfg: dict,
            stop_event: threading.Event) -> None:
    conn: sqlite3.Connection | None = None
    progress: dict = {"done": 0, "total": len(urls), "results": []}
    try:
        # Its OWN connection: store.tx()'s re-entrancy is connection-scoped,
        # so sharing the caller's would let two threads nest transactions
        # silently and the inner RELEASE would commit nothing durably.
        #
        # INSIDE the try, because opening it is itself a failure path — an
        # unwritable GSC_MCP_HOME, a full disk — and an exception raised
        # above this line would escape the thread entirely: no "failed"
        # recorded, no registry slot freed, and a row left pending forever.
        conn = store.connect()
        store.update_job(conn, job_id, state="running", progress=progress)

        def record(attempt: submit.Attempt) -> None:
            """Commit progress after every URL, not once at the end.

            gsc_job_status reads on a different connection entirely, and a
            run that only reported at the end would look wedged for hours.
            """
            progress["done"] += 1
            progress["results"].append({"url": attempt.url,
                                        "property": attempt.property,
                                        "outcome": attempt.outcome,
                                        "spent_slot": attempt.spent_slot})
            store.update_job(conn, job_id, progress=progress)

        result = _execute(conn, job_id, urls, cfg, stop_event, record)
        store.update_job(conn, job_id, state=_final_state(result, stop_event))
    except Exception as exc:  # noqa: BLE001 — a dead worker must not leave a
        # job marked running forever: the row would stay live until the next
        # startup reconcile, and "one job at a time" would mean "no job ever
        # again". The stored error is user-visible AND on disk, so it carries
        # the exception TYPE only.
        log.warning("job %s failed (%s)", job_id, type(exc).__name__)
        _record_failure(job_id, conn, type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()
        # Last, and after the terminal state is written: join() treats an
        # unregistered job as settled, so clearing earlier would let a
        # caller read the row before its final state landed.
        with _lock:
            _stop_events.pop(job_id, None)
            _threads.pop(job_id, None)


def _final_state(result: submit.RunResult,
                 stop_event: threading.Event) -> str:
    """Which terminal state one RunResult earns.

    Throttling wins over a user stop when both are true: it is the fact
    that changes what the operator can do next.
    """
    if result.stop_reason in _THROTTLED:
        return "stopped_throttled"
    if stop_event.is_set() or result.stop_reason == "stopped_by_user":
        return "stopped_user"
    return "completed"


def _execute(conn: sqlite3.Connection, job_id: str, urls: list[str],
             cfg: dict, stop_event: threading.Event,
             on_progress: Callable[[submit.Attempt], None]
             ) -> submit.RunResult:
    """The submission run itself: a browser, a bridge and the run loop.

    Its own function so tests can replace it without a browser, a socket or
    a real extension — and it takes on_progress rather than owning it, so
    the progress-recording code stays in _worker where the tests exercise
    the real one.
    """
    from .tools_submit import _account, _properties   # noqa: PLC0415 — cycle

    chosen = target.resolve()
    if chosen is None:
        # The Target is never formatted into a message anywhere in this
        # module: it carries the profile path (the OS account name) and a
        # display name Chrome commonly sets to the signed-in address.
        raise NoBrowser("no Chromium browser profile was found")

    properties = _properties(conn)
    with bridge.bridge_session(chosen, cfg) as session:
        return submit.run(conn, session, urls, properties=properties,
                          account=_account(), job_id=job_id, cfg=cfg,
                          sleep=_interruptible_sleep(stop_event),
                          should_stop=stop_event.is_set,
                          on_progress=on_progress)
