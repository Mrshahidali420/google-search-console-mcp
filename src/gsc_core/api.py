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
from datetime import datetime, timedelta, UTC
from urllib.parse import quote

import requests

from . import quota, routing, runlog, store
from .transport import transport_failure
from . import gauth
from .gauth import AuthRequired, TokenProvider

log = runlog.get(__name__)

# gauth.py owns this literal (verify_token() needs it too, and gauth cannot
# import api without a cycle). Importing it here, rather than restating the
# string a second time, is what keeps the two from ever silently drifting.
SITES_URI = gauth.SITES_ENDPOINT
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

    A NON-200 RAISES. This is the one place the boundary can sit: "[] " and
    "the call was refused" are indistinguishable downstream, and every
    caller reads [] as a fact about the account. gsc_check_status labels
    every URL `no_property` — "no Search Console property matches this
    host" — off the back of it, so a 403 from a token missing the
    webmasters scope would tell a user their sites are unknown to Google.
    Unlike inspect_url and submit_sitemap, which return an error row so a
    bulk loop survives one bad URL, this call has no per-item loop to keep
    going for: if it fails there is no answer at all, only a wrong one.

    - 401 -> AuthRequired. The token is missing, expired or rejected;
      that is already the signal every tool turns into the structured
      "sign in" answer, so it does not need a second spelling.
    - anything else, including a body that is not JSON -> ApiError
      carrying the status, so a caller can tell a 403 (scope) from a 500
      (Google) without parsing a string.
    """
    client = session or _session
    resp = client.get(SITES_URI, headers=_auth_headers(provider), timeout=30)
    if resp.status_code == 401:
        raise AuthRequired(
            "Search Console rejected the access token (HTTP 401)")
    if resp.status_code != 200:
        raise ApiError(
            f"sites.list returned HTTP {resp.status_code}",
            status=resp.status_code)
    try:
        body = resp.json()
    except ValueError as exc:
        raise ApiError("sites.list returned a body that is not JSON",
                       status=resp.status_code) from exc
    return body.get("siteEntry", [])


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
      2 ** attempt seconds and retries, reporting on the final attempt --
      the exception's type name, never its message. See
      transport.transport_failure for what that message can contain.
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
                log.warning("inspect_url: transport failure (%s)",
                            type(exc).__name__)
                return "error", transport_failure(exc)
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
#
# Which is exactly why a suspect the loop never managed to re-check must not
# be returned looking like one it confirmed. Re-verification is itself quota
# gated, and it can also run out of time or hit the per-round cap; in every
# one of those cases the row keeps its suspect status but is flagged
# `unverified` and says why in its detail. "We asked again and Google still
# says unknown" and "we never got to ask" are different claims, and only one
# of them is worth acting on.
#
# On timestamps: the batch's `now` is a single checked_at shared by every row
# in the pass, but quota rows are stamped at the moment their calls actually
# go out — `now` plus the elapsed monotonic. A re-check made 300 seconds in
# and recorded as if it happened at T would leave the rolling 60-second
# window 300 seconds early, and the NEXT batch would read budget it does not
# have. Every other bias in this module and in quota.py runs the other way:
# over-count, never under-count.

MAX_WORKERS = 15
MAX_REVERIFY_ROUNDS = 4
REVERIFY_COOLDOWN_S = 5.0     # doubles each round
REVERIFY_GAP_S = 1.0          # between sequential re-checks within a round

# Only a sequential re-check can confirm any of these.
SUSPECT_STATUSES = frozenset({"unknown_to_google", "unknown", "error"})

_NO_PROPERTY_DETAIL = "no Search Console property matches this host"

_WHY_QUOTA = "re-check quota exhausted"
_WHY_CAP = "round cap reached"
_WHY_BUDGET = "time budget spent"


def check_status(
    conn: sqlite3.Connection, urls: list[str], provider: TokenProvider,
    properties: list[str], concurrency: int = 8, time_budget_s: float = 900.0,
    max_suspects_per_round: int = 200, now: datetime | None = None,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    *, _inspect: Callable[..., tuple[str, str]] | None = None,
) -> dict:
    """Inspect many URLs, re-verify what a burst may have degraded, persist.

    Route -> gate and RESERVE -> inspect concurrently -> re-verify
    sequentially -> persist. The order is the contract; the section comment
    above says why, including how unverified rows and quota timestamps are
    handled. A URL no property covers costs nothing. A URL repeated in the
    input is inspected ONCE against one reserved slot, and every occurrence
    of it gets that one result.

    `now`, `sleep` and `monotonic` are injected so a test runs in
    milliseconds rather than in wall-clock minutes.
    """
    moment = now or datetime.now(UTC)
    started = monotonic()
    inspect = _inspect or inspect_url
    routed = routing.route_all(urls, properties)

    # Housekeeping before the gate, not after: this is the only code path
    # that writes inspection_calls, so pruning here is the one place the
    # table's documented two-day retention can actually be enforced. It
    # cannot change a verdict — the widest rolling window the gate reads is
    # one day and prune_inspections keeps two — and it is a single indexed
    # DELETE, so paying for it once per call is cheaper than the unbounded
    # growth it prevents. Its own transaction, since it is not part of the
    # reserve-then-spend atom and must not widen it.
    with store.tx(conn):
        quota.prune_inspections(conn, now=moment)

    reserved = _reserve(conn, _group_by_property(routed), moment)
    targets = _targets(routed, reserved)

    workers = max(1, min(concurrency, MAX_WORKERS, len(targets)))
    results = _inspect_all(targets, provider, inspect, session, sleep, workers)
    unverified = _unverified(
        conn, targets, results, provider, inspect, session, workers=workers,
        moment=moment, started=started, time_budget_s=time_budget_s,
        max_suspects_per_round=max_suspects_per_round, sleep=sleep,
        monotonic=monotonic)
    _persist(conn, targets, results, store.utc_iso(moment))

    skipped = _skipped_rows(reserved)
    return {
        "rows": _rows(routed, results, skipped, unverified),
        "checked": len(targets),
        "skipped_quota": list(skipped.values()),
        "quota": _quota_report(reserved, unverified),
    }


def _unverified(conn: sqlite3.Connection, targets: list[tuple[str, str]],
                results: dict[str, tuple[str, str]], provider: TokenProvider,
                inspect: Callable[..., tuple[str, str]],
                session: requests.Session | None, *, workers: int,
                **kwargs) -> dict[str, str]:
    """Run the re-verification pass; return {url: why} for what it could not
    confirm.

    A single-worker pass cannot have been degraded by a burst — that is the
    entire premise of the loop — so it has nothing to re-verify and nothing
    left doubtful. Returning {} rather than "everything is unverified" is the
    difference between a sequential run reporting clean results and reporting
    every unknown as untrustworthy.
    """
    if workers <= 1:
        return {}
    unreached = _reverify(conn, dict(targets), results, provider, inspect,
                          session, **kwargs)
    return {url: why for url, why in unreached.items()
            if results[url][0] in SUSPECT_STATUSES}


def _targets(routed: list[tuple[str, str | None]],
             reserved: dict[str, tuple[list[str], list[str], object]],
             ) -> list[tuple[str, str]]:
    """The (url, property) pairs to inspect, deduplicated, in input order.

    dict.fromkeys is the dedupe: a URL listed twice was reserved once, and
    letting both copies through would send two requests against one reserved
    slot — under-counting, which is the direction reserve-then-spend exists
    to prevent.
    """
    granted = {url: property
               for property, (ok, _, _) in reserved.items() for url in ok}
    return [(url, granted[url]) for url in dict.fromkeys(url for url, _ in routed)
            if url in granted]


def _group_by_property(
    routed: list[tuple[str, str | None]]
) -> dict[str, list[str]]:
    """Routable URLs bucketed by property, deduplicated, input order kept.

    Quota is per property, so the gate has to see whole per-property batches;
    checking one URL at a time would let 200 individually-allowed calls walk
    past a ceiling that only 150 of them fit under. Duplicates are dropped
    here so the reservation matches the number of calls actually made — see
    _targets(), which dedupes the same way.
    """
    grouped: dict[str, list[str]] = {}
    seen: set[str] = set()
    for url, property in routed:
        if property is not None and url not in seen:
            seen.add(url)
            grouped.setdefault(property, []).append(url)
    return grouped


def _reserve(
    conn: sqlite3.Connection, grouped: dict[str, list[str]], when: datetime,
) -> dict[str, tuple[list[str], list[str], quota.InspectionVerdict]]:
    """Gate each property against its inspection budget and reserve the rest.

    Returns {property: (granted, deferred, verdict)}. Everything happens in
    one transaction on the CALLING thread, and it is complete — committed —
    before the caller makes its first HTTP call.

    `when` is the moment the calls will actually go out, used both to age the
    rolling windows and to stamp the recorded rows. Re-verification rounds
    pass their own, later, `when` for exactly that reason.

    A partially-allowed property takes min(daily_free, minute_free): the
    verdict names only the first binding window, and honouring that one alone
    would fire the other straight into a rejection.
    """
    outcome: dict[str, tuple[list[str], list[str], quota.InspectionVerdict]] = {}
    with store.tx(conn):
        for property, wanted in grouped.items():
            verdict = quota.inspection_check(conn, property, wanted=len(wanted),
                                             now=when)
            allowed = (len(wanted) if verdict.allowed
                       else min(verdict.daily_free, verdict.minute_free))
            if allowed:
                quota.record_inspections(conn, property, allowed, when=when)
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


def _mark(unreached: dict[str, str], urls: list[str],
          reason: str) -> dict[str, str]:
    """Record why these suspects were not re-checked. Returns the dict so a
    caller can mark-and-return in one line."""
    for url in urls:
        unreached[url] = reason
    return unreached


def _cap(suspects: list[str], limit: int,
         unreached: dict[str, str]) -> list[str]:
    """Trim a round to `limit` suspects, recording what that dropped."""
    if len(suspects) <= limit:
        return suspects
    log.warning("re-verification capped at %d of %d suspect(s) this round; "
                "%d left unverified", limit, len(suspects),
                len(suspects) - limit)
    _mark(unreached, suspects[limit:], _WHY_CAP)
    return suspects[:limit]


def _reserve_round(conn: sqlite3.Connection, suspects: list[str],
                   properties: dict[str, str], unreached: dict[str, str],
                   when: datetime) -> list[str]:
    """Reserve quota for one re-verification round; return what it granted.

    Re-verification calls are inspections like any other — reserved on this
    thread, before the round's first request, same as the batch itself. What
    the budget refuses is marked unreached rather than quietly dropped: a
    suspect nobody re-checked must never come back looking confirmed.

    An empty return stops the loop rather than continuing it. Nothing was
    looked at, so nothing changed, so the remaining rounds would each cool
    down and hit the same wall — four cooldowns spent to learn nothing.
    """
    reserved = _reserve(conn, _group_by_property(
        [(url, properties[url]) for url in suspects]), when)
    granted = [url for _, (ok, _, _) in reserved.items() for url in ok]
    _mark(unreached, [url for url in suspects if url not in set(granted)],
          _WHY_QUOTA)
    if not granted:
        log.warning("re-verification quota exhausted; %d suspect result(s) "
                    "left unconfirmed", len(suspects))
    return granted


def _reverify(conn: sqlite3.Connection, properties: dict[str, str],
              results: dict[str, tuple[str, str]], provider: TokenProvider,
              inspect: Callable[..., tuple[str, str]],
              session: requests.Session | None, *, moment: datetime,
              started: float, time_budget_s: float,
              max_suspects_per_round: int, sleep: Callable[[float], None],
              monotonic: Callable[[], float]) -> dict[str, str]:
    """Re-check suspect results sequentially, in place, until they settle.

    A round cools down first, then walks its suspects one at a time with a
    gap between them — the point is to be the opposite of the burst that
    produced the suspect answers, so both waits are load-bearing. Stops on
    whichever comes first: nothing suspect left, a round that flipped
    nothing, a round the budget refused outright, the time budget, or four
    rounds.

    Returns {url: reason} for every suspect it could NOT re-check; empty
    means every remaining suspect was confirmed sequentially.
    """
    unreached: dict[str, str] = {}
    rechecked: set[str] = set()
    cooldown = REVERIFY_COOLDOWN_S
    for round_number in range(1, MAX_REVERIFY_ROUNDS + 1):
        suspects = [url for url, (status, _) in results.items()
                    if status in SUSPECT_STATUSES]
        if not suspects:
            break
        if monotonic() - started > time_budget_s:
            log.warning("time budget of %ss spent after %d re-verification "
                        "round(s); %d row(s) left unverified", time_budget_s,
                        round_number - 1, len(suspects))
            _mark(unreached, suspects, _WHY_BUDGET)
            break

        sleep(cooldown)
        # Stamped when the calls actually go out, not at the batch start.
        granted = _reserve_round(
            conn, _cap(suspects, max_suspects_per_round, unreached), properties,
            unreached, moment + timedelta(seconds=max(0.0, monotonic() - started)))
        if not granted:
            break   # nothing looked at, nothing changed; see _reserve_round
        rechecked.update(granted)
        if not _recheck(granted, properties, results, provider, inspect,
                        session, sleep):
            log.info("re-verification round %d flipped nothing; treating %d "
                     "unknown result(s) as real", round_number, len(granted))
            break
        cooldown *= 2
    return _settle(unreached, rechecked)


def _settle(unreached: dict[str, str], rechecked: set[str]) -> dict[str, str]:
    """Drop every mark against a URL some round did manage to re-check.

    A later round's cap, refusal or expired budget cannot un-ask a question
    an earlier round already answered. Reporting such a URL as unverified is
    a false alarm about our own work — the mirror image of reporting an
    unreached suspect as confirmed, and just as untrue an account of what ran.
    """
    return {url: why for url, why in unreached.items() if url not in rechecked}


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
    """Write the settled rows in one transaction, skipping any the store rejects.

    The try/except is what delivers "one bad row costs one row" — not a
    savepoint. SQLite already rolls back the individual failed statement and
    leaves the enclosing transaction usable, so wrapping each row in its own
    store.tx() adds a SAVEPOINT that never has anything to undo; an earlier
    revision did exactly that and no test could tell it apart from its
    absence. What the transaction here IS for is committing 1,400 rows once
    instead of 1,400 times.

    upsert_url opens its own store.tx, which nests inside this one as a
    savepoint. That is its business, not a guarantee this function leans on.
    """
    with store.tx(conn):
        for url, property in targets:
            status, detail = results[url]
            try:
                store.upsert_url(conn, url, property, status, detail,
                                 checked_at)
            except Exception:  # noqa: BLE001 — one bad row must not end the batch
                log.warning("could not store the result for %s; skipping it",
                            url, exc_info=True)


def _skipped_rows(
    reserved: dict[str, tuple[list[str], list[str], quota.InspectionVerdict]]
) -> dict[str, dict]:
    return {
        url: {"url": url, "property": property,
              "binding_at_gate": verdict.binding,
              "retry_after_seconds": verdict.retry_after_seconds}
        for property, (_, deferred, verdict) in reserved.items()
        for url in deferred
    }


def _rows(routed: list[tuple[str, str | None]],
          results: dict[str, tuple[str, str]], skipped: dict[str, dict],
          unverified: dict[str, str]) -> list[dict]:
    """One row per input URL, in input order — including the ones that never
    reached the API, so a caller can account for every URL it handed over.

    `unverified` is True only for a suspect status no sequential re-check
    confirmed, and the reason is appended to the detail. Without it a caller
    cannot tell a checked "unknown to Google" from one the re-verification
    pass ran out of budget before reaching — and would act on both.
    """
    rows: list[dict] = []
    for url, property in routed:
        if property is None:
            row = {"url": url, "status": "no_property",
                   "detail": _NO_PROPERTY_DETAIL}
        elif url in skipped:
            retry = skipped[url]["retry_after_seconds"]
            row = {"url": url, "status": "skipped_quota",
                   "detail": f"inspection quota exhausted; retry in {retry}s"}
        else:
            status, detail = results[url]
            why = unverified.get(url)
            row = {"url": url, "status": status,
                   "detail": detail if why is None
                             else f"{detail} | not re-verified: {why}"}
        rows.append({**row, "unverified": url in unverified})
    return rows


def _quota_report(
    reserved: dict[str, tuple[list[str], list[str], quota.InspectionVerdict]],
    unverified: dict[str, str],
) -> dict[str, dict]:
    """Per-property accounting. daily_free_at_gate/minute_free_at_gate are the
    headroom seen AT THE GATE, before this batch reserved any of it — not what
    is left now.

    The `_at_gate` suffix is not decoration. gsc_quota reports fields called
    `daily_free`/`minute_free` measured at the moment it is asked; these are
    measured before a reservation this very call then spends, so the two
    differ by the size of the batch. Naming both pairs identically invited a
    caller to compare them, or to treat this one as current headroom and
    launch a second batch against budget that is already gone.

    `binding_at_gate` carries the suffix for the same reason and no other.
    gsc_quota's `binding` covers submission AND inspection budgets and is
    read now; this one is inspection-only and read at the gate. The two
    value sets do not overlap ("submission" cannot appear here), so nothing
    silently mis-compares today — but a name that means two things is the
    defect this rename exists to remove, and applying it to one field and
    not its neighbour would ship half of it.

    `unverified` counts rows whose suspect status no re-check confirmed, so a
    caller reading only this summary still sees that the pass was incomplete.
    """
    return {
        property: {
            "attempted": len(granted),
            "deferred": len(deferred),
            "unverified": sum(1 for url in granted if url in unverified),
            "binding_at_gate": verdict.binding,
            "retry_after_seconds": verdict.retry_after_seconds,
            "daily_free_at_gate": verdict.daily_free,
            "minute_free_at_gate": verdict.minute_free,
        }
        for property, (granted, deferred, verdict) in reserved.items()
    }


class ApiError(RuntimeError):
    """Raised by callers that need a hard failure instead of an
    ("error", detail) tuple — inspect_url and submit_sitemap deliberately
    return rather than raise so a bulk caller can keep going past one bad
    URL, but callers with no such loop can wrap a result in this.

    `status` is the HTTP status when there was one, else None. It carries
    the status as a NUMBER rather than leaving a caller to parse it out of
    the message, and the message never includes a response body: this
    exception's text can reach a log, and a body cannot.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
