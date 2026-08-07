"""The `{ok: False, error, fix}` refusal envelope, defined once.

Every tool in this package promises the same contract: never raise across
the MCP boundary, always return a dict a model can act on. Three helpers
build the recurring shapes, and a set of FIX_* strings supply the remedy
text. They lived in server.py and were copied into tools_audit.py because
server.py imports tools_audit and importing back would cycle. This module
imports nothing from the package, so both can import it and the cycle
never arises.

The copies had already drifted, which is the reason this exists rather
than a tidiness argument:

- tools_audit's FIX_OAUTH_CLIENT still told a caller to set the env vars
  and specifically NOT to run gsc_setup, on the grounds that gsc_setup
  would only tell them the same thing. That stopped being true when the
  shipped-client download landed: gsc_setup now fetches a client on first
  run, so it is the one-step answer for a source checkout, and the copy
  that says otherwise sends the caller the long way round. server.py's
  copy had been updated; this one had not.
- The two FIX_TOKEN strings disagreed on what to actually run. One named
  gsc_setup; the other said "run the consent flow", which is not the name
  of anything a caller can call.
- Three FIX_UNEXPECTED strings said the same thing three ways.

A remedy that varies by which tool you happened to call is worse than a
vague one: the caller has no way to tell which of the two applies, and no
reason to suspect there are two.

WHERE A DIFFERENCE IS REAL, IT IS A PARAMETER. api_fix() takes the 403
remedy from its caller rather than being near-copied for it, because 403
genuinely means different things in different tools -- see that function.
The same reasoning gives unexpected() its optional `fix`: browser
detection earns its own wording, and a parameter is how it gets one
without a fourth copy of the envelope.
"""
from __future__ import annotations

from gsc_core import api, runlog

log = runlog.get(__name__)

FIX_OAUTH_CLIENT = (
    "No OAuth client is configured. Run gsc_setup() to download the bundled "
    "client, or set GSC_MCP_CLIENT_ID and GSC_MCP_CLIENT_SECRET to use your own."
)
FIX_TOKEN = "Not signed in. Run gsc_setup() to authorise Search Console access."
FIX_PROPERTIES = (
    "This account has no Search Console properties, or the token lacks "
    "the webmasters scope."
)
FIX_UNKNOWN_PROPERTY = (
    "Run gsc_list_sites to see the properties this account can reach, and "
    "pass one of them exactly as listed."
)
FIX_API = (
    "Search Console refused the call. Run gsc_doctor, and confirm the "
    "signed-in account can see this property in Search Console."
)
FIX_UNEXPECTED = (
    "Something failed unexpectedly. Run gsc_doctor to check the setup and "
    "retry; the log file records what happened."
)


def refuse(error: str, detail: str, fix: str) -> dict:
    """The bare envelope, for refusals a tool detects itself."""
    return {"ok": False, "error": error, "detail": detail, "fix": fix}


def api_fix(status: int | None, forbidden: str) -> str:
    """The remedy for an ApiError, given the 403 remedy this tool wants.

    403 is almost always a scope or a permission problem, which has a more
    useful next step than the generic one -- but WHICH step depends on the
    tool, and that is why `forbidden` is an argument instead of a constant.

    A tool that takes a `site` the caller chose (gsc_audit,
    gsc_find_unindexed) most likely hit a property this account cannot
    read, so FIX_UNKNOWN_PROPERTY -- list them and pass one exactly as
    listed -- is what resolves it. A tool that takes no site
    (gsc_list_sites) cannot have been given a wrong one, so the same 403
    means the account has no properties or the token lacks the webmasters
    scope, which is FIX_PROPERTIES. Both were correct; having them live in
    two near-identical private functions is what let everything AROUND
    them drift too.
    """
    return forbidden if status == 403 else FIX_API


def api_error(tool: str, exc: api.ApiError, forbidden: str) -> dict:
    """Search Console answered, and said no. Logs the status, not the body."""
    log.warning("%s: Search Console returned HTTP %s", tool, exc.status)
    return {"ok": False, "error": "api_error", "status": exc.status,
            "fix": api_fix(exc.status, forbidden)}


def unexpected(tool: str, exc: Exception, fix: str = FIX_UNEXPECTED) -> dict:
    """The catch-all that keeps the {ok, error, fix} contract whole.

    Without this an unmodelled failure -- an expired token meeting a
    transiently failing token endpoint raises a plain RuntimeError out of
    the eager probe in gsc_check_status, for one -- escapes the tool.
    The SDK does turn that into `isError: true` rather than killing the
    session, but the caller loses the structured `fix` this package
    promises and gets a stringified exception to parse instead.

    Only the exception's TYPE NAME is recorded, never its message, for the
    same reason gsc_doctor does the same: a message can carry an absolute
    path containing the operator's account name (an OSError out of the
    store is the everyday case, not a hypothetical one), a credentialed
    URL, or a truncated response body.
    """
    log.warning("%s: unexpected %s", tool, type(exc).__name__)
    return {"ok": False, "error": "unexpected", "detail": type(exc).__name__,
            "fix": fix}
