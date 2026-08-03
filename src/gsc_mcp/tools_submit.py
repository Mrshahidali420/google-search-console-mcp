"""Tool bodies for submission. Never raise: every path returns a dict.

The tool functions live here rather than in server.py because server.py is
already at its accepted size ceiling, and because the tools that follow
(the job trio) share the helpers below.

PRIVACY. Everything returned from here leaves the process: an MCP result is
consumed by a model, rendered into a transcript, and retained by whatever
client is driving the server. So nothing identifying is ever put into one.
In particular the resolved `Target` is NEVER formatted into a message — it
carries the profile's filesystem path (which contains the operator's
account name) and its display name (which Chrome routinely sets to the
signed-in address). `target.describe()` exists for the cases that need to
name a browser; this module needs none of them, so it names no browser at
all. Failures are reported by exception TYPE NAME, with the single stated
exception in _request_indexing.
"""
from __future__ import annotations

import sqlite3
import time

from gsc_core import bridge, config, runlog, store, submit

from . import deps, envelopes, jobs, target

log = runlog.get(__name__)

# The synchronous tool blocks an MCP client for one delay_range gap per URL
# after the first. Five URLs at the proven [130,180]s pacing is already up
# to twelve minutes of a client sitting on a stalled call; more than that
# belongs in a job. This ceiling is not configurable upward — config's
# sync_submit_cap may lower it, never raise it.
HARD_SYNC_CAP = 5

_FIX_NO_URLS = "pass a list of absolute URLs, at least one and at most five"
_FIX_TOO_MANY = ("use the background job tools for anything larger — they "
                 "run without blocking and report progress as they go")
_FIX_NO_BROWSER = ("run gsc_detect_browsers to see which Chromium browsers "
                   "are installed, then gsc_setup to pair one")
_FIX_NO_PROPERTIES = ("run gsc_list_sites first — a URL can only be "
                      "submitted through a property that covers it")
_FIX_NO_EXTENSION = ("open the browser and load the GSC MCP Bridge extension; "
                     "gsc_doctor reports where it should be loaded from")
_FIX_BAD_CAP = (f"set sync_submit_cap in the config file to a whole number "
                f"between 1 and {HARD_SYNC_CAP}, or remove it to use the "
                f"default")
_FIX_NO_JOB_URLS = "pass a list of absolute URLs, at least one"
_FIX_JOB_RUNNING = ("stop it with gsc_stop_job, or wait for it to finish — "
                    "the bridge drives one browser tab and cannot run two")
_FIX_NO_JOBS = "start one with gsc_start_indexing_job"
_FIX_UNKNOWN_JOB = ("call gsc_job_status with no argument for the most "
                    "recent job")

_NOTE_JOB_QUEUED = "poll gsc_job_status for progress"
_NOTE_JOB_STOPPING = "stopping after the URL in flight"
_NOTE_JOB_FINISHED = "that job had already finished"

# Two loop-level results have no disposition and never reach the wire, and
# collapsing them into a single "skipped" count would hide that they are
# different problems with different fixes: one says we could not route the
# URL, the other says the property is out of budget for today.
_NOTE_NO_PROPERTY = (
    "some URLs matched no known Search Console property — check the domain, "
    "and run gsc_list_sites to refresh the property list"
)
_NOTE_NO_QUOTA = (
    "some URLs found no spendable slot — a property's daily submission quota "
    "(about eleven per property, on a rolling day) is used up; the rest can "
    "go tomorrow"
)

# Everything that reached an end state without costing a submission. Not a
# judgement about success: a "skipped" URL may be fine (already indexed) or
# may need the operator to act (no_property, no_quota), which is what the
# notes above are for.
_SKIPPED = frozenset({"already_indexed", "skipped", "no_property", "no_quota"})


def _account() -> str:
    """The key the submissions ledger is written under.

    Deliberately NOT the user's email address: that column lands on disk,
    and no email may be written to disk anywhere in this project. Quota is
    per property in any case, so this key carries no accounting weight
    today — it exists so a future multi-account story has somewhere to go.
    """
    return "default"


def _properties(conn: sqlite3.Connection) -> list[str]:
    """Every property the local store knows about, for routing."""
    return [site["property"] for site in store.get_sites(conn)]


def _cap(cfg: dict) -> int | None:
    """How many URLs this call may carry, or None if the config is unusable.

    config.load() layers a user file over the defaults WITHOUT running
    validate(), so anything JSON can hold arrives here. Two guards:

    * the ceiling can only come DOWN — min() against HARD_SYNC_CAP, because
      a blocking tool that waits half an hour is a hung client;
    * and it cannot come down to nothing — max(1, ...), because a cap of 0
      or -1 would refuse every call with "takes at most 0", which reads as
      a broken tool rather than as a typo in a config file.

    None is reserved for a value that is not a number at all (null, a
    string, a list). That earns a named refusal naming the setting, rather
    than a TypeError surfacing as a generic "unexpected".
    """
    raw = cfg.get("sync_submit_cap", HARD_SYNC_CAP)
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(configured, HARD_SYNC_CAP))


def _notes_for(outcomes: set[str | None]) -> list[str]:
    """The guidance a set of outcomes earns. Additive: never a replacement
    for the counts, because a note explains a number rather than standing
    in for one."""
    notes = []
    if "no_property" in outcomes:
        notes.append(_NOTE_NO_PROPERTY)
    if "no_quota" in outcomes:
        notes.append(_NOTE_NO_QUOTA)
    return notes


def _notes(result: submit.RunResult) -> list[str]:
    return _notes_for({attempt.outcome for attempt in result.attempts})


def _tally(result: submit.RunResult) -> dict:
    """The run, counted. Carries URLs and properties — and nothing else.

    No browser, no profile, no account: the caller asked about URLs and
    every other field would only add something a transcript should not
    keep.
    """
    submitted = sum(1 for attempt in result.attempts
                    if attempt.outcome == "submitted")
    skipped = sum(1 for attempt in result.attempts
                  if attempt.outcome in _SKIPPED)
    return {
        "ok": True,
        "submitted": submitted,
        "skipped": skipped,
        "failed": len(result.attempts) - submitted - skipped,
        "stopped_early": result.stopped_early,
        "stop_reason": result.stop_reason,
        "notes": _notes(result),
        "results": [{"url": attempt.url, "property": attempt.property,
                     "outcome": attempt.outcome,
                     "spent_slot": attempt.spent_slot}
                    for attempt in result.attempts],
    }


def request_indexing(urls: list[str]) -> dict:
    """Submit up to five URLs, blocking until every one has an outcome."""
    try:
        return _request_indexing(urls)
    except jobs.AlreadyRunning as exc:
        # Repeated for the same reason start_indexing_job repeats it: the
        # message is authored in jobs.py, it names a job id at most, and
        # without it the caller cannot tell "something else has the browser"
        # from "the extension is broken".
        return envelopes.refuse("job_already_running", str(exc), _FIX_JOB_RUNNING)
    except Exception as exc:  # noqa: BLE001 — a tool never raises
        # TYPE NAME only, in the log and in the result: an unauthored
        # message can carry a filesystem path holding the operator's
        # account name, or the bridge token.
        log.warning("gsc_request_indexing: unexpected %s", type(exc).__name__)
        return envelopes.refuse("unexpected", type(exc).__name__,
                                envelopes.FIX_UNEXPECTED)


def _request_indexing(urls: list[str]) -> dict:
    cfg = config.load()
    cap = _cap(cfg)
    if cap is None:
        return envelopes.refuse("bad_config",
                       "sync_submit_cap is not a whole number", _FIX_BAD_CAP)
    if not urls:
        return envelopes.refuse("no_urls", "no URLs were given", _FIX_NO_URLS)
    if len(urls) > cap:
        # Refused whole, before a browser is resolved or a row is opened.
        # Truncating instead would spend quota on a list the caller never
        # agreed to cut.
        return envelopes.refuse("too_many_urls",
                       f"{len(urls)} URLs given; this tool takes at most {cap}",
                       _FIX_TOO_MANY)

    # ONE RUN AT A TIME, and the same claim a background job takes. The
    # bridge binds one fixed localhost port and drives one browser tab, so a
    # sync call landing on top of a running job cannot work — and fails
    # unhelpfully if it is allowed to try: ws_serve's bind error is raised
    # inside a daemon thread nothing can catch, so `ready` never sets, this
    # call waits out its whole connect timeout, and the caller is told to
    # load an extension that is already loaded. Claimed HERE, before the
    # browser is resolved and long before a slot is reserved, so a refusal
    # costs nothing. AlreadyRunning is left to request_indexing to convert.
    with jobs.claim_bridge():
        chosen = target.resolve()
        if chosen is None:
            return envelopes.refuse("no_browser",
                           "no Chromium browser profile was found",
                           _FIX_NO_BROWSER)

        with deps.connection() as conn:
            properties = _properties(conn)
            if not properties:
                return envelopes.refuse("no_properties",
                               "no Search Console properties are known yet",
                               _FIX_NO_PROPERTIES)
            try:
                with bridge.bridge_session(chosen, cfg) as session:
                    # sleep is passed explicitly, resolved off this module's
                    # own `time` at call time: submit.run binds the real
                    # time.sleep as a default argument at import, so a test
                    # that replaced it afterwards would still wait out a
                    # real 130-180s gap.
                    result = submit.run(conn, session, urls,
                                        properties=properties,
                                        account=_account(), job_id=None,
                                        cfg=cfg, sleep=time.sleep)
            except bridge.ExtensionNotConnected as exc:
                # The ONLY place in this module that repeats an exception's
                # message. Scoped to one exception type this project raises,
                # whose text names a browser brand and nothing else — not to
                # RuntimeError, which would also catch anything the run body
                # raises and hand its unauthored message (a filesystem path
                # holding the operator's account name, say) to an MCP client.
                # Do not widen it. Everything else, including the plain
                # RuntimeError load_or_create_token raises for an unwritable
                # config directory, falls through to request_indexing's
                # type-name-only handler.
                return envelopes.refuse("extension_not_connected", str(exc),
                               _FIX_NO_EXTENSION)
    return _tally(result)


# --- the job trio ------------------------------------------------------------
#
# The asynchronous half of the same machinery. request_indexing above is
# capped at five URLs because it blocks for one 130-180 second gap per URL;
# these three exist so a run of any size can proceed without a client
# sitting on a stalled call for hours.


def start_indexing_job(urls: list[str]) -> dict:
    """Queue a background submission run over any number of URLs.

    Returns as soon as the job row exists. No cap: the whole point of a job
    is that nobody is waiting on it.
    """
    try:
        if not urls:
            return envelopes.refuse("no_urls", "no URLs were given",
                                    _FIX_NO_JOB_URLS)

        # Checked here rather than left to the run: with no properties every
        # URL would report no_property, so the job would open a browser, a
        # bridge and a socket to produce a list of misses. Refusing costs
        # nothing and says the same thing sooner.
        with deps.connection() as conn:
            if not _properties(conn):
                return envelopes.refuse("no_properties",
                               "no Search Console properties are known yet",
                               _FIX_NO_PROPERTIES)

        job_id = jobs.start(urls, config.load())
        return {"ok": True, "job_id": job_id, "total": len(urls),
                "note": _NOTE_JOB_QUEUED}
    except jobs.AlreadyRunning as exc:
        # The one message repeated here, like _request_indexing's single
        # exemption: it is authored in jobs.start(), it names the running
        # job id and nothing else, and without that id "stop it" is not
        # something the caller can act on.
        return envelopes.refuse("job_already_running", str(exc), _FIX_JOB_RUNNING)
    except Exception as exc:  # noqa: BLE001 — a tool never raises
        log.warning("gsc_start_indexing_job: unexpected %s",
                    type(exc).__name__)
        return envelopes.refuse("unexpected", type(exc).__name__,
                                envelopes.FIX_UNEXPECTED)


def job_status(job_id: str | None = None) -> dict:
    """One job's state and progress; the most recent when no id is given.

    ok is about the CALL, not about the job: a failed job is reported as a
    successful status read whose state is "failed".
    """
    try:
        with deps.connection() as conn:
            if job_id is None:
                known = store.list_jobs(conn)
                if not known:
                    return envelopes.refuse("no_jobs",
                                   "no submission job has run yet",
                                   _FIX_NO_JOBS)
                job = known[-1]        # list_jobs orders by started_at
            else:
                job = store.get_job(conn, job_id)
                if job is None:
                    return envelopes.refuse("unknown_job", "no job with that id",
                                   _FIX_UNKNOWN_JOB)

        progress = job.get("progress") or {}
        results = progress.get("results", [])
        return {"ok": True, "job_id": job["id"], "state": job["state"],
                "done": progress.get("done", 0),
                "total": progress.get("total", 0),
                # Kept apart rather than counted together: no_property and
                # no_quota are different problems with different fixes.
                "notes": _notes_for({row.get("outcome") for row in results}),
                "results": results,
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "error": job.get("error"),
                # The row alone cannot tell a live job from one whose worker
                # died; only the registry knows, until the next startup
                # reconcile catches up.
                "live": jobs.is_running(job["id"])}
    except Exception as exc:  # noqa: BLE001 — a tool never raises
        log.warning("gsc_job_status: unexpected %s", type(exc).__name__)
        return envelopes.refuse("unexpected", type(exc).__name__,
                                envelopes.FIX_UNEXPECTED)


def stop_job(job_id: str) -> dict:
    """Ask a running job to stop after the URL it is on.

    Not mid-URL: a submission already sent has spent its slot, and its row
    must settle with the real outcome rather than be abandoned blind. The
    signal still lands within the pacing gap rather than after it, so no
    further slot is reserved.
    """
    try:
        if jobs.stop(job_id):
            return {"ok": True, "job_id": job_id,
                    "note": _NOTE_JOB_STOPPING}

        with deps.connection() as conn:
            job = store.get_job(conn, job_id)
        if job is None:
            return envelopes.refuse("unknown_job", "no job with that id",
                           _FIX_UNKNOWN_JOB)
        # Already finished is not a failure: the caller wanted it stopped
        # and it is stopped. Refusing here would read as "still running".
        return {"ok": True, "job_id": job_id, "state": job["state"],
                "note": _NOTE_JOB_FINISHED}
    except Exception as exc:  # noqa: BLE001 — a tool never raises
        log.warning("gsc_stop_job: unexpected %s", type(exc).__name__)
        return envelopes.refuse("unexpected", type(exc).__name__,
                                envelopes.FIX_UNEXPECTED)
