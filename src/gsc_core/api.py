"""Search Console API client — single-call primitives, and one bulk pass.

Most functions here make exactly one logical operation against the Search
Console REST API (list properties, inspect a URL, submit or list a sitemap)
and return plain data: no MCP framing, no config lookups, no retries beyond
what the operation itself needs to survive a transient Google-side hiccup.

check_status() is the deliberate exception. It is the one caller that has to
compose routing, quota accounting, concurrency and persistence, because the
correctness of the whole thing depends on their ORDER — reserve before
spending, inspect off-thread, re-verify before believing, persist last. Split
across modules that ordering becomes a convention someone can break; here it
is a single readable sequence.

Two seams keep this testable without touching the network or a clock:

- `session` defaults to a module-level `requests` session but every call
  accepts an override, so tests inject a fake that records calls and
  returns queued responses.
- `inspect_url`'s `sleep` defaults to `time.sleep` but accepts an override,
  so retry tests run instantly instead of actually backing off.

Nothing in this module logs the bearer token or a full response body. A
truncated `resp.text[:200]` inside a returned error tuple is fine — that
value goes back to the caller, not to a logger — but it must never be
passed to `log.*`.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, UTC
from urllib.parse import quote

import requests

from . import quota, routing, runlog, store
from .gauth import TokenProvider

log = runlog.get(__name__)

SITES_URI = "https://www.googleapis.com/webmasters/v3/sites"
INSPECT_URI = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
SITEMAPS_URI = "https://www.googleapis.com/webmasters/v3/sites/{site}/sitemaps"

# Exact coverage strings the Search Console UI/API returns, established
# against live data. Copied verbatim — do not paraphrase, "tidy" casing, or
# fix apparent typos (the spaced hyphen in "crawled - currently not
# indexed" and the apostrophes in "excluded by 'noindex' tag" are real). A
# wording that drifts from what Google actually sends silently falls
# through to the verdict-based fallback in classify() and reports
# "unknown" instead of the true status.
COVERAGE_MAP: dict[str, str] = {
    "submitted and indexed": "indexed",
    "indexed, not submitted in sitemap": "indexed",
    "crawled - currently not indexed": "crawled_not_indexed",
    "discovered - currently not indexed": "discovered_not_indexed",
    "url is unknown to google": "unknown_to_google",
    "page with redirect": "redirect",
    "excluded by 'noindex' tag": "noindex",
    "duplicate without user-selected canonical": "duplicate",
    "duplicate, google chose different canonical than user": "duplicate",
    "alternate page with proper canonical tag": "alternate_canonical",
    "not found (404)": "not_found",
    "soft 404": "soft_404",
    "blocked by robots.txt": "blocked_robots",
}

# Transient failures are worth a backoff-and-retry; anything else means
# retrying will just get the same answer again.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503})

_ERROR_DETAIL_LIMIT = 200

_session = requests.Session()


def classify(payload: dict) -> tuple[str, str]:
    """Turn an inspect_url response body into a (status, detail) pair.

    Pure and side-effect free: no network, no logging, so callers can unit
    test coverage-string handling without a fake session in the loop.
    """
    inspection = payload.get("inspectionResult") or {}
    index_status = inspection.get("indexStatusResult") or {}

    coverage = index_status.get("coverageState")
    verdict = index_status.get("verdict")

    if coverage:
        status = COVERAGE_MAP.get(coverage.lower().strip())
        if status is None:
            status = "indexed" if verdict == "PASS" else "unknown"
    else:
        status = "indexed" if verdict == "PASS" else "unknown"

    detail = coverage or verdict or "no indexStatusResult"
    last_crawl = index_status.get("lastCrawlTime")
    if last_crawl:
        detail = f"{detail} | last crawl {last_crawl[:10]}"
    return status, detail


def _auth_headers(provider: TokenProvider) -> dict[str, str]:
    return {"Authorization": f"Bearer {provider.access_token()}"}


def list_properties(provider: TokenProvider,
                    session: requests.Session | None = None) -> list[dict]:
    """All Search Console properties this account can see.

    Each entry is `{"siteUrl": ..., "permissionLevel": ...}` straight from
    the API; an account with no verified properties returns an empty list
    rather than a missing key.
    """
    client = session or _session
    resp = client.get(SITES_URI, headers=_auth_headers(provider), timeout=30)
    return resp.json().get("siteEntry", [])


def inspect_url(url: str, property: str, provider: TokenProvider,
                session: requests.Session | None = None, max_retries: int = 4,
                sleep: Callable[[float], None] = time.sleep) -> tuple[str, str]:
    """Inspect one URL's index status, retrying past transient failures.

    - 200 -> classified via classify().
    - 401 -> the token was likely stale; invalidate it and retry on the next
      loop iteration with a fresh one, without spending a backoff sleep (an
      expired token is not a rate-limit signal, and a genuine 429 later in
      the run needs the full retry budget).
    - 429/500/502/503 -> exponential backoff (2 ** attempt * 2 seconds) and
      retry; these are Google-side and typically resolve on their own.
    - Any other status -> return an error immediately; retrying a 403 or a
      404 just gets the same answer.
    - A transport exception (timeout, connection error) backs off with
      2 ** attempt seconds and retries, reporting the exception text only
      on the final attempt.
    """
    client = session or _session
    body = {"inspectionUrl": url, "siteUrl": property}

    for attempt in range(max_retries):
        try:
            resp = client.post(
                INSPECT_URI,
                headers=_auth_headers(provider),
                json=body,
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt == max_retries - 1:
                return "error", f"request failed: {exc}"
            sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            return classify(resp.json())

        if resp.status_code == 401:
            provider.invalidate()
            continue

        if resp.status_code in _TRANSIENT_STATUSES:
            sleep(2 ** attempt * 2)
            continue

        return "error", f"HTTP {resp.status_code}: {resp.text[:_ERROR_DETAIL_LIMIT]}"

    return "error", "retries exhausted (rate limited?)"


def submit_sitemap(property: str, sitemap_url: str, provider: TokenProvider,
                   session: requests.Session | None = None) -> dict:
    """PUT a sitemap onto a property. Idempotent — safe to resubmit.

    Both path segments are percent-encoded with safe="" because either can
    legitimately contain characters ("sc-domain:", "://", query strings)
    that would otherwise be interpreted as URL structure rather than data.
    """
    client = session or _session
    uri = SITEMAPS_URI.format(site=quote(property, safe="")) + "/" + quote(
        sitemap_url, safe="")
    resp = client.put(uri, headers=_auth_headers(provider), timeout=30)
    ok = resp.status_code in (200, 204)
    return {
        "site": property,
        "sitemap": sitemap_url,
        "http_status": resp.status_code,
        "ok": ok,
        "note": "" if ok else resp.text[:_ERROR_DETAIL_LIMIT],
    }


def list_sitemaps(property: str, provider: TokenProvider,
                  session: requests.Session | None = None) -> list[dict]:
    """All sitemaps registered against a property."""
    client = session or _session
    uri = SITEMAPS_URI.format(site=quote(property, safe=""))
    resp = client.get(uri, headers=_auth_headers(provider), timeout=30)
    return resp.json().get("sitemap", [])


# --- bulk status checking ----------------------------------------------------
#
# Under a concurrent burst the URL Inspection API silently degrades real
# coverage states to "URL is unknown to Google" — the row comes back 200 OK
# and well-formed, just wrong. There is no signal in the response that
# distinguishes a degraded answer from a true one, so the only way to tell is
# to ask again SEQUENTIALLY and see whether the answer changes. That is what
# the re-verification loop below exists for, and it is why a status of
# unknown/error out of the concurrent pass is treated as a question rather
# than an answer. Removing it does not make check_status faster; it makes it
# report healthy pages as missing from Google's index, which is the single
# most expensive lie this tool could tell.

MAX_WORKERS = 15
MAX_REVERIFY_ROUNDS = 4
REVERIFY_COOLDOWN_S = 5.0     # doubles each round
REVERIFY_GAP_S = 1.0          # between sequential re-checks within a round

# Only a sequential re-check can confirm any of these.
SUSPECT_STATUSES = frozenset({"unknown_to_google", "unknown", "error"})

_NO_PROPERTY_DETAIL = "no Search Console property matches this host"


def check_status(
    conn: sqlite3.Connection,
    urls: list[str],
    provider: TokenProvider,
    properties: list[str],
    concurrency: int = 8,
    time_budget_s: float = 900.0,
    max_suspects_per_round: int = 200,
    now: datetime | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    *,
    _inspect: Callable[..., tuple[str, str]] | None = None,
) -> dict:
    """Inspect many URLs, re-verify what a burst may have degraded, persist.

    The sequence is the contract:

    1. Route. A URL no property covers is reported as "no_property" and costs
       nothing — no quota, no HTTP, no row.
    2. Gate and RESERVE, in one transaction, before a single request goes out.
       A crash mid-batch then over-counts, which costs the user a wait;
       recording afterwards would under-count and earn a hard rejection from
       Google, which costs them the rest of the day.
    3. Inspect concurrently. Worker threads call inspect_url and nothing else
       — `conn` never crosses a thread boundary, because sqlite3 forbids it
       and one-connection-per-caller makes sharing wrong regardless.
    4. Re-verify suspects sequentially (see the note above).
    5. Persist, every row under its own SAVEPOINT so one unstorable row skips
       instead of taking the batch with it.

    `now`, `sleep` and `monotonic` are injected so a test can run this in
    milliseconds. `now` is captured ONCE and used for every quota stamp and
    every checked_at in the batch: rows checked in one pass share one
    timestamp, and a long run's quota arithmetic stays pinned to the start,
    which can only ever over-state how much of the rolling minute window is
    still occupied — the safe direction.
    """
    moment = now or datetime.now(UTC)
    started = monotonic()
    inspect = _inspect or inspect_url
    routed = routing.route_all(urls, properties)

    reserved = _reserve(conn, _group_by_property(routed), moment)
    granted = {url: prop for prop, (ok, _, _) in reserved.items() for url in ok}
    targets = [(url, granted[url]) for url, _ in routed if url in granted]

    workers = max(1, min(concurrency, MAX_WORKERS, len(targets)))
    results = _inspect_all(targets, provider, inspect, session, sleep, workers)
    if workers > 1:
        _reverify(conn, dict(targets), results, provider, inspect, session,
                  moment=moment, started=started, time_budget_s=time_budget_s,
                  max_suspects_per_round=max_suspects_per_round,
                  sleep=sleep, monotonic=monotonic)
    _persist(conn, targets, results, store.utc_iso(moment))

    skipped = _skipped_rows(reserved)
    return {
        "rows": _rows(routed, results, skipped),
        "checked": len(targets),
        "skipped_quota": list(skipped.values()),
        "quota": _quota_report(reserved),
    }


def _group_by_property(
    routed: list[tuple[str, str | None]]
) -> dict[str, list[str]]:
    """Routable URLs bucketed by property, input order preserved.

    Quota is per property, so the gate has to see whole per-property batches;
    checking one URL at a time would let 200 individually-allowed calls walk
    past a ceiling that only 150 of them fit under.
    """
    grouped: dict[str, list[str]] = {}
    for url, property in routed:
        if property is not None:
            grouped.setdefault(property, []).append(url)
    return grouped


def _reserve(
    conn: sqlite3.Connection, grouped: dict[str, list[str]], moment: datetime,
) -> dict[str, tuple[list[str], list[str], quota.InspectionVerdict]]:
    """Gate each property against its inspection budget and reserve the rest.

    Returns {property: (granted, deferred, verdict)}. Everything happens in
    one transaction on the CALLING thread, and it is complete — committed —
    before the caller makes its first HTTP call.

    A partially-allowed property takes min(daily_free, minute_free): the
    verdict names only the first binding window, and honouring that one alone
    would fire the other straight into a rejection.
    """
    outcome: dict[str, tuple[list[str], list[str], quota.InspectionVerdict]] = {}
    with store.tx(conn):
        for property, wanted in grouped.items():
            verdict = quota.inspection_check(conn, property, wanted=len(wanted),
                                             now=moment)
            allowed = (len(wanted) if verdict.allowed
                       else min(verdict.daily_free, verdict.minute_free))
            if allowed:
                quota.record_inspections(conn, property, allowed, when=moment)
            if allowed < len(wanted):
                log.warning(
                    "%s: inspection quota allows %d of %d call(s); %d deferred "
                    "(%s limit, retry in %ss)", property, allowed, len(wanted),
                    len(wanted) - allowed, verdict.binding,
                    verdict.retry_after_seconds)
            outcome[property] = (wanted[:allowed], wanted[allowed:], verdict)
    return outcome


def _safe_inspect(inspect: Callable[..., tuple[str, str]], url: str,
                  property: str, provider: TokenProvider,
                  session: requests.Session | None,
                  sleep: Callable[[float], None]) -> tuple[str, str]:
    """Call inspect, turning an unexpected raise into one bad row.

    inspect_url returns ("error", detail) rather than raising, so reaching
    this handler means a bug or an exception type it does not model. Either
    way the batch has already spent its quota reservation, and letting one
    URL abort the pass would throw away every other result with it. "error"
    is a suspect status, so a transient one still gets a second look from the
    re-verification loop.
    """
    try:
        return inspect(url, property, provider, session=session, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
        log.warning("inspecting %s raised %s; recording it as an error row",
                    url, type(exc).__name__)
        return "error", f"inspect raised {type(exc).__name__}"


def _inspect_all(targets: list[tuple[str, str]], provider: TokenProvider,
                 inspect: Callable[..., tuple[str, str]],
                 session: requests.Session | None,
                 sleep: Callable[[float], None],
                 workers: int) -> dict[str, tuple[str, str]]:
    """The concurrent pass. Worker threads call inspect and nothing else.

    No database handle, no shared mutable state, no logging of response
    bodies — a worker's entire contract is (url, property) in, (status,
    detail) out. That is what makes the "threads never touch the DB" rule
    structural rather than a comment someone has to remember.

    A single worker runs inline instead of through a one-thread pool. It is
    the same work either way, and keeping it on the calling thread means a
    sequential run has no thread in the picture at all.
    """
    if workers <= 1:
        return {url: _safe_inspect(inspect, url, property, provider, session, sleep)
                for url, property in targets}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_safe_inspect, inspect, url, property, provider,
                        session, sleep): url
            for url, property in targets
        }
        return {url: future.result() for future, url in futures.items()}


def _reverify(conn: sqlite3.Connection, properties: dict[str, str],
              results: dict[str, tuple[str, str]], provider: TokenProvider,
              inspect: Callable[..., tuple[str, str]],
              session: requests.Session | None, *, moment: datetime,
              started: float, time_budget_s: float,
              max_suspects_per_round: int, sleep: Callable[[float], None],
              monotonic: Callable[[], float]) -> None:
    """Re-check suspect results sequentially, in place, until they settle.

    A round cools down first, then walks its suspects one at a time with a
    gap between them — the whole point is to be the opposite of the burst
    that produced the suspect answers, so both waits are load-bearing.

    The loop stops on whichever comes first: nothing suspect left, a round
    that flipped nothing (those unknowns are real), the time budget, or four
    rounds. Truncations and abandonments are logged, because a silently
    shortened pass reads downstream as "everything was verified".
    """
    cooldown = REVERIFY_COOLDOWN_S
    for round_number in range(1, MAX_REVERIFY_ROUNDS + 1):
        suspects = [url for url, (status, _) in results.items()
                    if status in SUSPECT_STATUSES]
        if not suspects:
            return
        if monotonic() - started > time_budget_s:
            log.warning("time budget of %ss spent after %d re-verification "
                        "round(s); %d row(s) left unverified", time_budget_s,
                        round_number - 1, len(suspects))
            return
        if len(suspects) > max_suspects_per_round:
            log.warning("re-verification capped at %d of %d suspect(s) this "
                        "round; %d left unverified", max_suspects_per_round,
                        len(suspects), len(suspects) - max_suspects_per_round)
            suspects = suspects[:max_suspects_per_round]

        sleep(cooldown)
        # Re-verification calls are inspections like any other: reserved on
        # this thread, before the round's first request, same as the batch.
        granted = _reserve(conn, _group_by_property(
            [(url, properties[url]) for url in suspects]), moment)
        flipped = _recheck(
            [url for _, (ok, _, _) in granted.items() for url in ok],
            properties, results, provider, inspect, session, sleep)
        if not flipped:
            log.info("re-verification round %d flipped nothing; treating %d "
                     "unknown result(s) as real", round_number, len(suspects))
            return
        cooldown *= 2


def _recheck(suspects: list[str], properties: dict[str, str],
             results: dict[str, tuple[str, str]], provider: TokenProvider,
             inspect: Callable[..., tuple[str, str]],
             session: requests.Session | None,
             sleep: Callable[[float], None]) -> int:
    """One sequential round. Updates results in place; returns how many
    suspects came back with a trustworthy status."""
    flipped = 0
    for index, url in enumerate(suspects):
        if index:
            sleep(REVERIFY_GAP_S)
        status, detail = _safe_inspect(inspect, url, properties[url], provider,
                                       session, sleep)
        if status not in SUSPECT_STATUSES:
            flipped += 1
        results[url] = (status, detail)
    return flipped


def _persist(conn: sqlite3.Connection, targets: list[tuple[str, str]],
             results: dict[str, tuple[str, str]],
             checked_at: str | None) -> None:
    """Write the settled rows, one SAVEPOINT each inside one transaction.

    store.tx is re-entrant, so the inner block rolls back only its own row.
    One URL the store refuses therefore costs one row, not the other 1,399.
    """
    with store.tx(conn):
        for url, property in targets:
            status, detail = results[url]
            try:
                with store.tx(conn):
                    store.upsert_url(conn, url, property, status, detail,
                                     checked_at)
            except Exception:  # noqa: BLE001 — one bad row must not end the batch
                log.warning("could not store the result for %s; skipping it",
                            url, exc_info=True)


def _skipped_rows(
    reserved: dict[str, tuple[list[str], list[str], quota.InspectionVerdict]]
) -> dict[str, dict]:
    return {
        url: {"url": url, "property": property, "binding": verdict.binding,
              "retry_after_seconds": verdict.retry_after_seconds}
        for property, (_, deferred, verdict) in reserved.items()
        for url in deferred
    }


def _rows(routed: list[tuple[str, str | None]],
          results: dict[str, tuple[str, str]],
          skipped: dict[str, dict]) -> list[dict]:
    """One row per input URL, in input order — including the ones that never
    reached the API, so a caller can account for every URL it handed over."""
    rows: list[dict] = []
    for url, property in routed:
        if property is None:
            rows.append({"url": url, "status": "no_property",
                         "detail": _NO_PROPERTY_DETAIL})
        elif url in skipped:
            retry = skipped[url]["retry_after_seconds"]
            rows.append({"url": url, "status": "skipped_quota",
                         "detail": f"inspection quota exhausted; "
                                   f"retry in {retry}s"})
        else:
            status, detail = results[url]
            rows.append({"url": url, "status": status, "detail": detail})
    return rows


def _quota_report(
    reserved: dict[str, tuple[list[str], list[str], quota.InspectionVerdict]]
) -> dict[str, dict]:
    """Per-property accounting. daily_free/minute_free are the headroom seen
    AT THE GATE, before this batch reserved any of it — not what is left
    now."""
    return {
        property: {
            "attempted": len(granted),
            "deferred": len(deferred),
            "binding": verdict.binding,
            "retry_after_seconds": verdict.retry_after_seconds,
            "daily_free": verdict.daily_free,
            "minute_free": verdict.minute_free,
        }
        for property, (granted, deferred, verdict) in reserved.items()
    }


class ApiError(RuntimeError):
    """Raised by callers that need a hard failure instead of an
    ("error", detail) tuple — inspect_url and submit_sitemap deliberately
    return rather than raise so a bulk caller can keep going past one bad
    URL, but callers with no such loop can wrap a result in this."""
