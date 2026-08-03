"""Submission policy: what an outcome means for quota, and how a slot is
reserved without a check-then-act race.

The bridge is transport and knows nothing about any of this. Everything
here is about the two questions the transport cannot answer: may this URL
be submitted at all, and did the attempt spend one of the property's
eleven slots.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import quota, runlog, store

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
