"""Search Analytics reporting — pure logic: date windows, request/response
shaping, and aggregation.

No network, no database, no I/O of any kind. Task 6 calls into this module
to build a searchAnalytics.query request body and to normalise/aggregate
whatever comes back; everything here is deterministic (clocks are injected
as plain arguments) so it is testable without touching Google.

Why `dataState` defaults to "all", against Google's own default
-----------------------------------------------------------------
The Search Analytics API's own default for `dataState` is "final", which
silently omits the most recent ~2-3 days of data (see FINAL_LAG_DAYS
below). Not partial rows, not a flag in the response saying "some days are
missing" — the days are simply absent, as if nothing happened on them.
Google finalises data a few days in arrears, so a "final" query only ever
describes days old enough to have finished that process.

The Search Console web UI has no such restriction: it shows the freshest
data immediately and revises it upward as more clicks/impressions land.
That divergence is what makes the API's "final" default actively
dangerous rather than merely conservative — a caller who queries "final"
for, say, the last 7 days and compares the numbers against what a human
is looking at in the UI right now will conclude the tool (or the API) is
broken, when really both are correct answers to two different questions
and nothing told the caller which one it got. This already cost one real
debugging round-trip on a live site: a genuine traffic spike in the most
recent days was completely invisible in "final" data and only showed up
once the identical date range was re-queried with `dataState="all"`.

DEFAULT_DATA_STATE is therefore "all", not "final" — on purpose. The
price of that choice is a loud, understandable caveat: the last day or two
of an "all" query are provisional and may revise upward the next time the
same range is queried. That is a far better failure mode than a silent
omission with no signal anywhere in the response. A future maintainer who
"corrects" this default back to match Google's documented behaviour will
reintroduce the exact bug this module exists to avoid — don't.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

MAX_ROW_LIMIT = 25000
DEFAULT_DAYS = 28
DEFAULT_ROW_LIMIT = 1000
DEFAULT_DATA_STATE = "all"
DATA_STATES = ("all", "final")
VALID_DIMENSIONS = ("query", "page", "country", "device", "date", "searchAppearance")
VALID_TYPES = ("web", "image", "video", "news", "discover", "googleNews")

# How many trailing days "final" typically has not yet settled — informational
# only (nothing here enforces it); it is why "all" exists as the default above.
FINAL_LAG_DAYS = 3

# hourly_all/HOUR live together in Task 6's hourly() call, not here. Kept as
# named constants so that module and this one agree on the literal values
# without either hard-coding the other's string.
HOURLY_DATA_STATE = "hourly_all"
HOUR_DIMENSION = "HOUR"
HOURLY_WINDOW_DAYS = 10  # Google only retains hourly data this far back.


class PerfError(RuntimeError):
    """Raised by callers (Task 6) that need a hard failure out of a
    Search Analytics response — this module's own functions raise
    ValueError for bad input instead, since that is a caller mistake, not
    an API-shaped one; PerfError is for the network layer built on top."""


def date_range(days: int = DEFAULT_DAYS, end: str | None = None,
               today: date | None = None) -> tuple[str, str]:
    """An inclusive (start, end) window of `days` calendar days, as ISO strings.

    Ends yesterday, not today: Search Console has nothing meaningful to say
    about a day that has not finished yet, and including "today" in a
    default window would put an always-empty or always-partial day at the
    end of every report. `today` is injectable so that rule is testable
    without depending on the real clock; it is ignored when `end` is given
    explicitly, since an explicit end date says exactly what the caller
    wants regardless of what day it is.

    Inclusive at both ends: a `days`-day window covers `days` calendar
    dates, so start is `end - (days - 1)`, not `end - days`.
    """
    if days <= 0:
        raise ValueError(f"days must be positive, got {days}")

    if end is not None:
        end_date = date.fromisoformat(end)
    else:
        end_date = (today or date.today()) - timedelta(days=1)

    start_date = end_date - timedelta(days=days - 1)
    return start_date.isoformat(), end_date.isoformat()


def validate_query(dimensions: list[str] | None, data_state: str,
                   search_type: str) -> None:
    """Raise ValueError on a request the API would reject; otherwise return None.

    `hourly_all` is a real dataState value, but it is only valid paired
    with the HOUR dimension, which is exclusive to Task 6's hourly() call —
    sending it here, with date/query dimensions, gets HTTP 400 from Google.
    DATA_STATES deliberately excludes it so that check below rejects it the
    same way it would reject any other unsupported string; this is not an
    oversight, it is what keeps a query-shaped call out of hourly-only
    territory.
    """
    if dimensions is not None:
        unknown = [d for d in dimensions if d not in VALID_DIMENSIONS]
        if unknown:
            raise ValueError(
                f"unknown dimension(s) {unknown}; must be one of {VALID_DIMENSIONS}")

    if data_state not in DATA_STATES:
        raise ValueError(
            f"unknown data_state {data_state!r}; must be one of {DATA_STATES} "
            f"({HOURLY_DATA_STATE!r} is only valid with the {HOUR_DIMENSION} "
            f"dimension, via hourly())")

    if search_type not in VALID_TYPES:
        raise ValueError(f"unknown type {search_type!r}; must be one of {VALID_TYPES}")


def normalize_row(row: dict, dimensions: list[str] | None) -> dict:
    """Turn one API row into a flat dict keyed by dimension name plus metrics.

    The API returns dimension values positionally in `row["keys"]`, in the
    same order the request's `dimensions` list was sent — this zips the two
    back together so a caller gets `{"query": "shoes", ...}` instead of
    having to remember index 0 means query. A totals row (no dimensions
    requested) has no "keys" at all, so `dimensions` is `None` and this
    contributes nothing but the metrics.

    Missing metrics default to zero rather than being omitted, so every
    normalised row has the same four metric keys regardless of what the API
    happened to include.
    """
    out: dict = {}
    if dimensions:
        out.update(zip(dimensions, row.get("keys", [])))

    out["clicks"] = row.get("clicks", 0)
    out["impressions"] = row.get("impressions", 0)
    out["ctr"] = row.get("ctr", 0.0)
    out["position"] = row.get("position", 0.0)
    return out


def aggregate_rows(rows: list[dict]) -> dict:
    """Sum clicks/impressions across `rows`; recompute ctr and position — do
    not average them.

    `ctr` is `clicks / impressions` over the whole set, never a mean of the
    per-row ctr values — averaging ctr treats a 1-impression row and a
    10,000-impression row as equally informative, which is not how a
    click-through rate works.

    `position` is impression-weighted — `sum(position * impressions) /
    sum(impressions)` — because that is how Google itself computes an
    aggregate position. A plain mean over-weights low-impression rows and
    reads far too optimistic: a rank-5 row seen 100 times and a rank-9 row
    seen 300 times is a real average position of 8 (mostly rank 9, seen far
    more), not 7 (splitting the difference as if both rows mattered
    equally). Reporting 7 when the true answer is 8 is exactly the kind of
    confidently wrong number this module exists to prevent.

    Both divisions are guarded against zero impressions — an empty result
    set, or a set of rows that are all impression-less, aggregates to 0.0
    rather than raising ZeroDivisionError.
    """
    clicks = sum(row.get("clicks", 0) for row in rows)
    impressions = sum(row.get("impressions", 0) for row in rows)

    if impressions:
        ctr = clicks / impressions
        position = sum(row.get("position", 0.0) * row.get("impressions", 0)
                       for row in rows) / impressions
    else:
        ctr = 0.0
        position = 0.0

    return {"clicks": clicks, "impressions": impressions, "ctr": ctr,
           "position": position}


def build_body(start_date: str, end_date: str, dimensions: list[str] | None = None,
              data_state: str = DEFAULT_DATA_STATE, search_type: str = "web",
              row_limit: int = DEFAULT_ROW_LIMIT, start_row: int = 0,
              filters: list[dict] | None = None) -> dict:
    """Assemble a searchAnalytics.query request body.

    `row_limit` is clamped to [1, MAX_ROW_LIMIT] rather than trusting the
    caller — the API hard-rejects anything above 25,000, and 0 or a
    negative row_limit would silently ask for nothing. `dimensions` and
    `filters` are omitted entirely rather than sent as empty lists, since
    an absent key and an empty-list value are not guaranteed to mean the
    same thing to the API.
    """
    body: dict = {
        "startDate": start_date,
        "endDate": end_date,
        "dataState": data_state,
        "type": search_type,
        "rowLimit": max(1, min(row_limit, MAX_ROW_LIMIT)),
        "startRow": start_row,
    }
    if dimensions:
        body["dimensions"] = dimensions
    if filters:
        body["dimensionFilterGroups"] = filters
    return body


def filter_hourly_rows(rows: list[dict], hours: int,
                       now: datetime | None = None) -> list[dict]:
    """Keep rows whose `hour` timestamp falls within the trailing `hours`
    window, oldest first.

    A row whose `hour` cannot be parsed is kept rather than dropped — a
    timestamp this module cannot make sense of is a signal something is
    wrong with the data, and silently discarding it would hide that behind
    what looks like a clean, in-window result. It sorts first (oldest),
    since there is no timestamp to rank it by and "unknown, so assume it
    matters" is the safer default than losing it at the end where a
    row-limited caller might never see it.
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(hours=hours)

    kept: list[tuple[datetime | None, dict]] = []
    for row in rows:
        try:
            when = datetime.fromisoformat(row["hour"])
        except (KeyError, ValueError):
            kept.append((None, row))
            continue
        if when >= cutoff:
            kept.append((when, row))

    kept.sort(key=lambda pair: pair[0] or datetime.min.replace(tzinfo=UTC))
    return [row for _, row in kept]
