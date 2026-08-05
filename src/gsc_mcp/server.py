"""The MCP server shell: tool registration — `gsc_list_sites`, `gsc_doctor`,
`gsc_check_status`, `gsc_quota`, `gsc_detect_browsers`, `gsc_setup`.

The last two keep their bodies in their own modules (`tools_browsers`,
`onboarding`) and appear here as a docstring and a one-line delegate: this
file is already at its size ceiling, and a tool's docstring is its entire
interface to a model, so the docstring is the part that belongs beside the
registration.

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
and gets the shared TokenProvider via `deps.provider()` — see deps.py's
docstring for why the connection must not be shared and why the provider
must be. Tools
return plain dicts/lists and never let an exception cross the MCP
boundary: a raised exception is far less useful to a calling model than a
structured `{"ok": False, "error": ..., "fix": ...}` it can act on.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from gsc_core import api, config, gauth, paths, perf, quota, routing, runlog, store

# Must run before FastMCP is constructed — see the module docstring.
runlog.init()

from mcp.server.fastmcp import FastMCP  # noqa: E402 — import order is deliberate

# noqa: E402 on the import below — import order is deliberate, see above.
from . import (deps, envelopes, jobs, onboarding, tools_audit,  # noqa: E402
               tools_browsers, tools_submit)
from . import __version__  # noqa: E402 — import order is deliberate, see above

log = runlog.get(__name__)

mcp = FastMCP("gsc-mcp")

# FastMCP takes no version argument, and the lowlevel Server it wraps falls
# back to importlib.metadata.version("mcp") when its own version is unset --
# so an unstamped server introduces itself with the SDK's version number.
# That is worse than a blank field: it is a plausible number that is not
# ours, and it is the number a bug report will quote. Assigning the
# attribute the SDK already reads is the only route FastMCP leaves open;
# test_server_identity.py pins the result at the handshake, so a future SDK
# that renames the attribute reddens there rather than shipping the wrong
# version silently.
mcp._mcp_server.version = __version__

# The refusal envelope and its remedy strings live in envelopes.py, shared
# with tools_audit and tools_submit — see that module for why they are not
# defined here any more. The tools below use them via `envelopes.`; nothing
# in this file re-exports them, so there is no second name for one string.
#
# api_error's third argument is the 403 remedy. Every tool here that can
# raise ApiError takes no `site`, so a 403 cannot mean "wrong property" —
# it means no properties or a missing scope, hence FIX_PROPERTIES.


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
    return {"ok": False, "error": "auth_required", "fix": envelopes.FIX_TOKEN}


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
    an exception. A Search Console error returns `{"ok": False, "error":
    "api_error", "status": <http status>, "fix": ...}`, and anything else
    `{"ok": False, "error": "unexpected", "detail": <exception type>,
    "fix": ...}`. An EMPTY LIST therefore means what it says — this
    account really has no properties — and never a call that was refused.

    Does not fetch sitemaps, index status, or search analytics; see
    gsc_doctor, gsc_check_status, and gsc_performance for those. A
    property already known to the store keeps whatever sitemaps a prior
    gsc_submit_sitemaps() call recorded against it — this call never
    fetches or clears that list, so a routine refresh cannot erase it.
    """
    try:
        properties = api.list_properties(deps.provider())

        sites: list[dict] = []
        with deps.connection() as conn:
            # Read what each property already has BEFORE upserting: the
            # upsert below always writes what we pass as sitemaps, so a prior
            # gsc_submit_sitemaps() result has to be looked up and carried
            # forward explicitly here or it is overwritten with [] on every
            # refresh — see the docstring above and gsc_submit_sitemaps
            # (Task 9), which relies on this list surviving a routine
            # gsc_list_sites call.
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
    except deps.NotConfigured:
        log.info("gsc_list_sites: no OAuth client configured; "
                 "reporting not_configured")
        return {"ok": False, "error": "not_configured",
                "fix": envelopes.FIX_OAUTH_CLIENT}
    except gauth.AuthRequired:
        log.info("gsc_list_sites: no usable token; reporting auth_required")
        return _auth_required()
    except api.ApiError as exc:
        return envelopes.api_error("gsc_list_sites", exc, envelopes.FIX_PROPERTIES)
    except Exception as exc:  # noqa: BLE001 — see _unexpected
        return envelopes.unexpected("gsc_list_sites", exc)

    return sorted(sites, key=lambda site: site["property"])


def _check_oauth_client() -> dict:
    name = "oauth_client"
    try:
        deps.oauth_client()
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": envelopes.FIX_OAUTH_CLIENT}
    return {"name": name, "ok": True, "detail": "configured", "fix": ""}


def _check_token() -> dict:
    name = "token"
    try:
        token = gauth.load_token()
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": envelopes.FIX_TOKEN}
    if token is None:
        return {"name": name, "ok": False, "detail": "no token file",
                "fix": envelopes.FIX_TOKEN}
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
    if version > store.SCHEMA_VERSION:
        # A newer gsc-mcp opened this database. store.connect() leaves the
        # stamp alone rather than downgrading it, and the fix is to run the
        # newer build -- NOT to delete anything. The old advice was to
        # rebuild, which on this branch would have thrown away the quota
        # ledger over a version stamp that is one migration ahead.
        fix = (f"Database schema is version {version}, newer than this "
               f"build expects ({store.SCHEMA_VERSION}). Update gsc-mcp, or "
               "restart it if you have just upgraded. Do not delete the "
               "database: it holds the quota ledger.")
        return {"name": name, "ok": False,
                "detail": f"schema version {version}", "fix": fix}
    if version < store.SCHEMA_VERSION:
        fix = (f"Database schema is version {version}, older than expected "
               f"({store.SCHEMA_VERSION}), and did not migrate on open. "
               f"Delete {paths.db_path()} to rebuild.")
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
                "fix": envelopes.FIX_OAUTH_CLIENT}
    except gauth.AuthRequired as exc:
        # Distinct from the generic branch below: "the token was rejected"
        # and "you may be missing a scope" have different next steps, and
        # api.list_properties now separates the two rather than reporting
        # a refused call as an account with no properties.
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": envelopes.FIX_TOKEN}
    except Exception as exc:  # noqa: BLE001 — see gsc_doctor docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return {"name": name, "ok": False, "detail": type(exc).__name__,
                "fix": envelopes.FIX_PROPERTIES}
    if not properties:
        return {"name": name, "ok": False, "detail": "0 properties",
                "fix": envelopes.FIX_PROPERTIES}
    return {"name": name, "ok": True, "detail": f"{len(properties)} propert"
            f"{'y' if len(properties) == 1 else 'ies'}", "fix": ""}


@mcp.tool()
def gsc_doctor() -> dict:
    """Diagnose whether gsc-mcp is set up to talk to Search Console.

    Runs seven checks in order — oauth_client, token, config, store,
    properties, browser, extension — and reports all of them even if one
    raises. A check that raises is recorded as `ok: False` with the
    exception's TYPE NAME only in `detail`; the message is never included,
    because it can carry a bearer token, a credentialed URL, a raw response
    body, or a filesystem path holding your account name. Every failing
    check carries a non-empty `fix` string with a concrete next step; this
    tool diagnoses, it does not repair anything itself.

    The last two are the local setup for browser-driven submission, and
    they come last because they cost nothing and the first five establish
    whether anything works at all. `browser` names the profile to use;
    `extension` reports whether the bridge extension is REGISTERED in that
    profile — a green check does not mean its background worker is
    running, which needs a live connection and arrives with Milestone 3B.
    "Could not be checked" is reported as such, never as "not installed".

    Costs at most one Search Console API call (`sites.list`, for the
    `properties` check); the browser and extension checks are local file
    reads and make none.

    Returns `{"ok": bool, "checks": [{"name", "ok", "detail", "fix"}, ...]}`;
    `ok` is true only when every check passed.
    """
    checks = [
        _check_oauth_client(),
        _check_token(),
        _check_config(),
        _check_store(),
        _check_properties(),
        onboarding.check_browser(),
        onboarding.check_extension(),
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
    Request-Indexing slot. No tool here does yet — requesting indexing is
    not built, so an assistant asked to get a URL indexed should say so
    rather than reaching for this one, which answers a different question
    and spends a different budget doing it. This tool DOES spend
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
    "quota": {...}}`. "quota" is per property, and its
    `daily_free_at_gate`/`minute_free_at_gate` are the headroom measured
    BEFORE this call reserved its own budget — they are a record of what
    the gate saw, not current headroom, and are already stale by the size
    of this batch by the time you read them. gsc_quota's similarly-shaped
    `daily_free`/`minute_free` are the ones measured now; do not compare
    the two pairs or plan a second batch against these. `binding_at_gate`
    (here and on each "skipped_quota" entry) carries the suffix for the
    same reason: it names only the exhausted INSPECTION window as seen at
    the gate, where gsc_quota's `binding` covers the submission budget too
    and is read now. Each row is
    `{"url", "status", "detail",
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
        return {"ok": False, "error": "not_configured",
                "fix": envelopes.FIX_OAUTH_CLIENT}
    except gauth.AuthRequired:
        log.info("gsc_check_status: no usable token; reporting auth_required")
        return _auth_required()
    except api.ApiError as exc:
        return envelopes.api_error("gsc_check_status", exc, envelopes.FIX_PROPERTIES)
    except Exception as exc:  # noqa: BLE001 — see _unexpected
        # The eager probe above raises a plain RuntimeError when a stored
        # token is expired AND Google's token endpoint is transiently
        # failing — neither NotConfigured nor AuthRequired. Without this
        # branch that leaves the tool via FastMCP as an isError with no
        # structured fix attached.
        return envelopes.unexpected("gsc_check_status", exc)


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
def gsc_quota() -> list[dict] | dict:
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
    try:
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
    except Exception as exc:  # noqa: BLE001 — see _unexpected
        return envelopes.unexpected("gsc_quota", exc)
    return report


def _final_data_state_warning() -> str:
    return (
        f"data_state='final' silently omits the most recent "
        f"~{perf.FINAL_LAG_DAYS} day(s) of data -- Google has not finished "
        f"processing them yet, and nothing in the response marks the gap. "
        f"The Search Console web UI has no such restriction and shows those "
        f"days immediately, so a 'final' query compared against what a human "
        f"sees in the UI right now will look like a discrepancy or a missed "
        f"traffic change when both are simply answering different questions. "
        f"Prefer data_state='all' (the default here) unless you specifically "
        f"need finalized-only rows; its trade-off is that the last day or two "
        f"may still revise upward on a later query."
    )


@mcp.tool()
def gsc_performance(
    site: str | None = None, days: int = 28, dim: str | None = None,
    limit: int = 25, start_date: str | None = None, end_date: str | None = None,
    data_state: str = "all", search_type: str = "web",
) -> dict:
    """Search Analytics performance: clicks, impressions, ctr, position.

    Three shapes, chosen by what is passed:
    - No `site` -> one row per property the store knows about (`scope:
      "portfolio"`, `sites`, `totals` aggregated across all of them).
    - `site`, no `dim` -> a single aggregate for that site (`scope: "site"`,
      plus `clicks`/`impressions`/`ctr`/`position`).
    - `site` and `dim` -> per-`dim` rows for that site, sorted by clicks
      descending (`scope: <dim>`, `rows`, `totals` aggregated across `rows`).
    `dim` must be one of perf.VALID_DIMENSIONS ("query", "page", "country",
    "device", "date", "searchAppearance").

    Date window: pass `start_date` AND `end_date` (both "YYYY-MM-DD") for an
    explicit range, or leave both unset and get the trailing `days` calendar
    days ending yesterday. `start_date` without `end_date` is refused
    outright -- `{"ok": False, "note": "start_date needs end_date (both
    YYYY-MM-DD)"}` -- rather than guessing an end.

    IMPORTANT -- `data_state` defaults to "all", not Google's own API default
    of "final". Passing `data_state="final"` attaches a `warning` string
    explaining why: data_state='final' silently omits the most recent
    ~perf.FINAL_LAG_DAYS (3) day(s) of data -- Google has not finished
    processing them yet, and nothing in the response marks the gap. The
    Search Console web UI has no such restriction and shows those days
    immediately, so a 'final' query compared against what a human sees in
    the UI right now will look like a discrepancy or a missed traffic
    change when both are simply answering different questions. Prefer
    data_state='all' (the default here) unless you specifically need
    finalized-only rows; its trade-off is that the last day or two may
    still revise upward on a later query.

    On a missing, expired, or rejected token, returns `{"ok": False, "error":
    "auth_required", "fix": ...}`; if no OAuth client is configured at all,
    `{"ok": False, "error": "not_configured", "fix": ...}`. Any other failure
    -- a bad dimension, an unroutable site, a Search Console API error --
    comes back as `{"ok": False, "start", "end", "note": str(exc)}` rather
    than raising.
    """
    if start_date and not end_date:
        return {"ok": False, "note": "start_date needs end_date (both YYYY-MM-DD)"}

    try:
        if start_date and end_date:
            window_start, window_end = start_date, end_date
        else:
            window_start, window_end = perf.date_range(days, end=end_date)
    except ValueError as exc:
        return {"ok": False, "note": str(exc)}

    window = {"start": window_start, "end": window_end}
    result: dict = dict(window)
    if data_state == "final":
        result["warning"] = _final_data_state_warning()

    try:
        active_provider = deps.provider()
        with deps.connection() as conn:
            properties = [row["property"] for row in store.get_sites(conn)]

            if site is None:
                sites_report = perf.portfolio(
                    properties, active_provider, days=days,
                    start_date=start_date, end_date=end_date,
                    data_state=data_state, search_type=search_type)
                result["scope"] = "portfolio"
                result["sites"] = sites_report
                result["totals"] = perf.aggregate_rows(sites_report)
            elif dim is None:
                site_totals = perf.totals(
                    site, properties, active_provider, days=days,
                    start_date=start_date, end_date=end_date,
                    data_state=data_state, search_type=search_type)
                result["scope"] = "site"
                result.update(site_totals)
            else:
                rows = perf.query(
                    site, properties, active_provider, days=days,
                    dimensions=[dim], start_date=start_date, end_date=end_date,
                    data_state=data_state, search_type=search_type, limit=limit)
                rows.sort(key=lambda row: row["clicks"], reverse=True)
                result["scope"] = dim
                result["rows"] = rows
                result["totals"] = perf.aggregate_rows(rows)
    except deps.NotConfigured:
        log.info("gsc_performance: no OAuth client configured; "
                 "reporting not_configured")
        return {"ok": False, "error": "not_configured",
                "fix": envelopes.FIX_OAUTH_CLIENT}
    except gauth.AuthRequired:
        log.info("gsc_performance: no usable token; reporting auth_required")
        return _auth_required()
    except (perf.PerfError, ValueError) as exc:
        return {"ok": False, **window, "note": str(exc)}
    except Exception as exc:  # noqa: BLE001 — see _unexpected
        return {**envelopes.unexpected("gsc_performance", exc), **window}

    return result


def _merge_sitemaps(existing: list[str], newly_submitted: list[str]) -> list[str]:
    """Append newly succeeded sitemaps onto what a property already had,
    without duplicating one already recorded."""
    merged = list(existing)
    for sitemap_url in newly_submitted:
        if sitemap_url not in merged:
            merged.append(sitemap_url)
    return merged


def _record_submitted(conn: sqlite3.Connection, snapshot: list[dict],
                      succeeded: dict[str, list[str]]) -> None:
    """Merge newly-submitted sitemaps into `sites`, re-reading CURRENT state.

    The caller read its `snapshot` before making N slow network calls.
    Merging against that snapshot is a read-modify-write with the whole
    round trip sitting inside it, and upsert_site REPLACES the sitemaps
    column outright — so a concurrent gsc_submit_sitemaps that recorded a
    sitemap in the meantime has its row silently overwritten. The lost
    sitemap is one Google really did accept, and because a bare
    gsc_submit_sitemaps() resubmits only what the store knows about, it is
    never resubmitted again either: permanently absent, with nothing
    anywhere reporting a failure.

    Re-reading INSIDE the transaction is what closes it. store.tx() issues
    BEGIN IMMEDIATE, so the write lock is held before the read happens and
    no other writer can commit between the read and the upserts — the
    merge is against current state, not against a pre-network snapshot.

    A property missing from the re-read (deleted between the two) falls
    back to the snapshot's host/permission rather than dropping the
    submission: re-creating a row for a sitemap Google accepted is the
    recoverable direction; forgetting it is not.
    """
    with store.tx(conn):
        current = {site["property"]: site for site in store.get_sites(conn)}
        fallback = {site["property"]: site for site in snapshot}
        for property, newly_submitted in succeeded.items():
            site = current.get(property) or fallback[property]
            store.upsert_site(
                conn, property, site["host"], site["permission"],
                _merge_sitemaps(site["sitemaps"], newly_submitted))


def _persist_or_log(conn: sqlite3.Connection, snapshot: list[dict],
                    succeeded: dict[str, list[str]]) -> None:
    """_record_submitted on the way out of a failure, never masking it.

    Used only while an exception is already propagating. If the write also
    fails there is nothing useful left to do -- raising here would replace
    the exception that explains what actually went wrong with a second one
    about the cleanup -- so it is logged and the original propagates. Only
    the failure's TYPE NAME is logged, per this module's rule.
    """
    if not succeeded:
        return
    try:
        _record_submitted(conn, snapshot, succeeded)
    except Exception as exc:  # noqa: BLE001 — must not mask the original
        log.warning(
            "gsc_submit_sitemaps: %d sitemap(s) reached Google but could not "
            "be recorded (%s); a later bare call will not resubmit them",
            sum(len(urls) for urls in succeeded.values()), type(exc).__name__)


def _sitemap_targets(
    sitemaps: list[str] | None, sites: list[dict], properties: list[str],
) -> list[tuple[str | None, str]]:
    """(property, sitemap_url) pairs to submit.

    An explicit `sitemaps` list is routed against the store's known
    properties one URL at a time -- a URL that matches nothing gets a `None`
    property, reported back as a failed row rather than dropped silently.
    Omitted, this falls back to whatever is already recorded per property in
    the store, which is the ONLY case that touches `store.sites.sitemaps`
    directly rather than routing a caller-supplied list.
    """
    if sitemaps is not None:
        return [(routing.resolve_property(url, properties), url) for url in sitemaps]
    return [(site["property"], sitemap_url)
            for site in sites for sitemap_url in site["sitemaps"]]


@mcp.tool()
def gsc_submit_sitemaps(sitemaps: list[str] | None = None) -> dict | list[dict]:
    """Submit one or more sitemaps to Search Console (PUT, idempotent --
    safe to resubmit an already-known sitemap).

    `sitemaps` is an optional list of sitemap URLs; each is routed to its
    covering property via the same host-matching gsc_check_status
    uses. Omit it to resubmit every sitemap already on
    record for every property (the same list gsc_list_sites carries
    forward on refresh).

    REFUSES TO GUESS: when `sitemaps` is omitted and the store has no
    sitemap recorded for any property, this returns `{"ok": False, "note":
    "No sitemaps known. Pass sitemaps=[...] explicitly."}` rather than
    trying `/sitemap.xml` -- submitting a URL nobody named is an
    outward-facing action against the caller's Search Console property, and
    a wrong guess leaves a permanent failed submission in their sitemap
    list for no reason.

    Returns a list of `{"site", "sitemap", "http_status", "ok", "note"}`
    (api.submit_sitemap's shape) -- one entry per sitemap attempted, plus one
    `{"site": None, "sitemap", "http_status": None, "ok": False, "note"}`
    entry for any URL that matched no known property. Every successful
    submission is recorded back onto its property's row (merged with,
    never replacing, whatever sitemaps were already there), so a later bare
    call resubmits it too.

    On a missing, expired, or rejected token, returns `{"ok": False,
    "error": "auth_required", "fix": ...}` instead of raising, once real
    work is about to start (nothing has been submitted yet at that point);
    if no OAuth client is configured at all, `{"ok": False, "error":
    "not_configured", "fix": ...}`. Any other failure comes back as
    `{"ok": False, "error": "unexpected", "detail": <exception type>,
    "fix": ...}`.
    """
    try:
        return _submit_sitemaps(sitemaps)
    except Exception as exc:  # noqa: BLE001 — see _unexpected
        return envelopes.unexpected("gsc_submit_sitemaps", exc)


def _submit_sitemaps(sitemaps: list[str] | None) -> dict | list[dict]:
    """gsc_submit_sitemaps' body, split out so the tool above is nothing but
    the {ok, error, fix} guard — the loop below has real work to do and
    wrapping it in place buried that work two levels deep in a try."""
    with deps.connection() as conn:
        sites = store.get_sites(conn)
        properties = [site["property"] for site in sites]
        targets = _sitemap_targets(sitemaps, sites, properties)

        if not targets:
            return {"ok": False,
                    "note": "No sitemaps known. Pass sitemaps=[...] explicitly."}

        try:
            active_provider = deps.provider()
        except deps.NotConfigured:
            log.info("gsc_submit_sitemaps: no OAuth client configured; "
                     "reporting not_configured")
            return {"ok": False, "error": "not_configured",
                "fix": envelopes.FIX_OAUTH_CLIENT}

        results: list[dict] = []
        succeeded: dict[str, list[str]] = {}
        auth_failed = False
        try:
            for property, sitemap_url in targets:
                if property is None:
                    results.append({
                        "site": None, "sitemap": sitemap_url,
                        "http_status": None, "ok": False,
                        "note": "no Search Console property matches this URL",
                    })
                    continue
                try:
                    outcome = api.submit_sitemap(property, sitemap_url,
                                                 active_provider)
                except gauth.AuthRequired:
                    # A token that dies mid-loop (rather than at the very
                    # first call) must not un-submit the sitemaps that
                    # already reached Google -- stop here, persist below
                    # what already succeeded, then report auth_required
                    # rather than silently discarding real submissions.
                    auth_failed = True
                    break
                results.append(outcome)
                if outcome["ok"]:
                    succeeded.setdefault(property, []).append(sitemap_url)
        except BaseException:
            # AuthRequired is caught in the loop because it decides the
            # ANSWER as well as ending the run. Every OTHER failure --
            # a requests transport error, or the plain RuntimeError gauth
            # raises when Google's token endpoint is transiently down --
            # used to escape here and skip the persist below entirely, so a
            # sitemap Google had already accepted was never recorded. The
            # loss predates the {ok, error, fix} guard on this tool; what
            # the guard changed is that it turned a loud isError into an
            # inert-looking {"ok": false, "error": "unexpected"} over a
            # store the caller has no reason to re-check.
            #
            # BaseException, not Exception, for store.tx's stated reason: a
            # disconnecting MCP client raises CancelledError and a CLI
            # interrupt raises KeyboardInterrupt, and neither is a good
            # reason to forget a live sitemap.
            _persist_or_log(conn, sites, succeeded)
            raise

        if succeeded:
            _record_submitted(conn, sites, succeeded)

        if auth_failed:
            log.info("gsc_submit_sitemaps: no usable token; reporting "
                     "auth_required")
            return _auth_required()

        return results


@mcp.tool()
def gsc_detect_browsers() -> dict:
    """List the Chromium browser profiles on this machine and recommend one.

    Local-only: reads the browsers' own state files, makes no network call,
    spends no quota, and needs no token — safe to call before signing in.
    Answers "which browser profile should I drive?", nothing else; it opens
    no browser and changes no setting.

    PRIVACY: no email address is returned, in either direction. Not the
    account signed in to a profile, not the account that authorised this
    server, and not a profile display name that is itself an address. No
    filesystem path is returned either — a profile path carries the
    operator's account name. What identifies a profile here is its browser
    and its profile directory.

    Returns `{"ok": True, "profiles": [...], "recommended": <one of them or
    None>, "reasons": [...]}`. `profiles` is a FLAT list across every
    browser, ranked-flag included, not grouped by browser — the question is
    which single profile to use, so each entry carries its own brand
    context (`browser`, `browser_key`, `extensions_url`) and stands alone.
    Each profile is `{"browser", "browser_key", "extensions_url",
    "profile", "display_name", "signed_in", "account_discoverable",
    "matches_authorised_account", "has_extension", "recommended"}`.

    `extensions_url` is where that browser's extensions page lives
    (`chrome://extensions`, `brave://extensions`, ...). Use the value given;
    do not build one from the browser key, since Chromium registers no
    chromium:// scheme and uses chrome://extensions.

    `has_extension` is TRI-STATE: true means the pairing extension is
    installed in that profile, false means every preferences file was read
    and it was not among them, and null means the check could not be
    PERFORMED — an unreadable preferences file, or no unpacked extension
    directory to match against. Read null as "not detected", never as "not
    installed"; telling a user with a working install to reinstall it is
    the one wrong answer this flag exists to avoid.

    `signed_in` says a Google account was found in that profile's files.
    `account_discoverable` is a fact about the BRAND: Brave, Vivaldi, Opera
    and plain Chromium record no Google account at all, so `signed_in:
    false` there means "not discoverable", NOT "nobody is signed in".
    `matches_authorised_account` is true, false, or NULL — null means the
    question could not be asked (nothing has authorised yet, or the brand
    records nothing), and must not be read as "no". `reasons` explains the
    recommendation in plain sentences.

    A machine with no Chromium browser installed returns `ok: true` with an
    empty `profiles` list, `recommended: null`, and a `note` saying what to
    install; that is an ordinary state, not a failure. Only an unexpected
    fault returns `{"ok": False, "error": "unexpected", "detail":
    <exception type>, "fix": ...}`.
    """
    return tools_browsers.detect_browsers()


@mcp.tool()
def gsc_use_browser(browser: str | None = None, profile: str | None = None,
                   clear: bool = False) -> dict:
    """Choose which browser profile this server drives, overriding detection.

    Local-only: writes one preference to this server's config file, makes
    no network call, spends no quota, needs no token, and opens no browser.
    Use it when gsc_detect_browsers recommends a profile that is not the
    one holding your Search Console account — the detector can rank the
    profiles it finds, but it cannot know which browser you work in.

    `browser` is the `browser_key` from gsc_detect_browsers ("chrome",
    "brave", "edge", "vivaldi", "opera", "chromium") — the key, not the
    display label. `profile` is the `profile` field from the same entry
    (the profile DIRECTORY, e.g. "Default" or "Profile 3"); omit it to take
    that browser's default profile. `clear=True` removes the pin and
    returns to the detector's recommendation, and ignores the other two.

    The pair is checked against the profiles that actually exist BEFORE it
    is saved. A pair that matches nothing is refused with `{"ok": False,
    "error": "browser_not_found", ...}` whose `fix` lists the pairs that
    would work, so a typo is answered immediately rather than becoming a
    browser that silently never opens.

    Once pinned, the choice is absolute: gsc_setup, gsc_doctor and every
    tool that drives the browser use it, and the ranking is not consulted.
    If the pinned profile later disappears — browser uninstalled, profile
    deleted — nothing falls back to a different one. Every affected tool
    stops and says the pin is dangling, because driving the wrong profile
    would submit URLs from whichever account happens to be signed in there.

    The pin survives restarts. Returns `{"ok": True, "pinned": "<browser> /
    <profile>" or null, "note": ...}`.
    """
    return tools_browsers.use_browser(browser=browser, profile=profile,
                                      clear=clear)


@mcp.tool()
def gsc_setup(open_browser: bool = True) -> dict:
    """Set this server up, one step at a time. Call it, do what it says,
    call it again — repeat until `ok` is true.

    IDEMPOTENT and SAFE TO CALL REPEATEDLY. It spends no Search Console
    indexing quota and makes at most one API call (verifying the stored
    sign-in still works). Every call re-reports the whole state from
    scratch, so there is no session to resume and no order to get wrong: if
    you have lost track of where setup got to, just call it again.

    There are four steps, checked in order: `oauth_client` (credentials to
    sign in with), `consent` (the user approves Google's consent screen),
    `browser` (a Chromium browser with a profile exists), `extension` (the
    gsc-mcp bridge extension is loaded in the profile this server
    recommends). The FIRST unsatisfied step is returned as `next` and the
    call stops there — later steps are meaningless until it is done.

    Returns `{"ok": bool, "done": [step], "pending": [step], "next":
    {"step", "action", "url"?, "path"?} | None}`. `ok` is true, and `next`
    is null, only when all four steps are satisfied. `next.action` is a
    plain-English instruction to relay to the user. `next.url`, when
    present, is the Google consent URL to open. `next.path`, when present,
    is the folder to choose in the browser's "Load unpacked" dialog.

    `open_browser` (default true) opens the consent URL in the user's
    default browser when a NEW consent is started. A repeat call while one
    is already pending returns the SAME url and opens nothing — the pending
    consent screen is the only one whose redirect will be accepted, so
    never assume a second call means a second link.

    PRIVACY: no email address, no token field, no PKCE verifier and no
    authorization code is ever returned. The only filesystem path returned
    is `next.path`, which is this server's own extension directory.

    Never raises. An unexpected fault comes back as `next.step:
    "unexpected"` with an action to retry.
    """
    return onboarding.setup(open_browser)


@mcp.tool()
def gsc_request_indexing(urls: list[str]) -> dict:
    """Submit up to five URLs to Google's Request Indexing, one at a time,
    through the browser extension in your own signed-in profile.

    BLOCKING and slow by design: submissions are paced 130-180 seconds
    apart, so five URLs can take fifteen minutes. Use
    gsc_start_indexing_job for anything larger.

    Quota is per property — roughly eleven slots per property on a rolling
    24-hour window, and properties are independent. Call gsc_quota first to
    see what is spendable; act on `spendable_free`, not `free`.

    Returns `{"ok", "submitted", "skipped", "failed", "stopped_early",
    "stop_reason", "notes", "results"}`, with one entry per URL in
    `results`. A run stops early on the first throttle, captcha, or
    signed-out session rather than burning the rest of the batch against a
    refusing server. A refusal is `{"ok": False, "error", "detail", "fix"}`.
    """
    return tools_submit.request_indexing(urls)


@mcp.tool()
def gsc_start_indexing_job(urls: list[str]) -> dict:
    """Queue a background submission run over any number of URLs.

    Returns immediately with `{"ok", "job_id", "total", "note"}`. Poll
    gsc_job_status for progress and gsc_stop_job to end it early. One
    submission job runs at a time: the bridge drives a single browser tab
    in your real profile, so a second job is refused rather than queued.
    """
    return tools_submit.start_indexing_job(urls)


@mcp.tool()
def gsc_job_status(job_id: str | None = None) -> dict:
    """Progress and state for one submission job, or the most recent one.

    States: pending, running, completed, stopped_user, stopped_throttled,
    failed. `results` holds one entry per URL attempted so far, and `live`
    says whether a worker is still on it in this process.

    `stop_reason` says why a run ended early — "quota_exceeded", "no_quota",
    "stopped_by_user". It is the only trace of a run refused at the gate,
    which attempts nothing and so leaves `results` and `error` both empty.
    """
    return tools_submit.job_status(job_id)


@mcp.tool()
def gsc_stop_job(job_id: str) -> dict:
    """Ask a running submission job to stop.

    It stops after the URL currently in flight, never mid-URL: a
    submission already sent has spent its quota slot and its ledger row
    must settle with the real outcome.
    """
    return tools_submit.stop_job(job_id)


@mcp.tool()
def gsc_find_unindexed(site: str, source: str = "both",
                       limit: int | None = None) -> dict:
    """Which of a property's URLs are not in Google's index, and why.

    `source` chooses where candidate URLs come from: "sitemap" fetches and
    parses the property's registered sitemaps fresh, "store" uses only
    URLs already seen, "both" (the default) unions them.

    `limit` caps how many URLs are INSPECTED, not how many are returned —
    inspection spends a daily budget, so a cap that only trimmed the
    output would pay full price for an answer it threw away. Which URLs a
    capped run reaches follows the store's url ordering (alphabetical),
    not staleness: a capped run is a sample, not a worst-first sweep. The
    result reports `candidates_total`, `inspected` and `limited` so you
    can tell a truncated answer from a complete one.

    `limit` is not the only thing that can cut a run short. Inspection
    quota is per property and roughly eleven slots a day, so on any site
    larger than that the run reaches the gate and stops: `inspected` is
    what was handed to the API, `checked` is what actually reached it,
    and `skipped_quota` lists the URLs accounting for the difference.
    Report both numbers rather than `inspected` alone — a run that
    answered for three of forty URLs is not a survey of the property, and
    the remaining URLs are answerable tomorrow at no extra cost.

    Only URLs whose last inspection is older than `inspection_ttl_days`
    are re-inspected; the rest are reported from their stored status. A
    second call the same day therefore costs no budget and still answers
    in full. `"fresh": true` on a row means only that THIS run did not
    inspect it — usually because it was within the TTL, but also when
    `limit` cut the run short before reaching it. It is not a promise
    that the stored status is within the TTL.

    Each unindexed row carries `reason` (one of ten codes), `action`,
    `submitting_helps` and `needs_site_access`. Act on
    `submitting_helps` before calling gsc_request_indexing: submitting a
    404, a redirect, a noindex, or a page Google crawled and declined
    wastes an unrecoverable quota slot.

    URLs whose state this run did not establish are in `undetermined`,
    never in `unindexed`. Read each one's `status` before wording the
    answer: it separates two cases a reader acts on differently. "We
    looked and could not tell" covers a failed inspection and a result
    the burst re-verify pass could not confirm. `"skipped_quota"` is the
    other case and means the opposite: we never got to look, because the
    property's daily budget ran out first. That is not a problem with the
    URL, needs no investigation, and is answered by running again
    tomorrow — say so rather than reporting it as a fault.

    A refusal is `{"ok": false, "error": <code>, "fix": <what to do>}`,
    plus `status` when Search Console refused the call and `detail` (an
    exception type name) when the failure was unexpected.
    """
    return tools_audit.find_unindexed(site, source, limit)


@mcp.tool()
def gsc_audit(site: str) -> dict:
    """The current indexation position for one property, as structured data.

    Reads the local store only: no network call, no quota spent. It
    reports what the last inspection found, which is why the payload
    carries `as_at` — run gsc_find_unindexed first if the picture is
    stale, and check the `stale` count to see how much of it is.

    Point-in-time by design. There are no movement numbers — nothing
    "moved to indexed", nothing "de-indexed" — because the store keeps no
    status history, and a zero in a field like that would read as a
    measurement that found no change rather than as an absence of data.
    `basis` says so in the payload.

    Returns counts (`total_known`, `checked`, `indexed`, `unindexed`,
    `undetermined`, `never_checked`, `stale`), `indexed_pct` (null when
    nothing has been checked), a `by_reason` histogram over the ten reason
    codes, and three action counts: `submittable`, `needs_site_access` and
    `no_action_needed`. Every reason code is in exactly one of the three,
    so they sum to `unindexed`. `submittable` counts URLs a quota slot can
    move, not URLs to submit right now: it includes `crawled-not-indexed`,
    usually the largest bucket, where Google has already fetched the page
    and passed — a re-crawl can reverse that, but only after the page
    changes. Read each row's `action` before spending slots. Rendering all
    this — prose, table, chart — is yours to do.
    """
    return tools_audit.audit(site)


def _reconcile_at_startup() -> None:
    """Close out what a crash or restart left dangling.

    A job row marked pending or running implies a live worker thread inside
    this process; after a restart there is none, so it would sit unsettled
    forever. Open submission rows are swept by store.reconcile() on the
    same principle — each one holds a quota slot that never settles.
    """
    try:
        with store.session() as conn:
            store.reconcile_jobs(conn)
            store.reconcile(conn)
    except Exception as exc:  # noqa: BLE001 — never block startup
        # Type name only: an unauthored message can carry a filesystem path
        # holding the operator's account name, and log files get shipped.
        log.warning("startup reconciliation skipped (%s)", type(exc).__name__)


def main() -> None:
    """Entry point for the `gsc-mcp` console script. Runs until the client
    disconnects; communicates over stdio per the MCP transport default.
    The exit is bounded, not abrupt — see jobs.shutdown."""
    _reconcile_at_startup()
    try:
        mcp.run()
    finally:
        jobs.shutdown(jobs.SHUTDOWN_TIMEOUT)


if __name__ == "__main__":
    main()
