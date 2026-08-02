"""The MCP server shell: tool registration, `gsc_list_sites`, `gsc_doctor`.

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

from gsc_core import api, config, gauth, paths, routing, runlog, store

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
    raising — the caller can surface `fix` directly rather than parsing an
    exception.

    Does not fetch sitemaps, index status, or search analytics; see
    gsc_doctor, gsc_check_status, and gsc_performance for those.
    """
    try:
        properties = api.list_properties(deps.provider())
    except gauth.AuthRequired:
        log.info("gsc_list_sites: no usable token; reporting auth_required")
        return _auth_required()

    sites: list[dict] = []
    with deps.connection() as conn:
        for entry in properties:
            site_url = entry.get("siteUrl", "")
            permission = entry.get("permissionLevel")
            host = _host_of_property(site_url)
            store.upsert_site(conn, site_url, host, permission, [])
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
    name = "properties"
    try:
        properties = api.list_properties(deps.provider())
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


def main() -> None:
    """Entry point for the `gsc-mcp` console script. Runs until the client
    disconnects; communicates over stdio per the MCP transport default."""
    mcp.run()


if __name__ == "__main__":
    main()
