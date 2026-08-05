"""Request-Indexing slot accounting.

The limit is PER PROPERTY, not per account: roughly 11 slots per property,
each freeing 24h + 1min after its own use. Properties are independent, so a
user with eight properties has eight budgets. Re-confirmed live 2026-08-05,
against a refusal that looked at first like proof of a shared account budget.

Slots free back ONE AT A TIME, not in a daily batch, and that detail is the
one most likely to be lost. A property refused at 12:29 accepted a submission
at 12:42, refused again at 12:54, and accepted again at 13:22 — all on the
same account, property and machine. Nothing there is scoped to a device, an
IP or a browser session (each of those was tested and eliminated); it is just
individual slots ageing out of a rolling window. Anything in this module that
treats a refusal as a statement about the next 24 hours is wrong by
construction.

The previous implementation keyed its ledger by account email, which modelled
the limit as per-account and under-counted capacity badly — one account with
seventeen properties was being capped at eleven submissions total.

What this ledger CANNOT see is the thing to keep in mind before trusting it:
it counts only the slots this tool spent. Manual submissions in the browser,
another machine, and a URL resubmitted by hand are all invisible, so `free`
is an upper bound on real capacity, never a guarantee. That is why a refusal
from Google is treated as authoritative — see record_refusal() — and why
nothing should conclude anything about Google's rules from these numbers
alone. Callers that show `free` to a human owe them that caveat with it.
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

#: How long to stop submitting to a property after Google refuses it. Short on
#: purpose -- see record_refusal(). Slots return one at a time, so the useful
#: question after a refusal is "has one come back yet", and the only way to
#: learn the answer is to ask again. Asking is cheap: a refusal spends no slot.
#: Long enough not to hammer, short enough to catch the next slot; measured
#: gaps between slots freeing on a real property ran 13-28 minutes.
REFUSAL_COOLDOWN_MINUTES = 15


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


def _refusal_key(property: str) -> str:
    return f"refused_at:{property}"


def record_refusal(conn: sqlite3.Connection, property: str, *,
                   now: datetime | None = None) -> datetime:
    """Note that Google refused this property, and hold off briefly.

    Called when Google itself answers "Quota Exceeded". Read carefully what
    that answer does and does not tell you, because the previous
    implementation of this function got it wrong in a way that cost a whole
    afternoon of misdiagnosis on 2026-08-05.

    It tells you ONE thing: zero slots are free at this instant. It says
    nothing whatsoever about WHEN the next one frees. The window is rolling
    and each slot ages out 24h+1min after its own use, so on a property
    filled hours ago the slots come back one at a time, minutes apart.

    What used to happen here was a backfill: write enough synthetic
    quota_slots rows to read as fully spent, each stamped `now`. The comment
    justifying it argued that stamping `now` is "deliberately pessimistic",
    freeing slots slightly LATE, and that late merely costs a wait. That
    reasoning is what broke. The real slots were spent HOURS before the
    refusal and expire on their own earlier schedule; the synthetic ones
    expire 24h after the refusal. So the ledger reported a property as
    completely full straight through a window in which Google was handing
    capacity back every few minutes -- observed live, three successful manual
    submissions inside 45 minutes while the ledger insisted on zero. "Late"
    was not a small wait. It was a day-long lockout invented from a single
    bit of information.

    So: record the refusal, refuse to submit for a short cooldown, and then
    let the ledger's own estimate speak again. If capacity really is gone the
    next attempt is refused too -- and a refusal COSTS NO SLOT, which is the
    whole reason the cheap answer is also the correct one. Retrying is nearly
    free; inventing a day of state is not.

    Stored in `meta` rather than a table of its own: one row per property,
    overwritten in place, no migration, and nothing to prune. Returns the
    moment the cooldown ends. Does not open its own transaction -- callers
    wrap this in store.tx(), like every other accounting write here.
    """
    moment = now or datetime.now(UTC)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_refusal_key(property), utc_iso(moment)),
    )
    until = moment + timedelta(minutes=REFUSAL_COOLDOWN_MINUTES)
    log.info("quota refused by Google for %s; holding off %d min (no slot spent)",
             property, REFUSAL_COOLDOWN_MINUTES)
    return until


def cooling_off(conn: sqlite3.Connection, property: str, *,
                cooldown_minutes: int = REFUSAL_COOLDOWN_MINUTES,
                now: datetime | None = None) -> datetime | None:
    """When this property's post-refusal cooldown ends, or None if it is over.

    None is also the answer when Google has never refused this property, so
    callers get one shape for "nothing is holding this back".
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (_refusal_key(property),)
    ).fetchone()
    if row is None:
        return None
    moment = now or datetime.now(UTC)
    until = datetime.fromisoformat(row["value"]) + timedelta(minutes=cooldown_minutes)
    return until if until > moment else None


def last_refusal(conn: sqlite3.Connection, property: str) -> datetime | None:
    """The last time Google refused this property, or None. Never expires."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (_refusal_key(property),)
    ).fetchone()
    return None if row is None else datetime.fromisoformat(row["value"])


@dataclass(frozen=True)
class QuotaVerdict:
    """Whether a submission may proceed, and which limit is holding it back.

    `binding` is "property", "account", "refused", or None. "refused" is the
    odd one out and the only one sourced from Google rather than our own
    counting: it means Google said Quota Exceeded recently and we are sitting
    out a short cooldown. On that verdict `property_free` can be NONZERO while
    `allowed` is False — not a contradiction, it is the ledger's estimate
    being overruled by an observation, which is exactly what should happen.
    `next_free_at` then carries the end of the cooldown, i.e. when to ask
    Google again, not a moment at which a slot is known to exist.

    property_free and next_free_at are computed against check()'s
    RESERVE-ADJUSTED ceiling (property_slots - daily_reserve) whenever the
    caller passed a nonzero daily_reserve — they are NOT the same quantity
    quota.free() / quota.next_free() report, which stay pinned to the raw
    property_slots ceiling on purpose (see check()'s docstring). A caller
    holding a QuotaVerdict is holding "free/next-free to actually SPEND",
    already reserve-adjusted; it does not agree with a bare free()/
    next_free() call on the same property once daily_reserve is nonzero,
    and that is by design, not a bug to reconcile.
    """
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
          daily_reserve: int = 0,
          now: datetime | None = None) -> QuotaVerdict:
    """Gate a submission on the property budget and, if configured, the account.

    account_slots=None means the account dimension is tracked but not enforced.
    No per-account ceiling has been observed; inventing one would cap users
    below what Google actually permits.

    daily_reserve holds slots back from every caller of check(): the
    effective property ceiling used here is property_slots - daily_reserve,
    never property_slots itself. free() and next_free() are deliberately NOT
    given the reserve — they keep answering against the raw property_slots
    ceiling, so a caller reading them directly still sees true remaining
    capacity. The reserve is applied in this one place so there is exactly
    one rule for what a submission may actually spend, not two functions
    that can silently disagree about it.
    """
    moment = now or datetime.now(UTC)
    effective_slots = max(0, property_slots - daily_reserve)
    property_free = free(conn, property, slots=effective_slots, now=moment)

    account_free: int | None = None
    if account_slots is not None:
        account_free = max(0, account_slots - account_used(conn, account, now=moment))

    # A recent refusal outranks the ledger's own arithmetic, because it is the
    # only input here that came from Google rather than from our own counting.
    # It is deliberately NOT expressed by writing slots into the ledger -- see
    # record_refusal() for what that cost -- so it has to be consulted as its
    # own condition, and it expires on its own short clock.
    cooldown_until = cooling_off(conn, property, now=moment)
    if cooldown_until is not None:
        return QuotaVerdict(
            allowed=False,
            binding="refused",
            property_free=property_free,
            account_free=account_free,
            next_free_at=cooldown_until,
        )

    binding: str | None = None
    if property_free <= 0:
        binding = "property"
    elif account_free is not None and account_free <= 0:
        binding = "account"

    if binding is None:
        next_free_at = None
    else:
        computed = (
            next_free(conn, property, slots=effective_slots, now=moment)
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

    A window can be binding with nothing in it: `wanted` alone can exceed the
    ceiling, which is not hypothetical -- one check_status() call over a
    1,400-URL site asks for more than MINUTE_INSPECTION_LIMIT on a completely
    empty ledger. MIN() over no rows is NULL, so this used to raise TypeError
    and take the whole batch with it. Nothing is in the window, so nothing
    will age out of it and waiting cannot help; the caller has to ask for
    less. Report 0 -- "no wait will fix this" -- rather than None, which
    QuotaVerdict reserves for "not blocked at all" and which would hand a
    caller the blocked-with-no-wait-time contradiction check() takes care to
    avoid.
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
    if row is None or row["oldest"] is None:
        return 0
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
