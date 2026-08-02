"""The MCP server shell: tool registration — `gsc_list_sites`, `gsc_doctor`,
`gsc_check_status`, `gsc_quota`.

Import order is load-bearing. `runlog.init()` runs before `FastMCP` is
constructed, and before anything else in this module can log a line —
stdout is the MCP JSON-RPC transport, so a stray print (from this module,
from a library it imports, or from a handler that logs to the wrong
stream) corrupts every message after it, and the failure surfaces to a
client as an unrelated parse error, not as a bug report pointing back
here. runlog itself only ever attaches stderr and file handlers (see its
module docstring), so calling init() first is what guarantees nothing
downstream has a chance to reach for the true root logger before this
module's own logging is nailed down.

Every tool below opens its own database connection via `deps.connection()`
and builds its own TokenProvider via `deps.provider()` — see deps.py's
docstring for why a shared connection or provider is unsafe here. Tools
return plain dicts/lists and never let an exception cross the MCP
boundary: a raised exception is far less useful to a calling model than a
structured `{"ok": False, "error": ..., "fix": ...}` it can act on.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from gsc_core import api, config, gauth, paths, quota, routing, runlog, store

# Must run before FastMCP is constructed — see the module docstring.
runlog.init()

from mcp.server.fastmcp import FastMCP  # noqa: E402 — import order is deliberate

from . import deps  # noqa: E402 — import order is deliberate

log = runlog.get(__name__)

mcp = FastMCP("gsc-mcp")

_FIX_OAUTH_CLIENT = (
    "No OAuth client is configured. Set GSC_MCP_CLIENT_ID and "
    "GSC_MCP_CLIENT_SECRET, or install a release build."
)
_FIX_TOKEN = "Not signed in. Run the consent flow to authorise Search Console access."
_FIX_PROPERTIES = (
    "This account has no Search Console properties, or the token lacks "
    "the webmasters scope."
)


def _host_of_property(site_url: str) -> str:
    """The routable host for a Search Console property string.

    An `sc-domain:` property is not a URL — routing.host_of() runs it
    through urlparse, which reads the colon in "sc-domain:example.com" as
    a port separator and returns the nonsense host "sc-domain". Stripping
    the prefix first is what makes the result "example.com" instead.
    """
    domain = site_url.removeprefix("sc-domain:")
    return routing.host_of(domain)


def _auth_required() -> dict:
    return {"ok": False, "error": "auth_required", "fix": _FIX_TOKEN}


@mcp.tool()
def gsc_list_sites() -> list[dict] | dict:
    """List every Search Console property this account can see.

    Costs one Search Console API call (`sites.list`) — no per-property
    quota is spent. Persists each property into the local store (upserted
    by property URL) so other tools can route a page URL to its property
    without another network round trip; an existing row's sitemaps are
    left untouched here, since this call does not fetch or change them.

    Returns `[{"property", "host", "permission"}, ...]` sorted by
    property. On a missing, expired, or rejected token, returns
    `{"ok": False, "error": "auth_required", "fix": ...}` instead of
    raising; if no OAuth client is configured at all, returns
    `{"ok": False, "error": "not_configured", "fix": ...}` instead — in
    either case the caller can surface `fix` directly rather than parsing
    an exception.

    Does not fetch sitemaps, index status, or search analytics; see
    gsc_doctor, gsc_check_status, and gsc_performance for those. A
    property already known to the store keeps whatever sitemaps a prior
    gsc_submit_sitemaps() call recorded against it — this call never
    fetches or clears that list, so a routine refresh cannot erase it.
    """
    try:
        properties = api.list_properties(deps.provider())
    except deps.NotConfigured:
        log.info("gsc_list_sites: no OAuth client configured; "
                 "reporting not_configured")
        return {"ok": False, "error": "not_configured", "fix": _FIX_OAUTH_CLIENT}
    except gauth.AuthRequired:
        log.info("gsc_list_sites: no usable token; reporting auth_required")
        return _auth_required()

    sites: list[dict] = []
    with deps.connection() as conn:
        # Read what each property already has BEFORE upserting: the
        # upsert below always writes what we pass as sitemaps, so a prior
        # gsc_submit_sitemaps() result has to be looked up and carried
        # forward explicitly here or it is overwritten with [] on every
        # refresh — see the docstring above and gsc_submit_sitemaps (Task 9),
        # which relies on this list surviving a routine gsc_list_sites call.
        known_sitemaps = {site["property"]: site["sitemaps"]
                          for site in store.get_sites(conn)}
        for entry in properties:
            site_url = entry.get("siteUrl", "")
            permission = entry.get("permissionLevel")
            host = _host_of_property(site_url)
            sitemaps = known_sitemaps.get(site_url, [])
            store.upsert_site(conn, site_url, host, permission, sitemaps)
            sites.append({"property": site_url, "host": host,
                          "permission": permission})

    return sorted(sites, key=lambda site: site["property"])


def _check_oauth_client() -> dict:
    name = "oauth_client"
    try:
        deps.oauth_client()
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": _FIX_OAUTH_CLIENT}
    return {"name": name, "ok": True, "detail": "configured", "fix": ""}


def _check_token() -> dict:
    name = "token"
    try:
        token = gauth.load_token()
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": _FIX_TOKEN}
    if token is None:
        return {"name": name, "ok": False, "detail": "no token file",
                "fix": _FIX_TOKEN}
    return {"name": name, "ok": True, "detail": "token file present", "fix": ""}


def _check_config() -> dict:
    name = "config"
    try:
        problems = config.validate(config.load())
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": "Could not validate the configuration; see the log."}
    if problems:
        joined = "; ".join(problems)
        return {"name": name, "ok": False, "detail": joined, "fix": joined}
    return {"name": name, "ok": True, "detail": "config valid", "fix": ""}


def _check_store() -> dict:
    name = "store"
    try:
        with deps.connection() as conn:
            version = store.schema_version(conn)
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": f"Could not open the database ({type(exc).__name__})."}
    if version != store.SCHEMA_VERSION:
        fix = (f"Database schema is version {version}, expected "
               f"{store.SCHEMA_VERSION}. Delete {paths.db_path()} to rebuild.")
        return {"name": name, "ok": False,
                "detail": f"schema version {version}", "fix": fix}
    return {"name": name, "ok": True, "detail": f"schema version {version}",
            "fix": ""}


def _check_properties() -> dict:
    """The `properties` check's own network call also fails whenever the
    `oauth_client` check does — no client means no provider means no call —
    and that specific cause gets its own branch so the fix pointed at is
    "configure a client", not the generic "check your properties/scope"
    text, which would send a user with no client at all down the wrong
    path entirely.
    """
    name = "properties"
    try:
        properties = api.list_properties(deps.provider())
    except deps.NotConfigured as exc:
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": _FIX_OAUTH_CLIENT}
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": _FIX_PROPERTIES}
    if not properties:
        return {"name": name, "ok": False, "detail": "0 properties",
                "fix": _FIX_PROPERTIES}
    return {"name": name, "ok": True, "detail": f"{len(properties)} propert"
            f"{'y' if len(properties) == 1 else 'ies'}", "fix": ""}


@mcp.tool()
def gsc_doctor() -> dict:
    """Diagnose whether gsc-mcp is set up to talk to Search Console.

    Runs five checks in order — oauth_client, token, config, store,
    properties — and reports all of them even if one raises. A check that
    raises is recorded as `ok: False` with the exception's TYPE NAME only
    in `detail`; the message is never included, because it can carry a
    bearer token, a credentialed URL, or a raw response body. Every
    failing check carries a non-empty `fix` string with a concrete next
    step; this tool diagnoses, it does not repair anything itself.

    Costs at most one Search Console API call (`sites.list`, for the
    `properties` check) — zero network calls if an earlier check already
    shows the client or token is unusable and a caller stops before
    reaching it, though this implementation always runs all five.

    Returns `{"ok": bool, "checks": [{"name", "ok", "detail", "fix"}, ...]}`;
    `ok` is true only when every check passed.
    """
    checks = [
        _check_oauth_client(),
        _check_token(),
        _check_config(),
        _check_store(),
        _check_properties(),
    ]
    return {"ok": all(check["ok"] for check in checks), "checks": checks}


def _synced_property_urls(
    conn: sqlite3.Connection, active_provider: gauth.TokenProvider
) -> list[str]:
    """Property strings known to the store, syncing from the API first if
    the store has never been synced.

    A brand-new install's first gsc_check_status call must not fail with
    "no property matches" purely because gsc_list_sites has never run — so
    an empty store triggers the same sync-and-persist gsc_list_sites does,
    reusing the provider this call already obtained rather than asking for
    a second one. There is nothing to preserve on this path (an empty store
    has no prior sitemaps to carry forward, unlike gsc_list_sites' own
    refresh case), so each property is written with an empty sitemap list.
    """
    sites = store.get_sites(conn)
    if not sites:
        for entry in api.list_properties(active_provider):
            site_url = entry.get("siteUrl", "")
            permission = entry.get("permissionLevel")
            store.upsert_site(conn, site_url, _host_of_property(site_url),
                              permission, [])
        sites = store.get_sites(conn)
    return [site["property"] for site in sites]


@mcp.tool()
def gsc_check_status(urls: list[str], concurrency: int | None = None) -> dict:
    """Check whether each URL is indexed by Google Search, via the URL
    Inspection API.

    READ-ONLY: this tool inspects current index status and submits
    NOTHING. It never requests indexing and never spends a
    Request-Indexing slot — an assistant that wants a URL indexed must
    call gsc_request_indexing instead; confusing the two burns a scarce,
    unrecoverable Request-Indexing slot for nothing. This tool DOES spend
    URL Inspection quota, a separate per-property budget of 2,000 calls a
    day and roughly 600 a minute (see gsc_quota) — one call per URL
    inspected.

    `concurrency` defaults to the configured inspect_concurrency
    (config.load()["inspect_concurrency"]) when omitted.

    Properties come from the local store, the same one gsc_list_sites
    populates. If the store has never been synced — e.g. this is the very
    first call this install has ever made — it is synced automatically
    first, so a first-ever gsc_check_status call does not fail with "no
    property matches" purely because nothing has been synced yet.

    Returns `{"rows": [...], "checked": int, "skipped_quota": [...],
    "quota": {...}}`. Each row is `{"url", "status", "detail",
    "unverified"}`. `status` is one of: indexed, crawled_not_indexed,
    discovered_not_indexed, unknown_to_google, redirect, noindex,
    duplicate, alternate_canonical, not_found, soft_404, blocked_robots,
    no_property, error. `no_property` means no Search Console property in
    this account covers that URL's host. `unverified` is True when a
    concurrent burst produced a suspect result (unknown_to_google or
    error) that a sequential re-check could not confirm before quota or
    time ran out — treat such a row as UNKNOWN, not as a confirmed
    "not indexed".

    On a missing, expired, or rejected token, returns `{"ok": False,
    "error": "auth_required", "fix": ...}` instead of raising; if no
    OAuth client is configured at all, returns `{"ok": False, "error":
    "not_configured", "fix": ...}`.
    """
    try:
        active_provider = deps.provider()
        # deps.provider() only CONSTRUCTS a TokenProvider — it never raises
        # AuthRequired itself; that only happens lazily, inside whichever
        # API call first needs a real access token (gauth.py's
        # TokenProvider._load()). Probing it here, before doing anything
        # else, is deliberate and load-bearing for two reasons:
        #
        # 1. The empty-store sync below calls api.list_properties(), which
        #    WOULD raise AuthRequired naturally from inside this try — but
        #    only on that one path. A populated store skips the sync
        #    entirely, so relying on that call alone leaves a signed-out
        #    user with a synced store no chance to hit this except clause.
        # 2. api.check_status() never raises AuthRequired at all: a 401 on
        #    any one URL is caught inside _safe_inspect() and turned into a
        #    plain "error" status row, by design (one bad row must not abort
        #    the batch — see api.py's module docstring). A fully signed-out
        #    caller would therefore get back a normal-shaped result full of
        #    fabricated per-URL "error" rows instead of the structured
        #    auth_required answer this tool promises above.
        #
        # One explicit probe up front answers both cases the same way,
        # before either the sync or the batch spends anything.
        active_provider.access_token()

        effective_concurrency = concurrency
        if effective_concurrency is None:
            effective_concurrency = config.load()["inspect_concurrency"]

        with deps.connection() as conn:
            properties = _synced_property_urls(conn, active_provider)
            return api.check_status(conn, urls, active_provider, properties,
                                    concurrency=effective_concurrency)
    except deps.NotConfigured:
        log.info("gsc_check_status: no OAuth client configured; "
                 "reporting not_configured")
        return {"ok": False, "error": "not_configured", "fix": _FIX_OAUTH_CLIENT}
    except gauth.AuthRequired:
        log.info("gsc_check_status: no usable token; reporting auth_required")
        return _auth_required()


def _submission_report(
    conn: sqlite3.Connection, property: str, property_slots: int,
    daily_reserve: int, moment: datetime,
) -> tuple[quota.QuotaVerdict, dict]:
    """The Request-Indexing submission budget for one property.

    quota.check() is kept as the single source of truth for the
    reserve-adjusted ceiling and its wait time — computing next_free_at a
    second time here via quota.next_free() would risk exactly the
    two-functions-disagree defect this project has already hit repeatedly
    (Plan 1 outcomes §2). The account argument is unused: gsc_quota reports
    property-level budgets only and never passes account_slots, so check()
    never consults account_used() for it.
    """
    verdict = quota.check(conn, "", property, property_slots=property_slots,
                          daily_reserve=daily_reserve, now=moment)
    raw_free = quota.free(conn, property, slots=property_slots, now=moment)
    spent = quota.used(conn, property, now=moment)
    return verdict, {
        "free": raw_free,
        "spendable_free": verdict.property_free,
        "used": spent,
        "slots": property_slots,
        "daily_reserve": daily_reserve,
        "next_free_at": store.utc_iso(verdict.next_free_at),
    }


def _quota_binding(verdict: quota.QuotaVerdict,
                   inspection: quota.InspectionVerdict) -> str | None:
    if not verdict.allowed:
        return "submission"
    if inspection.binding == "daily":
        return "inspection_daily"
    if inspection.binding == "minute":
        return "inspection_minute"
    return None


@mcp.tool()
def gsc_quota() -> list[dict]:
    """Report Request-Indexing and URL Inspection budget for every property
    the store currently knows about.

    Local-only: reads the store and the config file, makes no Search
    Console API call, and needs no OAuth token — safe to call at any time,
    including before signing in. An empty store (nothing synced yet via
    gsc_list_sites or gsc_check_status) returns [].

    One entry per property: `{"property", "submission", "inspection",
    "binding"}`.

    "submission" is the Request-Indexing slot budget: `{"free",
    "spendable_free", "used", "slots", "daily_reserve", "next_free_at"}`.
    `free` is the RAW free-slot count and ignores daily_reserve.
    `spendable_free` is computed against the RESERVE-ADJUSTED ceiling —
    max(0, (slots - daily_reserve) - used) — NOT simply `free` minus
    `daily_reserve`: that arithmetic breaks at the clamp (slots=11,
    daily_reserve=2, used=10 gives free=1, but spendable_free is 0, not
    -1). ACT ON spendable_free, NOT free: daily_reserve exists to hold
    slots back from every tool, and a caller that submits up to `free`
    instead will be refused once spendable_free runs out. `next_free_at`
    is an ISO-8601 string, or None when a slot is free right now — it is
    already computed against the reserve-adjusted ceiling too, so it can
    report a wait even while `free` (the raw count) is nonzero.

    "inspection" is the URL Inspection API budget: `{"daily_free",
    "minute_free", "daily_limit", "minute_limit"}` (2000/day, 600/minute,
    per property — the same quota gsc_check_status spends).

    "binding" names whichever budget is exhausted for that property right
    now — "submission" (the Request-Indexing ceiling, reserve applied),
    "inspection_daily", or "inspection_minute" — or None when every budget
    has headroom.
    """
    settings = config.load()
    property_slots = settings["property_slots"]
    daily_reserve = settings["daily_reserve"]
    moment = datetime.now(UTC)

    report: list[dict] = []
    with deps.connection() as conn:
        for site in store.get_sites(conn):
            property = site["property"]
            verdict, submission = _submission_report(
                conn, property, property_slots, daily_reserve, moment)
            inspection_verdict = quota.inspection_check(
                conn, property, wanted=1, now=moment)
            inspection = {
                "daily_free": inspection_verdict.daily_free,
                "minute_free": inspection_verdict.minute_free,
                "daily_limit": quota.DAILY_INSPECTION_LIMIT,
                "minute_limit": quota.MINUTE_INSPECTION_LIMIT,
            }
            report.append({
                "property": property,
                "submission": submission,
                "inspection": inspection,
                "binding": _quota_binding(verdict, inspection_verdict),
            })
    return report


def main() -> None:
    """Entry point for the `gsc-mcp` console script. Runs until the client
    disconnects; communicates over stdio per the MCP transport default."""
    mcp.run()


if __name__ == "__main__":
    main()
