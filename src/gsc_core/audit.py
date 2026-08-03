"""A point-in-time indexation audit for one property.

WHAT THIS IS. The current position, read from the store: how many URLs are
known, how many have been checked, how many are indexed, and for those
that are not, the reason histogram and what each reason implies. It spends
no quota and makes no network call -- it reports what the last inspection
found, which is why every payload carries `as_at`.

WHAT THIS DELIBERATELY IS NOT. There are no movement numbers -- no
moved_to_indexed, no de_indexed, no submitted_then_dropped, no monthly
breakdown. The store records first_seen, checked_at and last_submitted
(store.py:38-46) and no status history at all, so those numbers have no
derivable input. They are OMITTED rather than reported as zero, because a
zero in a field named "moved to indexed" reads as a measurement that
found no movement, which is a stronger claim than "we do not know".
`basis: point_in_time` states this in the payload itself.

THE RULES A LATER MILESTONE MUST KEEP when history does arrive. Carried
from the private toolkit's audit, where they were learned against real
client challenges:

  1. Count a page as "moved to indexed" only if it was SUBMITTED and its
     first-indexed date falls inside the window. A page that indexed on
     its own is progress, but it is not work performed, and conflating the
     two invites a challenge that discredits the whole report.
  2. "Submitted and now indexed" is a weaker, undated claim than "moved to
     indexed". Report it as its own field; never merge it into the
     stronger one.
  3. "Needs action" is a current state with no date attached, so it can
     only ever be reported as at today. Attributing it to a past month
     claims knowledge that was never held.
  4. A percentage with no denominator is blank, never zero.

Only rule 4 is exercisable today. The others are written down while the
reasoning is in hand rather than rediscovered later.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from . import reasons, store


def audit(conn: sqlite3.Connection, property: str, *, ttl_days: int = 7,
          now: datetime | None = None) -> dict:
    """The current indexation position for one property.

    Reads only. Returns structured data with no prose rendering: turning
    this into a paragraph, a table, or a chart is the calling model's job,
    and shipping presentation vocabulary from here would constrain it.
    """
    moment = now or datetime.now(UTC)
    cutoff = store.utc_iso(moment - timedelta(days=ttl_days))
    rows = store.get_urls(conn, property)

    indexed = unindexed = undetermined = never_checked = stale = 0
    submittable = needs_access = no_action = 0
    by_reason: dict[str, int] = {}

    for row in rows:
        status = row["status"]
        checked_at = row["checked_at"]
        if checked_at is None:
            never_checked += 1
            stale += 1
        elif checked_at < cutoff:
            stale += 1

        if status is None:
            continue
        if status in reasons.INDEXED_STATUSES:
            indexed += 1
            continue
        code = reasons.reason_for(status)
        if code is None:
            undetermined += 1
            continue
        unindexed += 1
        by_reason[code] = by_reason.get(code, 0) + 1
        if code in reasons.SUBMITTING_HELPS:
            submittable += 1
        elif code in reasons.NEEDS_SITE_ACCESS:
            needs_access += 1
        else:
            # alt-canonical is the only code in neither set: it is working
            # as intended, so there is nothing to do. Named explicitly
            # rather than folded into a bare `else` so that a reason code
            # added without a bucket lands in none of the three -- visible
            # as buckets that no longer sum to unindexed -- instead of
            # being silently reported as needing no action.
            if code == "alt-canonical":
                no_action += 1

    checked = len(rows) - never_checked
    return {
        "ok": True,
        "property": property,
        "as_at": store.utc_iso(moment),
        "basis": "point_in_time",
        "total_known": len(rows),
        "checked": checked,
        "never_checked": never_checked,
        "indexed": indexed,
        "unindexed": unindexed,
        "undetermined": undetermined,
        "indexed_pct": pct(indexed, checked),
        "by_reason": by_reason,
        "submittable": submittable,
        "needs_site_access": needs_access,
        "no_action_needed": no_action,
        "stale": stale,
        "ttl_days": ttl_days,
    }


def pct(part: int, whole: int) -> float | None:
    """A percentage to one decimal, or None when there is no denominator.

    None rather than 0.0 deliberately: a percentage of nothing reads as a
    real measurement. A JSON tool hands back a number or a null and lets
    the caller render it -- unlike a spreadsheet cell, which needs a
    string.
    """
    if whole <= 0:
        return None
    return round(part * 100 / whole, 1)
