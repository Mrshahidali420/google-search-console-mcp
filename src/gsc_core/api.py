"""Search Console API client — single-call primitives.

Every function here makes exactly one logical operation against the
Search Console REST API (list properties, inspect a URL, submit or list a
sitemap) and returns plain data: no MCP framing, no config lookups, no
retries beyond what the operation itself needs to survive a transient
Google-side hiccup.

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

import time
from collections.abc import Callable
from urllib.parse import quote

import requests

from . import runlog
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


class ApiError(RuntimeError):
    """Raised by callers that need a hard failure instead of an
    ("error", detail) tuple — inspect_url and submit_sitemap deliberately
    return rather than raise so a bulk caller can keep going past one bad
    URL, but callers with no such loop can wrap a result in this."""
