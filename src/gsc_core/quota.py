"""Request-Indexing slot accounting.

The limit is PER PROPERTY, not per account: roughly 11 slots per property,
each freeing 24h + 1min after its own use. Properties are fully independent,
so a user with eight properties has eight independent budgets.

The previous implementation keyed its ledger by account email, which modelled
the limit as per-account and under-counted capacity badly — one account with
seventeen properties was being capped at eleven submissions total.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, UTC

from . import runlog
from .store import utc_iso

log = runlog.get(__name__)

SLOT_WINDOW_MINUTES = 1441  # 24h + 1min, proven against live runs
DEFAULT_PROPERTY_SLOTS = 11


def _window_start(now: datetime) -> str:
    """The cutoff, in the same normalised form quota_slots.used_at is stored in.

    Slot ages are compared as strings, so this must go through the same
    normaliser the writer used — otherwise a differing offset or microsecond
    width silently shifts the window by hours.
    """
    return utc_iso(now - timedelta(minutes=SLOT_WINDOW_MINUTES))


def used(conn: sqlite3.Connection, property: str, *,
         now: datetime | None = None) -> int:
    """Slots spent on this property inside the rolling window.

    Counts committed slots AND submissions still open, because a submission
    that has been clicked but not yet closed has really spent its slot at
    Google even though no quota_slots row exists yet. Reconcile only sweeps
    rows older than its grace period, so without this an in-flight submission
    is invisible to both — and a second process would read the property as
    having more capacity than it does.

    The two SELECTs are separate reads on an autocommit connection, not one
    transaction — no store.tx here, since that issues BEGIN IMMEDIATE and
    would take a write lock for what is a pure read. That leaves a race: a
    submission closing between the two reads. Reading submissions first and
    quota_slots second is deliberate — it biases that race toward counting
    the same submission twice (over-count by one, wasting a slot) rather
    than the reverse order, which can count it zero times (under-count,
    which is what earns a hard Quota Exceeded). Over-counting is
    recoverable; under-counting is not.
    """
    moment = now or datetime.now(UTC)
    cutoff = _window_start(moment)
    in_flight = conn.execute(
        "SELECT COUNT(*) AS n FROM submissions "
        "WHERE property=? AND committed=0 AND requested_at > ?",
        (property, cutoff),
    ).fetchone()["n"]
    committed = conn.execute(
        "SELECT COUNT(*) AS n FROM quota_slots WHERE property=? AND used_at > ?",
        (property, cutoff),
    ).fetchone()["n"]
    return int(committed) + int(in_flight)


def free(conn: sqlite3.Connection, property: str, *,
         slots: int = DEFAULT_PROPERTY_SLOTS,
         now: datetime | None = None) -> int:
    return max(0, slots - used(conn, property, now=now))


def next_free(conn: sqlite3.Connection, property: str, *,
              slots: int = DEFAULT_PROPERTY_SLOTS,
              now: datetime | None = None) -> datetime | None:
    """When the next slot frees, or None if slots are already available.

    The MIN() below must span the same two sources used() counts — quota_slots
    and submissions still open — and stay in step with it. If one counts a
    source the other ignores, this exact hole reopens: a property filled
    entirely by the uncounted source reads as having capacity and no wait
    time, and a caller submits into a hard Quota Exceeded.
    """
    moment = now or datetime.now(UTC)
    if free(conn, property, slots=slots, now=moment) > 0:
        return None

    cutoff = _window_start(moment)
    row = conn.execute(
        "SELECT MIN(stamp) AS oldest FROM ("
        "  SELECT used_at AS stamp FROM quota_slots "
        "   WHERE property=? AND used_at > ? "
        "  UNION ALL "
        "  SELECT requested_at AS stamp FROM submissions "
        "   WHERE property=? AND committed=0 AND requested_at > ? "
        ")",
        (property, cutoff, property, cutoff),
    ).fetchone()
    if row is None or row["oldest"] is None:
        return None
    return datetime.fromisoformat(row["oldest"]) + timedelta(
        minutes=SLOT_WINDOW_MINUTES
    )
