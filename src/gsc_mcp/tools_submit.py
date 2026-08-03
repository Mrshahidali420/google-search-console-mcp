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

from . import deps, target

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
_FIX_UNEXPECTED = ("check the log file for the failure type, then try again; "
                   "gsc_doctor reports on the setup as a whole")

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


def _refuse(error: str, detail: str, fix: str) -> dict:
    return {"ok": False, "error": error, "detail": detail, "fix": fix}


def _notes(result: submit.RunResult) -> list[str]:
    outcomes = {attempt.outcome for attempt in result.attempts}
    notes = []
    if "no_property" in outcomes:
        notes.append(_NOTE_NO_PROPERTY)
    if "no_quota" in outcomes:
        notes.append(_NOTE_NO_QUOTA)
    return notes


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
    except Exception as exc:  # noqa: BLE001 — a tool never raises
        # TYPE NAME only, in the log and in the result: an unauthored
        # message can carry a filesystem path holding the operator's
        # account name, or the bridge token.
        log.warning("gsc_request_indexing: unexpected %s", type(exc).__name__)
        return _refuse("unexpected", type(exc).__name__, _FIX_UNEXPECTED)


def _request_indexing(urls: list[str]) -> dict:
    cfg = config.load()
    cap = min(int(cfg.get("sync_submit_cap", HARD_SYNC_CAP)), HARD_SYNC_CAP)
    if not urls:
        return _refuse("no_urls", "no URLs were given", _FIX_NO_URLS)
    if len(urls) > cap:
        # Refused whole, before a browser is resolved or a row is opened.
        # Truncating instead would spend quota on a list the caller never
        # agreed to cut.
        return _refuse("too_many_urls",
                       f"{len(urls)} URLs given; this tool takes at most {cap}",
                       _FIX_TOO_MANY)

    chosen = target.resolve()
    if chosen is None:
        return _refuse("no_browser", "no Chromium browser profile was found",
                       _FIX_NO_BROWSER)

    with deps.connection() as conn:
        properties = _properties(conn)
        if not properties:
            return _refuse("no_properties",
                           "no Search Console properties are known yet",
                           _FIX_NO_PROPERTIES)
        try:
            with bridge.bridge_session(chosen, cfg) as session:
                # sleep is passed explicitly, resolved off this module's own
                # `time` at call time: submit.run binds the real time.sleep
                # as a default argument at import, so a test that replaced
                # it afterwards would still wait out a real 130-180s gap.
                result = submit.run(conn, session, urls,
                                    properties=properties,
                                    account=_account(), job_id=None, cfg=cfg,
                                    sleep=time.sleep)
        except RuntimeError as exc:
            # bridge_session raises this when the extension never connects,
            # and load_or_create_token when the config directory is not
            # writable. Both messages are written by this project and carry
            # no path and no token, which is why str(exc) is safe HERE and
            # nowhere else — do not extend it to exceptions we did not
            # author. Anything else falls through to request_indexing's
            # type-name-only handler.
            return _refuse("extension_not_connected", str(exc),
                           _FIX_NO_EXTENSION)
    return _tally(result)
