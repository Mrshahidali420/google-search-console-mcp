"""Submission policy: what an outcome means for quota, and how a slot is
reserved without a check-then-act race.

The bridge is transport and knows nothing about any of this. Everything
here is about the two questions the transport cannot answer: may this URL
be submitted at all, and did the attempt spend one of the property's
eleven slots.
"""
from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from . import quota, routing, runlog, store

log = runlog.get(__name__)


@dataclass(frozen=True)
class Disposition:
    """What one outcome does to the ledger and to the run."""

    spends_slot: bool
    hard_stop: bool
    label: str


# A slot is spent when a click plausibly reached Google. Where we cannot
# know — "timeout" — we assume it did: over-counting costs one slot, while
# under-counting fires into an exhausted property and earns a real Quota
# Exceeded, which is not recoverable within the rolling window. This mirrors
# store.reconcile()'s reasoning exactly, on purpose.
#
# "error" is the deliberate exception in the other direction. bridge.submit()
# guarantees only one direction — "timeout" implies a submit frame reached
# the socket — and NOT its converse: stop(), cancel() and exhausted resends
# can all return "error" after a send has already gone out. Charging nothing
# therefore under-counts in those narrow, already-degraded paths. That is an
# accepted risk recorded in bridge.submit()'s docstring, not an oversight;
# the reverse bias would burn a slot on every cancelled run.
DISPOSITIONS: dict[str, Disposition] = {
    "submitted":           Disposition(True,  False, "submitted"),
    "captcha_after_click": Disposition(True,  True,  "captcha_after_click"),
    "timeout":             Disposition(True,  False, "timeout"),
    "already_indexed":     Disposition(False, False, "already_indexed"),
    "quota_exceeded":      Disposition(False, True,  "quota_exceeded"),
    "rate_limited":        Disposition(False, True,  "rate_limited"),
    "auth_required":       Disposition(False, True,  "auth_required"),
    "account_mismatch":    Disposition(False, True,  "account_mismatch"),
    "captcha":             Disposition(False, True,  "captcha"),
    "skipped":             Disposition(False, False, "skipped"),
    "error":               Disposition(False, False, "error"),
}

# Only these are governed by the stop_on_throttle switch. quota_exceeded is
# deliberately NOT: the property has no capacity left, so continuing would
# submit into a guaranteed refusal whatever the user configured.
_THROTTLE_OUTCOMES = frozenset({"rate_limited"})

# Outcomes the extension reports WITHOUT having clicked Request Indexing.
# The run loop reads this to decide whether the next URL owes a gap; see the
# comment at the foot of run(). Adding an outcome here asserts that no click
# reached Google on that path — if that is not certain, leave it out.
_NO_CLICK_OUTCOMES = frozenset({"already_indexed", "skipped"})


def disposition_for(outcome: str, *, stop_on_throttle: bool) -> Disposition:
    """The ledger and run effect of one outcome. Raises KeyError if unknown.

    Loud on purpose: bridge.map_outcome has already coerced anything off
    the wire into the known vocabulary, so an unknown value here means the
    two tables have drifted apart, and a silent default would mis-charge
    quota for every URL after the drift.

    The loop-level results "no_property" and "no_quota" are not outcomes and
    have no entry here: neither ever opened a submission row, so neither has
    anything to dispose of. Reaching this function with one is a routing bug,
    and the KeyError is how it surfaces.

    stop_on_throttle only ever relaxes hard_stop, and only for a throttle. It
    never touches spends_slot: what an attempt cost Google cannot depend on a
    user preference about whether to keep going.
    """
    disposition = DISPOSITIONS[outcome]
    if disposition.hard_stop and not stop_on_throttle \
            and outcome in _THROTTLE_OUTCOMES:
        return Disposition(disposition.spends_slot, False, disposition.label)
    return disposition


@dataclass(frozen=True)
class Reservation:
    """A reserved slot, or the verdict explaining why there was none.

    submission_id is None exactly when verdict.allowed is False.
    """

    submission_id: int | None
    verdict: quota.QuotaVerdict


def reserve(conn: sqlite3.Connection, url: str, property: str, account: str,
            job_id: str | None, *, property_slots: int,
            account_slots: int | None, daily_reserve: int,
            now: datetime | None = None) -> Reservation:
    """Check the budget and open the submission row in ONE transaction.

    The two steps must not be separate: quota.check() reading "one slot
    free" and store.open_submission() writing the row are a classic
    check-then-act race, and two callers — two MCP tool calls, a tool and a
    job worker — would both read one free and both open a row, spending a
    slot the property did not have. store.tx() issues BEGIN IMMEDIATE, so
    the second caller blocks at the start and re-reads a store that already
    contains the first caller's open row (quota.used() counts open
    submissions, which is why this works at all).

    Gating is on quota.check()'s verdict and never on a bare quota.free():
    check() is the single place daily_reserve is applied, so free() answers a
    different question (raw remaining capacity) and would spend held-back
    slots.

    account_slots=None means the account dimension is tracked but not
    enforced; quota is per property.

    The caller must own this connection. tx()'s re-entrancy is
    connection-scoped, so two threads sharing one would nest silently and
    the inner RELEASE would commit nothing durably.
    """
    with store.tx(conn):
        verdict = quota.check(conn, account, property,
                              property_slots=property_slots,
                              account_slots=account_slots,
                              daily_reserve=daily_reserve, now=now)
        if not verdict.allowed:
            # No identifying detail: the account is sensitive and never
            # reaches a log line or an exception message.
            log.info("no spendable slot for %s (binding=%s)",
                     property, verdict.binding)
            return Reservation(None, verdict)
        submission_id = store.open_submission(conn, url, property, account,
                                              job_id)
        return Reservation(submission_id, verdict)


def settle(conn: sqlite3.Connection, submission_id: int, outcome: str, *,
           stop_on_throttle: bool) -> Disposition:
    """Close the submission row according to the outcome, and say what next.

    Exactly one of close_submission (spends the slot) and
    abandon_submission (does not) runs for every reservation. A row left
    open is not a leak that resolves itself: store.reconcile() will later
    close it and charge a slot, assuming the worst.

    The disposition is resolved BEFORE the store is touched, so an unknown
    outcome raises with the row still open. Leaving reconcile() to judge it
    later is strictly better than guessing a charge now.
    """
    disposition = disposition_for(outcome, stop_on_throttle=stop_on_throttle)
    if disposition.spends_slot:
        store.close_submission(conn, submission_id, disposition.label)
    else:
        store.abandon_submission(conn, submission_id, disposition.label)
    return disposition


# --- the run loop ------------------------------------------------------------


class Sender(Protocol):
    """The narrow slice of BridgeSession the loop needs.

    Stated as a Protocol so the loop can be tested against a fake without a
    socket, and so nothing in this module can quietly reach for a transport
    detail it has no business knowing about.
    """

    def submit(self, property: str, url: str, authuser: str) -> str: ...


@dataclass(frozen=True)
class Attempt:
    """What happened to one URL. `property` is None only for "no_property"."""

    url: str
    property: str | None
    outcome: str
    spent_slot: bool


@dataclass(frozen=True)
class RunResult:
    """Every attempt in input order, and why the run ended if it ended early.

    `stopped_early` is True exactly when `stop_reason` is set. A run that
    walked the whole list — including one whose every URL was unroutable —
    did not stop early, however little it achieved.
    """

    attempts: list[Attempt]
    stopped_early: bool
    stop_reason: str | None


#: Fallback for a cfg with no range. Mirrors config.DEFAULTS deliberately
#: rather than importing it: config.load() already layers the defaults in, so
#: a caller reaching here passed a hand-built dict, and the value that gets
#: used must still be the proven one. 130-180 seconds is measured against
#: live runs — a shorter gap earns a throttle, which costs the rest of the day.
_DEFAULT_DELAY_RANGE = (130.0, 180.0)


def _delay(cfg: dict) -> float:
    """One fresh gap, in seconds, drawn per send.

    Drawn per send and not once per run: a single reused value is a fixed
    beat, and the point of a range is that two consecutive gaps differ.
    """
    low, high = cfg.get("submit_delay_range") or _DEFAULT_DELAY_RANGE
    return random.uniform(float(low), float(high))


def _nothing_left_elsewhere(remaining: list[str], exhausted: str,
                            properties: list[str]) -> bool:
    """Would every remaining URL land on the property that just ran dry?

    If any remaining URL belongs to a different property, the run continues:
    a full property is a per-property fact, not a reason to abandon work
    that has budget. A URL that routes nowhere counts as "elsewhere" — it
    still has its own report line to produce, and producing it costs no
    quota and no wall clock.
    """
    return all(routing.resolve_property(url, properties) == exhausted
               for url in remaining)


def _report(on_progress: Callable[[Attempt], None] | None,
            attempts: list[Attempt], attempt: Attempt) -> None:
    """Record an attempt and tell the caller, without letting it derail us.

    The callback belongs to the caller (a job writing progress rows, a tool
    streaming a line). Its failure must not abort a run mid-flight: the run
    holds quota that has already been spent, and the remaining URLs would
    be left unattempted with no RunResult to say so.
    """
    attempts.append(attempt)
    if on_progress is None:
        return
    try:
        on_progress(attempt)
    except Exception as exc:  # noqa: BLE001 — the caller's bug, not the run's
        log.warning("progress callback failed (%s)", type(exc).__name__)


def run(conn: sqlite3.Connection, sender: Sender, urls: list[str], *,
        properties: list[str], account: str, job_id: str | None, cfg: dict,
        sleep: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[Attempt], None] | None = None) -> RunResult:
    """Submit each URL in turn, pacing between them and stopping on trouble.

    Two outcomes here are the loop's own and never come off the wire:
    "no_property" (routing found no property covering the URL) and
    "no_quota" (the property had no spendable slot). Neither reaches
    disposition_for, because neither opened a submission row — passing one
    would raise KeyError, which is the guard working, not a bug.

    An exhausted property stops the run only when every URL still to come
    would land on it. Quota is per property, so one full property must not
    end a run that still has work with budget elsewhere.

    Pacing is between SENDS, not between URLs: the gap exists to space real
    submissions, so a routing miss or an exhausted property costs no wall
    clock. `should_stop` is re-checked after each gap — the gap is minutes
    long, and a stop that arrives inside it must land before the next slot
    is reserved rather than after it is spent.

    The caller owns `conn` and must not share it with another thread:
    store.tx()'s re-entrancy is connection-scoped, and reserve() depends on
    BEGIN IMMEDIATE actually serialising two callers.

    `account` is sensitive and appears in no log line here, at any level.
    """
    attempts: list[Attempt] = []
    stop_reason: str | None = None
    stop_on_throttle = bool(cfg.get("stop_on_throttle", True))
    authuser = str(cfg.get("authuser", "0"))
    pending_delay: float | None = None

    for index, url in enumerate(urls):
        if should_stop is not None and should_stop():
            stop_reason = "stopped_by_user"
            break

        target_property = routing.resolve_property(url, properties)
        if target_property is None:
            _report(on_progress, attempts, Attempt(url, None, "no_property",
                                                   False))
            continue

        if pending_delay is not None:
            sleep(pending_delay)
            pending_delay = None
            if should_stop is not None and should_stop():
                stop_reason = "stopped_by_user"
                break

        reservation = reserve(conn, url, target_property, account, job_id,
                              property_slots=int(cfg.get("property_slots", 11)),
                              account_slots=cfg.get("account_slots"),
                              daily_reserve=int(cfg.get("daily_reserve", 0)))
        if reservation.submission_id is None:
            _report(on_progress, attempts,
                    Attempt(url, target_property, "no_quota", False))
            if _nothing_left_elsewhere(urls[index + 1:], target_property,
                                       properties):
                stop_reason = "no_quota"
                break
            continue

        try:
            outcome = sender.submit(target_property, url, authuser)
        except Exception as exc:  # noqa: BLE001 — the row MUST still settle
            # Leaving the row open would have quota.used() count it as spent
            # until reconcile() swept it, silently shrinking the budget.
            # Type name only: the exception text can carry the account.
            log.warning("submit failed for one URL (%s)", type(exc).__name__)
            outcome = "error"

        disposition = settle(conn, reservation.submission_id, outcome,
                             stop_on_throttle=stop_on_throttle)

        # Google just said the property is out of slots, on a property our
        # own ledger believed had capacity. Believe Google. The ledger only
        # counts what this tool spent, so manual submissions elsewhere -- or
        # a URL resubmitted by hand -- leave it legitimately short; the
        # refusal is the only signal that will ever correct it. Without this
        # the next caller reads the same stale free count and walks into the
        # same wall.
        if disposition.label == "quota_exceeded":
            with store.tx(conn):
                quota.mark_full(conn, account, target_property,
                                slots=int(cfg.get("property_slots", 11)))

        _report(on_progress, attempts,
                Attempt(url, target_property, disposition.label,
                        disposition.spends_slot))

        if disposition.hard_stop:
            stop_reason = disposition.label
            break

        # Only a click earns a gap. The extension inspects before it clicks,
        # so on these two outcomes Request Indexing was never touched and
        # there is no burst for the next URL to be part of. Pacing them
        # anyway made a five-URL batch of mostly-indexed pages sit silent for
        # ten minutes to perform one submission.
        #
        # Deliberately NOT keyed on `spends_slot`, which looks like the same
        # question and is not: "error" spends no slot and can still land
        # after a send has left the socket (see bridge.submit()). The ledger
        # under-counts there on purpose; the clock must not, because being
        # wrong costs a burst Google can see rather than one unspent slot.
        if outcome not in _NO_CLICK_OUTCOMES:
            pending_delay = _delay(cfg)

    return RunResult(attempts, stop_reason is not None, stop_reason)
