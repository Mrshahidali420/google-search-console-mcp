"""Tool bodies for discovery and audit. Never raise: every path returns a dict.

These live here rather than in server.py because server.py is already at
its accepted size ceiling, following the tools_submit.py precedent.

PRIVACY. Everything returned from here leaves the process: an MCP result
is consumed by a model, rendered into a transcript, and retained by
whatever client is driving the server. Property URLs and the user's own
page URLs belong in a result -- they are what the tool is for, and
gsc_list_sites already returns them. Exception messages do not: a failure
is reported by TYPE NAME only, because a message can carry a filesystem
path containing the operator's account name, a credentialed URL, or a
truncated response body. An OSError out of the store is the everyday case,
not a hypothetical one.
"""
from __future__ import annotations

from gsc_core import api, audit as audit_core, config, discovery, gauth, runlog

from . import deps, envelopes

log = runlog.get(__name__)

# Only the strings unique to these two tools live here. The rest — and the
# envelope helpers that were copied into this file — are in envelopes.py.
_FIX_BAD_SOURCE = ('source must be one of "sitemap", "store", or "both" '
                   "(the default)")
_FIX_BAD_LIMIT = ("limit must be a whole number of at least 1, or omitted "
                  "to inspect everything that has gone stale")

# The 403 remedy passed to envelopes.api_error throughout this module. Both
# tools here take a `site` the caller chose, so the likelier 403 is a
# property this account cannot read — "list them and pass one exactly as
# listed" is the step that resolves it. server.py's tools take no site and
# pass FIX_PROPERTIES for the same status; see envelopes.api_fix.
_FORBIDDEN = envelopes.FIX_UNKNOWN_PROPERTY


def _bad_limit(limit: object) -> bool:
    """True when `limit` is present but not a usable count.

    `isinstance(limit, bool)` is checked explicitly because bool subclasses
    int: True would otherwise pass as the integer 1 and silently cap a run
    at a single URL.
    """
    if limit is None:
        return False
    return isinstance(limit, bool) or not isinstance(limit, int) or limit < 1


def _property_urls(provider: object) -> list[str]:
    """The property URLs alone, not the API's entries.

    `api.list_properties` returns `{"siteUrl", "permissionLevel"}` dicts
    straight from Google, and both tools in this module want the URLs on
    their own: one to check the caller's `site` against, the other to hand
    to `routing.route_all`, which is annotated `list[str]` and means it.

    Doing the conversion once, here, is what stops `site not in properties`
    from comparing a string against a list of dicts — an expression that is
    valid Python, quietly always True, and therefore refuses every property
    the account actually owns. The type checker cannot see it because
    `in` accepts any container, and the tests could not see it because
    their fake returned the shape the caller wanted rather than the shape
    the real function documents.
    """
    return [entry["siteUrl"] for entry in api.list_properties(provider)]


def find_unindexed(site: str, source: str = "both",
                   limit: int | None = None) -> dict:
    """Which of a property's URLs are not indexed, and why.

    `limit` caps the number of URLs INSPECTED, not the number returned: an
    inspection spends a quota slot and a spent slot is unrecoverable.
    Which URLs a limited run reaches follows the store's own url ordering
    (alphabetical), not staleness — a limited run is a sample, not a
    worst-first sweep.

    On a missing, expired, or rejected token, returns `{"ok": False,
    "error": "auth_required", "fix": ...}` instead of raising; if no OAuth
    client is configured at all, `"not_configured"`. Every other failure
    also returns rather than raises, reported by exception type name only.
    """
    if source not in discovery.SOURCES:
        return {"ok": False, "error": "bad_source", "fix": _FIX_BAD_SOURCE}
    if _bad_limit(limit):
        return {"ok": False, "error": "bad_limit", "fix": _FIX_BAD_LIMIT}
    try:
        deps.oauth_client()
        provider = deps.provider()
        # Before the sitemap fetch, not after. api.check_status never
        # raises AuthRequired -- a 401 on any URL becomes an "error" row
        # inside _safe_inspect by design. A sitemap fetch needs no token
        # and would succeed, so a signed-out caller would otherwise get a
        # normal-shaped result in which every URL failed inspection: a
        # confident answer built entirely out of failures.
        provider.access_token()

        properties = _property_urls(provider)
        if site not in properties:
            return {"ok": False, "error": "unknown_property",
                    "fix": envelopes.FIX_UNKNOWN_PROPERTY}

        settings = config.load()
        with deps.connection() as conn:
            return discovery.find_unindexed(
                conn, site, provider, properties, source=source, limit=limit,
                ttl_days=settings["inspection_ttl_days"],
                concurrency=settings["inspect_concurrency"])
    except deps.NotConfigured:
        log.info("gsc_find_unindexed: no OAuth client configured")
        return {"ok": False, "error": "not_configured",
                "fix": envelopes.FIX_OAUTH_CLIENT}
    except gauth.AuthRequired:
        log.info("gsc_find_unindexed: no usable token")
        return {"ok": False, "error": "auth_required", "fix": envelopes.FIX_TOKEN}
    except api.ApiError as exc:
        return envelopes.api_error("gsc_find_unindexed", exc, _FORBIDDEN)
    except Exception as exc:  # noqa: BLE001 — see _unexpected
        # Exception, never RuntimeError: deps.NotConfigured subclasses
        # RuntimeError (deps.py:41), as do gauth.AuthRequired and
        # api.ApiError, so catching the base class here would shadow all
        # three arms above and report each of them as "unexpected".
        return envelopes.unexpected("gsc_find_unindexed", exc)


def audit(site: str) -> dict:
    """The current indexation position for one property, from the store.

    Point in time only. Nothing here is a movement metric: there is no
    first_indexed, no de_indexed, no "changed since". A zero that means
    "we did not measure this" reads to a caller as "nothing moved", so the
    numbers that were never measured are omitted rather than reported.

    Reads the store — no HTTP inspection, no quota spent. The property
    list is still fetched, so a typo is refused instead of answered with a
    confident all-zeros audit.
    """
    try:
        deps.oauth_client()
        provider = deps.provider()
        properties = _property_urls(provider)
        if site not in properties:
            return {"ok": False, "error": "unknown_property",
                    "fix": envelopes.FIX_UNKNOWN_PROPERTY}

        settings = config.load()
        with deps.connection() as conn:
            return audit_core.audit(
                conn, site, ttl_days=settings["inspection_ttl_days"])
    except deps.NotConfigured:
        log.info("gsc_audit: no OAuth client configured")
        return {"ok": False, "error": "not_configured",
                "fix": envelopes.FIX_OAUTH_CLIENT}
    except gauth.AuthRequired:
        log.info("gsc_audit: no usable token")
        return {"ok": False, "error": "auth_required", "fix": envelopes.FIX_TOKEN}
    except api.ApiError as exc:
        return envelopes.api_error("gsc_audit", exc, _FORBIDDEN)
    except Exception as exc:  # noqa: BLE001 — see _unexpected
        return envelopes.unexpected("gsc_audit", exc)
