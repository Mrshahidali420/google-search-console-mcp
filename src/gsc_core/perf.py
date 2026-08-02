"""Search Analytics reporting: date windows, request/response shaping,
aggregation (Task 5) plus the network layer built on top of it (Task 6).

The Task 5 half is pure logic — no network, no database, no I/O of any
kind — and stays that way; everything below the pagination/portfolio
section is what actually calls Google, with `session` and `sleep`
injected the same way `api.py` injects them, so it is testable without
touching the network or a clock either.

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

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import requests

from . import routing, runlog
from .gauth import TokenProvider

log = runlog.get(__name__)

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

QUERY_URI = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"

# Transient failures are worth a backoff-and-retry; anything else means
# retrying will just get the same answer again. Kept as perf.py's own
# constant rather than importing api.py's private one, so the two retry
# policies can be compared side by side without a cross-module dependency.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503})
_ERROR_DETAIL_LIMIT = 200

_session = requests.Session()


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


# --- network layer -----------------------------------------------------------
#
# Everything below actually calls Google. `properties: list[str]` replaces
# the private original's own property discovery: the caller supplies the
# list and `routing.resolve_property` does the matching, so there is no
# process-global properties cache and no lock guarding it here — the
# caller already has the list, so nothing needs to be memoized across
# calls.


def post_query(property: str, body: dict, provider: TokenProvider,
               session: requests.Session | None = None, max_retries: int = 4,
               sleep: Callable[[float], None] = time.sleep) -> dict:
    """POST one searchAnalytics.query request, retrying past transient failures.

    Mirrors api.inspect_url's retry policy exactly (see that function's
    docstring for the branch-by-branch reasoning) so the two clients cannot
    silently drift apart. The one difference: this raises PerfError rather
    than returning an error tuple — a paginated caller has no sane "error
    row" to substitute mid-loop.
    """
    client = session or _session
    uri = QUERY_URI.format(site=quote(property, safe=""))

    for attempt in range(max_retries):
        try:
            resp = client.post(
                uri,
                headers={"Authorization": f"Bearer {provider.access_token()}"},
                json=body,
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                raise PerfError(f"request failed: {exc}") from exc
            sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 401:
            provider.invalidate()
            continue

        if resp.status_code in _TRANSIENT_STATUSES:
            sleep(2 ** attempt * 2)
            continue

        raise PerfError(f"HTTP {resp.status_code}: {resp.text[:_ERROR_DETAIL_LIMIT]}")

    raise PerfError("retries exhausted (rate limited?)")


def _paginate(property: str, start_date: str, end_date: str, provider: TokenProvider,
             dimensions: list[str] | None = None, data_state: str = DEFAULT_DATA_STATE,
             search_type: str = "web", limit: int = DEFAULT_ROW_LIMIT,
             filters: list[dict] | None = None,
             session: requests.Session | None = None,
             sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """The pagination loop against an ALREADY-RESOLVED property.

    Preserve this contract exactly: `page_size = min(MAX_ROW_LIMIT, limit -
    len(rows))` when `limit` is truthy, else `MAX_ROW_LIMIT`; break when
    `page_size <= 0`; break when a page comes back shorter than requested.

    That last break is load-bearing, not an optimisation: it is what tells
    the loop Google has no more rows. Weakening it to "stop when the page
    is empty" breaks on the first empty page instead of the first short
    one — harmless against the real API, which never returns a partial
    page followed by more, but it turns a fake session that runs out of
    queued responses into an infinite loop instead of a failing test.

    `sleep` is forwarded to every post_query() call so a caller three
    layers up (query/totals/portfolio) can still avoid a real backoff
    delay when a page retries — without this seam retries inside a
    pagination loop always block on the real clock, no matter what the
    caller passed to query()/totals()/portfolio().
    """
    rows: list[dict] = []
    start_row = 0
    while True:
        page_size = min(MAX_ROW_LIMIT, limit - len(rows)) if limit else MAX_ROW_LIMIT
        if page_size <= 0:
            break

        body = build_body(start_date, end_date, dimensions=dimensions,
                          data_state=data_state, search_type=search_type,
                          row_limit=page_size, start_row=start_row, filters=filters)
        payload = post_query(property, body, provider, session=session, sleep=sleep)
        page = payload.get("rows", [])
        rows.extend(normalize_row(row, dimensions) for row in page)

        if len(page) < page_size:
            break
        start_row += page_size
    return rows


def _resolve_window(days: int, start_date: str | None,
                    end_date: str | None) -> tuple[str, str]:
    """An explicit (start_date, end_date) pair is used as-is; otherwise the
    window comes from date_range(days). date_range's own `today` is not
    threaded through here — query/totals/portfolio have no injected clock
    in their interface, only hourly() does."""
    if start_date and end_date:
        return start_date, end_date
    return date_range(days=days, end=end_date)


def query(site: str, properties: list[str], provider: TokenProvider,
         days: int = DEFAULT_DAYS, dimensions: list[str] | None = None,
         start_date: str | None = None, end_date: str | None = None,
         data_state: str = DEFAULT_DATA_STATE, search_type: str = "web",
         limit: int = DEFAULT_ROW_LIMIT, filters: list[dict] | None = None,
         session: requests.Session | None = None,
         sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """Paginated Search Analytics rows for `site`, normalised with dimension
    names. `limit=0` means every row Google will give.

    Validation happens before routing and before any request goes out —
    a caller mistake (bad dimension, bad data_state) should never spend a
    network call to discover it.
    """
    validate_query(dimensions, data_state, search_type)
    property = routing.resolve_property(site, properties)
    if property is None:
        raise PerfError(f"{site!r} matches no Search Console property")

    window_start, window_end = _resolve_window(days, start_date, end_date)
    return _paginate(property, window_start, window_end, provider,
                     dimensions=dimensions, data_state=data_state,
                     search_type=search_type, limit=limit, filters=filters,
                     session=session, sleep=sleep)


def totals(site: str, properties: list[str], provider: TokenProvider,
          days: int = DEFAULT_DAYS, start_date: str | None = None,
          end_date: str | None = None, data_state: str = DEFAULT_DATA_STATE,
          search_type: str = "web", filters: list[dict] | None = None,
          session: requests.Session | None = None,
          sleep: Callable[[float], None] = time.sleep) -> dict:
    """Site-wide aggregate metrics for the window, plus site/start/end.

    Queried with no dimensions, so Google returns at most one row; `limit=1`
    keeps that explicit rather than relying on the API to only ever send one.
    """
    property = routing.resolve_property(site, properties)
    if property is None:
        raise PerfError(f"{site!r} matches no Search Console property")

    window_start, window_end = _resolve_window(days, start_date, end_date)
    rows = _paginate(property, window_start, window_end, provider,
                     dimensions=None, data_state=data_state,
                     search_type=search_type, limit=1, filters=filters,
                     session=session, sleep=sleep)
    return {"site": property, "start": window_start, "end": window_end,
           **aggregate_rows(rows)}


def hourly(site: str, properties: list[str], provider: TokenProvider,
          hours: int = 24, search_type: str = "web",
          session: requests.Session | None = None,
          today: date | None = None,
          sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """Trailing `hours` of hourly Search Analytics data for `site`.

    `today`, if given, anchors "now" at the END of that date, not its
    start: production's real `now` is always mid-way through "today", and
    Google can never return data later than that real moment — the reason
    filter_hourly_rows' lower-bound-only cutoff is sufficient. Anchoring
    the injected seam to midnight instead would put "now" BEFORE the
    request's own end_date, so a fixture with data spanning through
    end_date would clear the lower bound in full and nothing would ever
    get filtered out. Requesting more than HOURLY_WINDOW_DAYS is rejected
    outright — Google does not retain hourly data that far back.

    dimensions/dataState are hardcoded to HOUR/hourly_all rather than going
    through validate_query, which rejects that combination everywhere else
    (see its docstring) — hourly() is the one call site allowed to use it.
    """
    if hours <= 0:
        raise ValueError(f"hours must be positive, got {hours}")
    if hours > HOURLY_WINDOW_DAYS * 24:
        raise ValueError(
            f"hours={hours} exceeds the {HOURLY_WINDOW_DAYS * 24}-hour "
            f"window Google retains hourly data for")

    property = routing.resolve_property(site, properties)
    if property is None:
        raise PerfError(f"{site!r} matches no Search Console property")

    now = (datetime.combine(today, datetime.max.time(), tzinfo=UTC) if today
          else datetime.now(UTC))
    end_date = now.date()
    span_days = min(HOURLY_WINDOW_DAYS, -(-hours // 24) + 1)
    start_date = end_date - timedelta(days=span_days - 1)

    body = build_body(start_date.isoformat(), end_date.isoformat(),
                      dimensions=[HOUR_DIMENSION], data_state=HOURLY_DATA_STATE,
                      search_type=search_type, row_limit=MAX_ROW_LIMIT)
    payload = post_query(property, body, provider, session=session, sleep=sleep)
    rows = [normalize_row(row, ["hour"]) for row in payload.get("rows", [])]
    return filter_hourly_rows(rows, hours, now=now)


def _zero_row(property: str, start_date: str, end_date: str, exc: Exception) -> dict:
    """A failed property's placeholder — same shape as a real totals() row,
    zeroed out, carrying why it failed rather than raising and sinking the
    whole portfolio run."""
    return {"site": property, "start": start_date, "end": end_date,
           "clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0,
           "error": str(exc)}


def portfolio(properties: list[str], provider: TokenProvider,
             days: int = DEFAULT_DAYS, start_date: str | None = None,
             end_date: str | None = None, data_state: str = DEFAULT_DATA_STATE,
             search_type: str = "web", concurrency: int = 4,
             session: requests.Session | None = None,
             sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    """One totals-shaped row per property, sorted busiest (clicks, then
    impressions) first.

    Each property is already a resolved property string, not a site to
    route — there is nothing to look up, so this calls _paginate() directly
    rather than going through query()/totals()'s routing.resolve_property.
    A property the token cannot read, or one that has since been deleted,
    must never sink the run: PerfError and ValueError are caught per
    property and turned into a zeroed row instead of propagating.
    """
    window_start, window_end = _resolve_window(days, start_date, end_date)

    def one(property: str) -> dict:
        try:
            rows = _paginate(property, window_start, window_end, provider,
                             dimensions=None, data_state=data_state,
                             search_type=search_type, limit=1, session=session,
                             sleep=sleep)
            return {"site": property, "start": window_start, "end": window_end,
                   **aggregate_rows(rows)}
        except (PerfError, ValueError) as exc:
            # Never log `exc` itself: PerfError's message can carry a
            # truncated response body (see post_query), and a body is fine
            # to return to a caller but must never reach a logger. Only
            # the property name and exception type go to the log; the
            # full detail still reaches the caller via the row's "error"
            # key below, which is not a log sink.
            log.warning("portfolio: %s failed with %s", property,
                        type(exc).__name__)
            return _zero_row(property, window_start, window_end, exc)

    workers = max(1, min(concurrency, len(properties)))
    if workers <= 1:
        results = [one(property) for property in properties]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, properties))

    return sorted(results, key=lambda row: (row["clicks"], row["impressions"]),
                 reverse=True)
