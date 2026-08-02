"""Request-Indexing slot accounting.

The limit is PER PROPERTY, not per account: roughly 11 slots per property,
each freeing 24h + 1min after its own use. Properties are fully independent,
so a user with eight properties has eight independent budgets.

The previous implementation keyed its ledger by account email, which modelled
the limit as per-account and under-counted capacity badly — one account with
seventeen properties was being capped at eleven submissions total.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
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


@dataclass(frozen=True)
class QuotaVerdict:
    """Whether a submission may proceed, and which limit is holding it back."""
    allowed: bool
    binding: str | None
    property_free: int
    account_free: int | None
    next_free_at: datetime | None


def account_used(conn: sqlite3.Connection, account: str, *,
                 now: datetime | None = None) -> int:
    """Slots this account spent across every property in the window.

    Counts open submissions as well as committed slots, and reads them in the
    same order as used() — so a submission closing between the two reads is
    counted twice rather than not at all. Over-counting wastes a slot;
    under-counting earns a hard Quota Exceeded.
    """
    moment = now or datetime.now(UTC)
    cutoff = _window_start(moment)
    in_flight = conn.execute(
        "SELECT COUNT(*) AS n FROM submissions "
        "WHERE account=? AND committed=0 AND requested_at > ?",
        (account, cutoff),
    ).fetchone()["n"]
    committed = conn.execute(
        "SELECT COUNT(*) AS n FROM quota_slots WHERE account=? AND used_at > ?",
        (account, cutoff),
    ).fetchone()["n"]
    return int(in_flight) + int(committed)


def _account_next_free(conn: sqlite3.Connection, account: str, *,
                       slots: int, now: datetime) -> datetime | None:
    """When this account's next slot frees, mirroring next_free() per property.

    Must stay in step with account_used(): if one counts a source the other
    ignores, a verdict can report no capacity and no wait time at once.
    """
    if max(0, slots - account_used(conn, account, now=now)) > 0:
        return None
    cutoff = _window_start(now)
    row = conn.execute(
        "SELECT MIN(stamp) AS oldest FROM ("
        "  SELECT used_at AS stamp FROM quota_slots "
        "   WHERE account=? AND used_at > ? "
        "  UNION ALL "
        "  SELECT requested_at AS stamp FROM submissions "
        "   WHERE account=? AND committed=0 AND requested_at > ? "
        ")",
        (account, cutoff, account, cutoff),
    ).fetchone()
    if row is None or row["oldest"] is None:
        return None
    return datetime.fromisoformat(row["oldest"]) + timedelta(
        minutes=SLOT_WINDOW_MINUTES
    )


def check(conn: sqlite3.Connection, account: str, property: str, *,
          property_slots: int = DEFAULT_PROPERTY_SLOTS,
          account_slots: int | None = None,
          now: datetime | None = None) -> QuotaVerdict:
    """Gate a submission on the property budget and, if configured, the account.

    account_slots=None means the account dimension is tracked but not enforced.
    No per-account ceiling has been observed; inventing one would cap users
    below what Google actually permits.
    """
    moment = now or datetime.now(UTC)
    property_free = free(conn, property, slots=property_slots, now=moment)

    account_free: int | None = None
    if account_slots is not None:
        account_free = max(0, account_slots - account_used(conn, account, now=moment))

    binding: str | None = None
    if property_free <= 0:
        binding = "property"
    elif account_free is not None and account_free <= 0:
        binding = "account"

    if binding is None:
        next_free_at = None
    else:
        computed = (
            next_free(conn, property, slots=property_slots, now=moment)
            if binding == "property"
            else _account_next_free(conn, account, slots=account_slots, now=moment)
        )
        # A slot can free between our two reads. Never return "blocked, with no
        # wait time" — that reads as "go ahead" to a caller and is exactly the
        # contradiction this pair of fields caused once already.
        next_free_at = computed or moment

    return QuotaVerdict(
        allowed=binding is None,
        binding=binding,
        property_free=property_free,
        account_free=account_free,
        next_free_at=next_free_at,
    )


# --- URL Inspection API quota -----------------------------------------------
#
# A completely separate mechanic from the Request-Indexing slots above: this
# accounts calls to the URL Inspection API (urlInspection.index.inspect), not
# clicks on the "Request Indexing" button. Google documents two ceilings --
# 2,000 calls/day and roughly 600/minute -- both enforced per property, the
# same granularity quota_slots already uses, so the two mechanics read the
# same way to anyone auditing this file.
#
# The daily limit is enforced as a ROLLING 24-hour window, not a calendar
# day, even though Google's own docs describe it as a daily quota. Google
# resets it on its own schedule in an unpublished timezone, so pinning this
# to UTC midnight (or any other fixed point) risks under-counting right after
# a real reset -- reading a property as having headroom it does not actually
# have, and firing into a hard rejection. A rolling window can only ever be
# *more* conservative than whatever fixed reset Google actually uses: worst
# case it makes a caller wait a little longer than strictly necessary, it can
# never let a call through that Google would refuse. This mirrors the rolling
# design already used for Request-Indexing slots above.

DAILY_INSPECTION_LIMIT = 2000
MINUTE_INSPECTION_LIMIT = 600

_DAILY_WINDOW = timedelta(hours=24)
_MINUTE_WINDOW = timedelta(seconds=60)


@dataclass(frozen=True)
class InspectionVerdict:
    """Whether an inspection call may proceed, and which window is binding."""
    allowed: bool
    daily_free: int
    minute_free: int
    binding: str | None            # "daily" | "minute" | None
    retry_after_seconds: int | None


def _daily_cutoff(now: datetime) -> str:
    return utc_iso(now - _DAILY_WINDOW)


def _minute_cutoff(now: datetime) -> str:
    return utc_iso(now - _MINUTE_WINDOW)


def record_inspections(conn: sqlite3.Connection, property: str, count: int,
                       when: datetime | None = None) -> None:
    """Record `count` URL Inspection API calls against `property`.

    Callers record BEFORE making the HTTP calls this accounts for, not after
    -- "reserve then spend", the same direction Plan 1 chose for
    close_submission()/used(): if the process dies mid-batch the count comes
    out too high, which only costs a wait, whereas recording afterwards would
    under-count and let a later batch walk into a hard API rejection.

    One row per call, matching quota_slots. Does not open its own
    transaction -- callers wrap this in store.tx(), exactly like every other
    accounting write in this module.
    """
    moment = when or datetime.now(UTC)
    stamp = utc_iso(moment)
    conn.executemany(
        "INSERT INTO inspection_calls (property, called_at) VALUES (?, ?)",
        [(property, stamp)] * count,
    )


def inspection_used(conn: sqlite3.Connection, property: str,
                    now: datetime | None = None) -> dict:
    """Calls spent on this property inside each rolling window.

    Compared as strings, not parsed datetimes -- store.utc_iso()'s fixed
    width is what makes that safe, so every timestamp must pass through it
    rather than being built by hand.
    """
    moment = now or datetime.now(UTC)
    day = conn.execute(
        "SELECT COUNT(*) AS n FROM inspection_calls "
        "WHERE property=? AND called_at >= ?",
        (property, _daily_cutoff(moment)),
    ).fetchone()["n"]
    minute = conn.execute(
        "SELECT COUNT(*) AS n FROM inspection_calls "
        "WHERE property=? AND called_at >= ?",
        (property, _minute_cutoff(moment)),
    ).fetchone()["n"]
    return {"day": int(day), "minute": int(minute)}


def _retry_after_seconds(conn: sqlite3.Connection, property: str,
                         binding: str, moment: datetime) -> int:
    """Seconds until the oldest row currently blocking `binding` ages out.

    Normalises `moment` through store.utc_iso() before doing arithmetic on
    it, the same as every timestamp elsewhere in this module -- `oldest`
    below is always tz-aware (parsed from a utc_iso-stamped column), and a
    caller-supplied naive `moment` subtracted against it raises TypeError.
    inspection_used()'s cutoffs dodge this because they hand `moment` to
    utc_iso() before ever comparing it; this is the one place that instead
    does datetime arithmetic directly, so it has to normalise for itself.
    """
    moment = datetime.fromisoformat(utc_iso(moment))
    if binding == "daily":
        cutoff, window = _daily_cutoff(moment), _DAILY_WINDOW
    else:
        cutoff, window = _minute_cutoff(moment), _MINUTE_WINDOW
    row = conn.execute(
        "SELECT MIN(called_at) AS oldest FROM inspection_calls "
        "WHERE property=? AND called_at >= ?",
        (property, cutoff),
    ).fetchone()
    oldest = datetime.fromisoformat(row["oldest"])
    remaining = ((oldest + window) - moment).total_seconds()
    return max(0, math.ceil(remaining))


def inspection_check(conn: sqlite3.Connection, property: str, wanted: int = 1,
                     now: datetime | None = None) -> InspectionVerdict:
    """Gate `wanted` inspection calls on both the daily and minute windows."""
    moment = now or datetime.now(UTC)
    used = inspection_used(conn, property, now=moment)
    daily_free = max(0, DAILY_INSPECTION_LIMIT - used["day"])
    minute_free = max(0, MINUTE_INSPECTION_LIMIT - used["minute"])

    binding: str | None = None
    if daily_free < wanted:
        binding = "daily"
    elif minute_free < wanted:
        binding = "minute"

    retry_after_seconds = (
        None if binding is None
        else _retry_after_seconds(conn, property, binding, moment)
    )

    return InspectionVerdict(
        allowed=binding is None,
        daily_free=daily_free,
        minute_free=minute_free,
        binding=binding,
        retry_after_seconds=retry_after_seconds,
    )


def prune_inspections(conn: sqlite3.Connection, keep_days: int = 2,
                      now: datetime | None = None) -> int:
    """Delete inspection_calls rows older than keep_days; return how many.

    Mirrors why this is safe at scale: even at the full 2,000/day ceiling on
    every property, a 2-day retention keeps this table small. No own
    transaction -- callers wrap this in store.tx().
    """
    moment = now or datetime.now(UTC)
    cutoff = utc_iso(moment - timedelta(days=keep_days))
    cursor = conn.execute(
        "DELETE FROM inspection_calls WHERE called_at < ?", (cutoff,)
    )
    return cursor.rowcount
