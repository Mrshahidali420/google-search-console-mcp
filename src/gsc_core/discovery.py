"""Find which of a property's URLs are not indexed, and why.

The pipeline is: assemble candidates -> record them as discovered ->
inspect only what has gone stale -> classify. Each stage has one job and
one reason for the shape it has.

WHY IT DOES NOT INSPECT EVERYTHING. Inspection spends a 2000/day budget
(quota.py). store.stale_urls (store.py:338) exists precisely to make
this incremental, so a second call on the same day costs nothing. But a
URL that is fresh is still REPORTED, from the status already stored --
otherwise the second call returns an empty unindexed set, which a calling
model reads as "everything is fine now".

WHY IT DOES NOT WRITE ITS OWN INSPECTION LOOP. api.check_status
(api.py:282) already routes, reserves quota, re-verifies what a concurrent
burst may have silently degraded, and persists. A second loop would
recreate the burst-degradation failure api.py:238-265 documents as the
most expensive lie this tool could tell.

WHY UNDETERMINED IS A SEPARATE ARRAY. "unknown", "error" and "no_property"
are not evidence that a page is missing from the index. Putting them in
`unindexed` with a null reason would report a failed HTTP call as a
finding about someone's website.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Callable

import requests

from . import api, reasons, runlog, sitemaps, store

log = runlog.get(__name__)

SOURCES = frozenset({"sitemap", "store", "both"})


def find_unindexed(
    conn: sqlite3.Connection, property: str, provider: Any,
    properties: list[str], *, source: str = "both", limit: int | None = None,
    ttl_days: int = 7, concurrency: int = 8,
    session: requests.Session | None = None, now: datetime | None = None,
    _fetch: Callable[..., sitemaps.SitemapResult] | None = None,
    _check: Callable[..., dict] | None = None,
    _sitemap_urls: list[str] | None = None,
) -> dict:
    """Which URLs of one property are not in Google's index, and why.

    `source` chooses the candidate set, not whether inspection happens:
    "sitemap" fetches and parses the property's registered sitemaps fresh,
    "store" uses only URLs already seen, "both" (the default) unions them
    with sitemap order first. `limit` caps how many URLs are INSPECTED,
    not how many are returned, because inspection is what costs budget.

    The `_fetch`, `_check` and `_sitemap_urls` seams exist so tests run
    without a network or a clock.
    """
    if source not in SOURCES:
        raise ValueError(source)

    moment = now or datetime.now(UTC)
    fetch = _fetch or sitemaps.fetch_urls
    check = _check or api.check_status

    read: list[str] = []
    failures: list[sitemaps.SitemapFailure] = []
    from_sitemap: list[str] = []

    if source in ("sitemap", "both"):
        targets = _sitemap_urls
        if targets is None:
            targets = [entry.get("path", "") for entry
                       in api.list_sitemaps(property, provider, session)]
            targets = [t for t in targets if t]
        for target in targets:
            result = fetch(target, session)
            read.extend(result.sitemaps_read)
            failures.extend(result.failures)
            from_sitemap.extend(result.urls)
        _record_discovery(conn, property, from_sitemap, moment)

    candidates = _candidates(conn, property, source, from_sitemap)
    known = set(candidates)
    stale = [url for url in store.stale_urls(conn, property, ttl_days, moment)
             if url in known]
    inspect_now = stale if limit is None else stale[:limit]

    quota: dict = {}
    fresh_results: dict[str, dict] = {}
    if inspect_now:
        outcome = check(conn, inspect_now, provider, properties,
                        concurrency=concurrency, now=moment, session=session)
        quota = outcome.get("quota", {})
        for row in outcome.get("rows", []):
            fresh_results[row["url"]] = row

    return _classify(
        conn=conn, property=property, source=source, candidates=candidates,
        inspected=inspect_now, fresh_results=fresh_results, stale=stale,
        limit=limit, read=read, failures=failures, quota=quota)


def _record_discovery(conn: sqlite3.Connection, property: str,
                      urls: list[str], moment: datetime) -> None:
    """Write newly seen URLs with no status.

    store.upsert_url's contract (store.py:297-307) reserves
    status/reason/checked_at of None for exactly this: a discovery pass
    carries no inspection result, so None means "no new information" and
    an existing status survives untouched. One transaction for the batch.
    """
    if not urls:
        return
    with store.tx(conn):
        for url in urls:
            store.upsert_url(conn, url, property, None, None, None)


def _candidates(conn: sqlite3.Connection, property: str, source: str,
                from_sitemap: list[str]) -> list[str]:
    """The URL set this run considers, in a stable order.

    Sitemap order first, then anything else the store already knows, each
    URL once. This order is what the returned findings are reported in, so
    a caller reads the sitemap's own priority first. It is NOT the order
    inspection happens in: `limit` truncates the stale list, which
    store.stale_urls (store.py:338) returns in its own order.
    """
    if source == "sitemap":
        return list(dict.fromkeys(from_sitemap))
    stored = [row["url"] for row in store.get_urls(conn, property)]
    if source == "store":
        return stored
    return list(dict.fromkeys([*from_sitemap, *stored]))


def _classify(*, conn: sqlite3.Connection, property: str, source: str,
              candidates: list[str], inspected: list[str],
              fresh_results: dict[str, dict], stale: list[str],
              limit: int | None, read: list[str],
              failures: list[sitemaps.SitemapFailure],
              quota: dict) -> dict:
    """Sort every candidate into unindexed / undetermined / indexed."""
    stored = {row["url"]: row for row in store.get_urls(conn, property)}
    unindexed: list[dict] = []
    undetermined: list[dict] = []
    indexed = 0

    for url in candidates:
        row = fresh_results.get(url)
        was_inspected = row is not None
        if row is None:
            record = stored.get(url, {})
            status = record.get("status")
            detail = record.get("reason") or ""
            checked_at = record.get("checked_at")
            unverified = None
        else:
            status = row.get("status")
            detail = row.get("detail") or ""
            checked_at = stored.get(url, {}).get("checked_at")
            unverified = row.get("unverified")

        if status is None:
            # Never inspected and not inspected this run -- the `limit`
            # cut it. Not a finding either way.
            continue
        if status in reasons.INDEXED_STATUSES:
            indexed += 1
            continue
        if unverified is not None or status in reasons.UNDETERMINED_STATUSES:
            undetermined.append({"url": url, "status": status,
                                 "detail": detail, "unverified": unverified})
            continue

        code = reasons.reason_for(status)
        if code is None:
            # A status api.classify() can emit that reasons.py does not
            # know. tests/test_reasons.py makes this unreachable; if it
            # ever fires, reporting it as undetermined is the honest
            # answer rather than guessing a reason.
            log.info("unmapped inspection status in discovery")
            undetermined.append({"url": url, "status": status,
                                 "detail": detail, "unverified": None})
            continue

        unindexed.append({**reasons.describe(code), "url": url,
                          "detail": detail, "checked_at": checked_at,
                          "fresh": not was_inspected})

    return {
        "ok": True,
        "property": property,
        "source": source,
        "candidates_total": len(candidates),
        "inspected": len(inspected),
        "limited": limit is not None and len(stale) > len(inspected),
        "limit": limit,
        "indexed": indexed,
        "unindexed": unindexed,
        "undetermined": undetermined,
        "sitemaps_read": read,
        "sitemap_failures": [{"url": f.url, "reason": f.reason}
                             for f in failures],
        "quota": quota,
    }
