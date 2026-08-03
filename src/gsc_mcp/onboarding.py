"""`gsc_setup`'s body: one idempotent tool that says what is left to do.

WHY ONE TOOL, NOT A START/POLL PAIR. The spec asks for a tool that is
"idempotent; returns done/pending steps", and that is also the shape a
language model handles best. Every call re-reports the whole state, so a
model that has lost track of where it was simply calls again and is told.
There is no session to resume, no handle to remember, and no ordering the
caller can get wrong. Calling it in a loop is safe: it spends no Search
Console quota, and makes at most one API call (the token verification).

FOUR STEPS, IN ORDER. `oauth_client`, `consent`, `browser`, `extension`.
Each one is only meaningful once the ones before it are satisfied — there
is no point checking a token when there is no client to have issued it —
so the first unsatisfied step becomes `next` and the call returns there.
`done` lists what is behind the user, `pending` what is still ahead,
including the step in `next`.

THE PENDING CONSENT. Consent is the one step that cannot be answered by
looking: it needs a live loopback receiver holding an ephemeral port and a
PKCE verifier, waiting for Google to redirect. That state lives in
`_pending`, a module-level dict keyed by client id and guarded by a lock,
and its lifecycle rules all exist to keep the "call me again" promise
honest:

  * A repeat call BEFORE the user clicks returns the same URL. Starting a
    second consent would bind a second port with a second `state`, and the
    first screen the user is actually looking at would then redirect to a
    receiver that has been replaced — a flow that fails only for the user
    who was slow, which is the worst kind.
  * A pending consent that COMPLETES or FAILS is closed and discarded
    immediately. The receiver owns a socket and a thread; the caller of
    `start_consent` owns closing it.
  * A pending consent older than `_CONSENT_TTL` seconds is closed and
    replaced. Google's authorization code and the loopback both go stale,
    so a user coming back an hour later must be handed a fresh URL rather
    than a dead one.
  * A process restart empties the dict, which is correct: the receiver
    died with the process, so there is nothing to resume and the next call
    starts a fresh flow.

NEVER RETURNED TO THE CLIENT, EVER: the PKCE verifier, the authorization
code, the client secret, or any token field. And never LOGGED either, at
any level, including the `state`.

The one thing that does travel out is `next.url`, the consent URL, and it
carries the client id, the PKCE challenge and the `state`. All three are
public by construction: OAuth delivers that URL through the user's own
browser, the challenge is a hash of the verifier and not the verifier, and
the `state` exists precisely to be sent to Google and echoed back — a
consent URL without it would have no CSRF binding for `poll()` to check.
What the `state` must never be is a field of its own in the response,
which would invite a caller to store or forward it separately.

NO EMAIL ADDRESS IS RETURNED OR LOGGED. A recommended profile is named by
its browser and its profile directory, exactly as in `tools_browsers` —
see that module's docstring for the reasoning, which applies here
unchanged. Failures are logged by exception TYPE NAME only: an OSError's
message carries a filesystem path holding the operator's account name.

`next.path` is the one filesystem path this module does return, and it is
unavoidable: "Load unpacked" needs a folder to select, and no other string
identifies it. It is our own config directory, never a browser's.

Nothing here writes to stdout — stdout is the MCP JSON-RPC transport.
"""
from __future__ import annotations

import threading
import time
import webbrowser
from dataclasses import dataclass

from gsc_core import gauth, pairing, profiles, runlog

from . import deps, shipped_client

log = runlog.get(__name__)

STEPS = ("oauth_client", "consent", "browser", "extension")

#: How long a pending consent stays usable. Google's authorization code is
#: short-lived and the loopback receiver holds a port the whole time.
_CONSENT_TTL = 600.0

_ACTION_OAUTH_CLIENT = (
    "Call gsc_setup() again to retry, or set GSC_MCP_CLIENT_ID and "
    "GSC_MCP_CLIENT_SECRET to use your own OAuth client"
)
_ACTION_CONSENT_OPEN = (
    "open the URL in next.url, approve the consent screen, then call "
    "gsc_setup() again"
)
_ACTION_CONSENT_WAITING = (
    "approve the consent screen, then call gsc_setup() again"
)
_ACTION_CONSENT_RETRY = (
    "the sign-in did not complete; call gsc_setup() again to get a fresh "
    "consent URL"
)
_ACTION_CONSENT_UNREACHABLE = (
    "the stored sign-in could not be checked because Google could not be "
    "reached; check the network connection and call gsc_setup() again"
)
_ACTION_BROWSER = (
    "install Chrome, Brave, Edge, Vivaldi, Opera, or Chromium, then call "
    "gsc_setup() again"
)
_ACTION_EXTENSION_NO_DIR = (
    "the bridge extension could not be unpacked to this machine's gsc-mcp "
    "config directory; make that directory writable (or set GSC_MCP_HOME to "
    "one that is) and call gsc_setup() again"
)
#: Two different reasons an account on disk may not be what it looks like,
#: and they are NOT opposite ends of one flag — see browsers.Brand.
#:
#: This one is Edge: an address was found, in the same file and the same key
#: Chrome uses, but Edge signs profiles in to MICROSOFT identities by default
#: and stores those addresses in exactly the same place. Nothing on disk
#: tells them apart, so the profile scoring cannot either — and if the user's
#: Microsoft address happens to equal their Google one, the score is a false
#: MATCH, not merely a false "signed in".
_NOTE_MAY_BE_NON_GOOGLE = (
    " This profile's Google sign-in could not be confirmed — this browser "
    "stores Microsoft accounts in the same place as Google ones, so the "
    "account found here may not be a Google account at all. Check it is the "
    "one that owns your Search Console properties."
)
#: This one is Brave, Vivaldi, Opera and plain Chromium: the brand records no
#: Google account, so anything found is not evidence of a Google sign-in.
_NOTE_NOT_DISCOVERABLE = (
    " This profile's Google sign-in could not be confirmed — this browser "
    "does not record a Google account, so check you are signed in to it "
    "with the account that owns your Search Console properties."
)
_FIX_BROWSER_UNEXPECTED = (
    "Browser detection failed unexpectedly. The log file records the failure "
    "type; retry, and if it persists name the browser and profile you want "
    "to use instead."
)
_FIX_EXTENSION_UNEXPECTED = (
    "The extension check failed unexpectedly. The log file records the "
    "failure type; retry, and if it persists reload the gsc-mcp extension "
    "from your browser's extensions page."
)
_DETAIL_NO_BROWSER = "no Chromium browser found on this machine"
_COULD_NOT_CHECK = (
    "whether the gsc-mcp bridge extension is installed in {where} could not "
    "be checked"
)
#: 3A can see that the extension is REGISTERED. It cannot see whether the
#: MV3 service worker is alive, because that needs the bridge to attempt a
#: connection — which is 3B's. The detail says so rather than letting a
#: green check be read as "the submission path works".
_NOTE_WORKER_UNKNOWN = (
    "; whether its background service worker is running is not checked in "
    "this milestone"
)


@dataclass
class _Held:
    """A pending consent plus when it was started.

    Mutable so `_age_pending` can move `started` backwards; the consent
    itself is never mutated.
    """

    consent: gauth.PendingConsent
    started: float


_lock = threading.Lock()
_pending: dict[str, _Held] = {}


# ---------------------------------------------------------------------------
# Test seams
# ---------------------------------------------------------------------------

def _reset_pending() -> None:
    """TEST SEAM. Close and forget every pending consent.

    Exists so a test can start from a known state without a live browser;
    also the honest way to release the sockets a test's half-finished flows
    are holding.
    """
    with _lock:
        held = list(_pending.values())
        _pending.clear()
    for item in held:
        _close(item.consent)


def _peek_pending(client_id: str) -> gauth.PendingConsent | None:
    """TEST SEAM. The live PendingConsent for a client id, if any.

    Exists so a test can assert that the verifier and the state a response
    was built alongside are absent FROM that response. Nothing in
    production reads this; a caller that needs the pending consent takes
    it under the lock.
    """
    with _lock:
        held = _pending.get(client_id)
    return held.consent if held else None


def _age_pending(client_id: str, *, seconds: float) -> None:
    """TEST SEAM. Pretend a pending consent was started `seconds` ago.

    Exists so the expiry rule is testable without waiting ten minutes, and
    without a clock injected through production code that has no other use
    for one.
    """
    with _lock:
        held = _pending.get(client_id)
        if held is not None:
            held.started -= seconds


# ---------------------------------------------------------------------------
# The tool body
# ---------------------------------------------------------------------------

def setup(open_browser: bool = True) -> dict:
    """Report setup state and hand back the single next action. Never raises."""
    try:
        return _setup(open_browser)
    except Exception as exc:  # noqa: BLE001 — mirrors server._unexpected
        log.warning("gsc_setup: unexpected %s", type(exc).__name__)
        return _result([], {
            "step": "unexpected",
            "action": ("setup failed unexpectedly; the log file records the "
                       "failure type. Retry, and if it persists check the "
                       "gsc-mcp log."),
        })


def _setup(open_browser: bool) -> dict:
    done: list[str] = []

    try:
        client_id, client_secret = deps.oauth_client()
    except deps.NotConfigured:
        # The one place in the server that may touch the network for a
        # client. A source checkout has none until this runs; a release
        # build and a configured environment never reach here at all.
        try:
            client_id, client_secret = shipped_client.fetch_and_cache()
        except shipped_client.FetchFailed as exc:
            # FetchFailed's message is written for a user and carries no
            # response body and no part of the credential — see its
            # docstring. It is the only exception in this module whose text
            # is repeated outward.
            return _result(done, {"step": "oauth_client",
                                  "action": f"{exc}. {_ACTION_OAUTH_CLIENT}"})
    done.append("oauth_client")

    consent = _consent_step(client_id, client_secret, open_browser)
    if consent is not None:
        return _result(done, consent)
    done.append("consent")

    candidates = profiles.survey()
    best = profiles.recommend(candidates)
    if best is None:
        return _result(done, {"step": "browser", "action": _ACTION_BROWSER})
    done.append("browser")

    extension = _extension_step(best)
    if extension is not None:
        return _result(done, extension)
    done.append("extension")

    return _result(done, None)


def _result(done: list[str], nxt: dict | None) -> dict:
    """The response shape, with `pending` derived rather than tracked.

    Deriving it means `done` and `pending` cannot disagree — two lists
    maintained by hand drift the first time a step is added.
    """
    pending = [step for step in STEPS if step not in done]
    return {"ok": nxt is None and len(done) == len(STEPS),
            "done": list(done), "pending": pending, "next": nxt}


# ---------------------------------------------------------------------------
# Step 2: consent
# ---------------------------------------------------------------------------

def _consent_step(client_id: str, client_secret: str,
                  open_browser: bool) -> dict | None:
    """None when a stored token verifies; otherwise the `next` to return.

    Checked before any pending consent is touched: a user who signed in
    through some other path should not be handed a consent URL, and a
    completed flow that already saved its token must be recognised even if
    the receiver that caught it is gone.
    """
    state = _stored_token_state()
    if state == "ok":
        _discard(client_id)
        return None
    if state == "unreachable":
        return {"step": "consent", "action": _ACTION_CONSENT_UNREACHABLE}
    return _advance_consent(client_id, client_secret, open_browser)


def _stored_token_state() -> str:
    """"ok", "missing", or "unreachable" for the token already on disk.

    "unreachable" is not a polite "missing". A network fault while checking
    an existing token must not push the user through a whole fresh consent
    round trip for a sign-in that is very probably fine — so the three are
    kept apart, exactly as `has_extension` keeps "no" apart from "could not
    check".

    Never raises and never logs a token field.
    """
    try:
        token = gauth.load_token()
    except Exception as exc:  # noqa: BLE001 — an unreadable token is "not signed in"
        log.debug("stored token unreadable (%s)", type(exc).__name__)
        return "missing"
    if not isinstance(token, dict) or not token:
        return "missing"
    try:
        gauth.verify_token(token)
    except gauth.ConsentFailed:
        # The grant itself is wrong — no refresh token, rejected, or it
        # sees no properties. Re-consent is the fix.
        return "missing"
    except Exception as exc:  # noqa: BLE001 — transport, DNS, proxy
        log.debug("token verification unreachable (%s)", type(exc).__name__)
        return "unreachable"
    finally:
        del token
    return "ok"


def _advance_consent(client_id: str, client_secret: str,
                     open_browser: bool) -> dict | None:
    """Poll or start the consent round trip; return the `next` to report.

    None means the redirect had landed and this very call completed the
    flow — the token is saved and verified, so consent is satisfied and the
    caller moves on to the browser step in the same response rather than
    making the user call once more for nothing.

    Everything happens under `_lock`, including the token exchange. That
    serialises two concurrent `gsc_setup` calls, which is the point: the
    hazard being prevented is exactly two calls each starting their own
    receiver, and a lock released before the check-then-start would not
    prevent it. The critical section is bounded by one HTTP exchange to
    Google, and this is a setup tool nobody calls concurrently on purpose.
    """
    with _lock:
        held = _pending.get(client_id)
        if held is not None and _expired(held):
            _pending.pop(client_id, None)
            _close(held.consent)
            held = None

        if held is not None:
            outcome = _poll(held.consent, client_id, client_secret)
            if outcome == "done":
                _pending.pop(client_id, None)
                return None
            if outcome == "failed":
                _pending.pop(client_id, None)
                return {"step": "consent", "action": _ACTION_CONSENT_RETRY}
            return {"step": "consent", "action": _ACTION_CONSENT_WAITING,
                    "url": held.consent.auth_url}

        consent = gauth.start_consent(client_id)
        _pending[client_id] = _Held(consent=consent, started=time.monotonic())
        url = consent.auth_url

    if open_browser:
        _open(url)
    return {"step": "consent", "action": _ACTION_CONSENT_OPEN, "url": url}


def _poll(consent: gauth.PendingConsent, client_id: str,
          client_secret: str) -> str:
    """"waiting", "done", or "failed" — and never leaks what it saw.

    `finish_consent` already closes the receiver on every terminal path, so
    the caller's job is only to forget the holder. A raise here is an
    ordinary outcome (a refused consent, a state mismatch, a stale code),
    not a fault worth propagating out of a setup tool, and its MESSAGE is
    never logged: an exchange failure's text can quote the request.
    """
    try:
        token = gauth.finish_consent(consent, client_secret,
                                     client_id=client_id)
    except Exception as exc:  # noqa: BLE001 — see docstring
        log.debug("consent did not complete (%s)", type(exc).__name__)
        return "failed"
    if token is None:
        return "waiting"
    del token
    return "done"


def _expired(held: _Held) -> bool:
    """Is this pending consent past its useful life?

    `time.monotonic` rather than wall clock: a machine that resyncs its
    clock, or comes back from sleep with a corrected time, must not be able
    to expire a consent that is seconds old or keep one that is an hour
    old.
    """
    return (time.monotonic() - held.started) >= _CONSENT_TTL


def _discard(client_id: str) -> None:
    """Close and forget any pending consent for this client id."""
    with _lock:
        held = _pending.pop(client_id, None)
    if held is not None:
        _close(held.consent)


def _close(consent: gauth.PendingConsent) -> None:
    """Release a receiver's port and thread. Never raises."""
    try:
        consent.receiver.close()
    except Exception as exc:  # noqa: BLE001 — already being discarded
        log.debug("consent receiver close failed (%s)", type(exc).__name__)


def _open(url: str) -> None:
    """Best effort. A headless machine has no browser and that is fine —
    the URL is in the response either way."""
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 — the URL is returned regardless
        log.debug("could not open a browser (%s)", type(exc).__name__)


# ---------------------------------------------------------------------------
# Step 4: extension
# ---------------------------------------------------------------------------

def _extension_step(best: profiles.Candidate) -> dict | None:
    """None when the bridge extension is installed in the recommended
    profile; otherwise the `next` to return.

    "Could not check" is NOT worded as "not installed". `look_up_extension`
    reports both, and a user whose preferences file was momentarily
    unreadable must not be told to reinstall something that is sitting
    right there — so the two share an action (install it if it is not
    there) but not a sentence.
    """
    try:
        ext_dir = pairing.extension_dir()
    except Exception as exc:  # noqa: BLE001 — logged by type: the path names the user
        log.debug("extension directory unavailable (%s)", type(exc).__name__)
        return {"step": "extension", "action": _ACTION_EXTENSION_NO_DIR}

    lookup = pairing.look_up_extension(best.installed, best.profile,
                                       ext_dir=ext_dir)
    if lookup.extension_id is not None:
        return None
    return {"step": "extension",
            "action": _extension_action(best, lookup.complete),
            "path": str(ext_dir)}


def _extension_action(best: profiles.Candidate, complete: bool) -> str:
    """What to tell the user, without naming them and without overclaiming.

    The extensions URL is read off the Brand, never rebuilt from the brand
    key: Chromium registers no chromium:// scheme and genuinely uses
    chrome://extensions, so a derived URL does not open.
    """
    brand = best.installed.brand
    where = f"{brand.label} / {best.profile.directory}"
    if complete:
        opening = (f"the gsc-mcp bridge extension is not installed in "
                   f"{where}.")
    else:
        opening = (f"whether the gsc-mcp bridge extension is installed in "
                   f"{where} could not be checked — a browser preferences "
                   f"file could not be read, so it may already be there.")
    action = (f"{opening} To install it: open {brand.extensions_url}, enable "
              f"Developer mode, choose Load unpacked, and select the folder "
              f"in next.path. Then call gsc_setup() again.")
    return action + _account_caveat(best.profile, brand)


# ---------------------------------------------------------------------------
# gsc_doctor's two setup checks
#
# They live here rather than in server.py for two reasons. server.py is
# already over the file-size ceiling and holds tool registration, not tool
# logic; and everything these two need — the brand caveats, the extension
# wording, the "could not check" distinction — is already written here for
# gsc_setup. Two modules phrasing the same diagnosis differently is how a
# user ends up being told to reinstall by one tool and to reload by
# another.
#
# Both return the {"name", "ok", "detail", "fix"} shape the other five
# checks return, both are last in the doctor's list because they cost
# nothing, and neither raises: a doctor that dies on one check is worse
# than no doctor.
# ---------------------------------------------------------------------------

def check_browser() -> dict:
    """Is there a Chromium browser and profile to drive?

    A raise is reported as a failed check carrying the exception's TYPE
    NAME only. The message is never included and never logged: an OSError
    from a browser state file quotes a path containing the operator's
    account name, and a parse error quotes a file holding their address.
    """
    name = "browser"
    try:
        best = profiles.recommend(profiles.survey())
    except Exception as exc:  # noqa: BLE001 — see docstring
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return _check(name, False, type(exc).__name__, _FIX_BROWSER_UNEXPECTED)

    if best is None:
        return _check(name, False, _DETAIL_NO_BROWSER, _ACTION_BROWSER)

    detail = (f"{_where(best)} is the profile to use"
              + _account_caveat(best.profile, best.installed.brand))
    return _check(name, True, detail, "")


def check_extension() -> dict:
    """Is the bridge extension loaded into that profile, and is it current?

    Four outcomes, not three. The brief's table lists never-installed,
    version-mismatch and present; the fourth is "could not be checked",
    and it is the reason this uses `look_up_extension` rather than
    `find_extension_id`. A user whose preferences file was momentarily
    unreadable must not be told to reinstall something that is sitting
    right there, so "could not be checked" gets its own detail AND its own
    fix — a shared fix string would make the two indistinguishable to
    anyone reading the output rather than the code.

    A green check means REGISTERED, not working; see _NOTE_WORKER_UNKNOWN.
    """
    name = "extension"
    try:
        return _extension_check(name)
    except Exception as exc:  # noqa: BLE001 — see check_browser
        log.warning("doctor: %s check raised %s", name, type(exc).__name__)
        return _check(name, False, type(exc).__name__,
                      _FIX_EXTENSION_UNEXPECTED)


def _extension_check(name: str) -> dict:
    best = profiles.recommend(profiles.survey())
    if best is None:
        return _check(name, False, _DETAIL_NO_BROWSER, _ACTION_BROWSER)

    where = _where(best)
    try:
        ext_dir = pairing.extension_dir()
    except Exception as exc:  # noqa: BLE001 — logged by type: the path names the user
        log.debug("extension directory unavailable (%s)", type(exc).__name__)
        return _check(name, False, _COULD_NOT_CHECK.format(where=where)
                      + " — the extension could not be unpacked, so there was "
                        "nothing to match against",
                      _ACTION_EXTENSION_NO_DIR)

    lookup = pairing.look_up_extension(best.installed, best.profile,
                                       ext_dir=ext_dir)
    if lookup.extension_id is None:
        return _absent(name, best, where, lookup.complete)
    return _present(name, best, ext_dir, where, lookup.version)


def _absent(name: str, best: profiles.Candidate, where: str,
            complete: bool) -> dict:
    """Not found — and whether that is an answer or a shrug.

    The extraction directory is deliberately NOT interpolated here, even
    though this function knows it. A doctor result travels into an MCP
    client we do not control, to be retained, logged or synced; an absolute
    path on this platform carries the operator's account name with it.
    gsc_setup() already names the folder in ``next.path``, so the fix points
    there instead — see ``_where``.
    """
    url = best.installed.brand.extensions_url
    install = (f"call gsc_setup(), then open {url}, enable Developer mode, "
               f"choose Load unpacked, and select the folder gsc_setup() "
               f"names in next.path.")
    if complete:
        return _check(name, False,
                      f"the gsc-mcp bridge extension is not installed in "
                      f"{where}",
                      f"To install it: {install}")
    return _check(name, False,
                  _COULD_NOT_CHECK.format(where=where)
                  + " — a browser preferences file could not be read, so it "
                    "may already be there",
                  f"Run gsc_doctor again; if the browser is running, close "
                  f"it first. If it really is not installed: {install}")


def _present(name: str, best: profiles.Candidate, ext_dir, where: str,
             loaded: str | None) -> dict:
    """Found — but possibly a build the browser loaded some releases ago.

    A missing version on either side is "cannot compare", never "differs".
    Telling a user to click Reload because a preferences entry happened to
    omit a manifest snapshot would be a fabricated instruction, and they
    would do it.
    """
    packaged = pairing.extension_version(ext_dir)
    if loaded and packaged and loaded != packaged:
        return _check(
            name, False,
            f"the gsc-mcp bridge extension in {where} is loaded at version "
            f"{loaded}, but the copy on disk is now version {packaged}",
            f"Click Reload on the gsc-mcp extension in "
            f"{best.installed.brand.extensions_url}.")
    shown = loaded or packaged
    version = f" at version {shown}" if shown else ""
    return _check(name, True,
                  f"the gsc-mcp bridge extension is installed in {where}"
                  f"{version}{_NOTE_WORKER_UNKNOWN}", "")


def _check(name: str, ok: bool, detail: str, fix: str) -> dict:
    """The doctor's check shape, built in one place.

    A failing check with an empty `fix` is the defect this guards: it is
    the difference between a diagnosis and a complaint, and it is easy to
    introduce by adding a branch and forgetting the string.
    """
    assert ok or fix.strip(), f"{name}: a failing check needs a fix"
    return {"name": name, "ok": ok, "detail": detail, "fix": fix}


def _where(best: profiles.Candidate) -> str:
    """A profile named the way a human picks one, and no other way.

    Brand label plus on-disk directory. NOT the display name — Chrome
    labels profiles with the account signed into them often enough that
    passing it through would leak an address into a transcript — and not
    the path, which contains the operator's account name on every
    supported OS.
    """
    return f"{best.installed.brand.label} / {best.profile.directory}"


def _account_caveat(profile: profiles.Profile, brand) -> str:
    """The caveat this brand's account needs, or nothing.

    Two conditions, checked separately because they are separate facts and
    only one of them is about Edge. Gating both on
    `not reports_google_account` was the bug this replaces: it hedged the
    brands that write no address, and left the one brand that writes a
    possibly-Microsoft address presented as a confirmed Google sign-in.

    No caveat is needed when no address was found at all — there is nothing
    being claimed to overstate.
    """
    if not profile.email:
        return ""
    if brand.account_may_be_non_google:
        return _NOTE_MAY_BE_NON_GOOGLE
    if not brand.reports_google_account:
        return _NOTE_NOT_DISCOVERABLE
    return ""
