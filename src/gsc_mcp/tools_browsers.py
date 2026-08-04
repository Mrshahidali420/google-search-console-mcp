"""gsc_detect_browsers' body: which browser profile the operator should use.

Layers 1-3 (browsers.detect, profiles.list_profiles, profiles.recommend) do
the work. This module does two things they cannot: it decides what part of
their answer is safe to hand to a language model, and it guarantees the
{ok, error, fix} contract server.py's other tools already keep.

PRIVACY — the whole reason this file exists separately.

This is the first layer whose output LEAVES the process. Everything below
kept the signed-in address in memory: read from the browser's own files,
never written, never logged. An MCP tool result has none of those
guarantees. It is consumed by a model, rendered into a transcript, and
retained by whatever client is driving the server — none of which this
project controls. The working assumption here is therefore that anything
returned is stored somewhere the operator cannot reach to delete.

So NO ADDRESS IS RETURNED, in either direction:

  * not the address found in a profile on disk — the user is picking a
    profile, and "Google Chrome / Work / signed in / matches the account
    you authorised" answers that question completely. The address adds
    nothing to the decision and everything to the transcript.
  * not the operator's own authorised address either. profiles.recommend
    writes the address the CALLER supplied into its reason string ("it is
    signed in as <address>, the account you authorised"), so passing those
    sentences through untouched would put it in the transcript by the back
    door. _redact() removes it. That the redaction is complete rests on
    Candidate's stated invariant: only the address the caller supplied can
    appear in a reason, an unrelated one is described rather than quoted.
  * not the profile's display NAME when the name is itself an address —
    Chrome will happily label a profile with the account signed into it.
  * not any filesystem path. A profile path contains the operator's
    account name on every supported OS, and brand + profile directory is
    what identifies a profile to a human anyway.

What the transcript does contain: a brand label, a brand key, the brand's
static extensions URL, a profile directory name, a display name, three
booleans about the account, whether our bridge extension is installed in
that profile — the fact, never the extension ID, which is 32 characters
naming one person's install — and prose
reasons with no identifier in them. `matches_authorised_account` is what makes that
sufficient — it is the useful half of knowing the address, without the
address.

The address needed to compute that flag is read from the STORED TOKEN, not
taken as a parameter. A parameter would make the caller — a model, writing
into the same transcript — name the operator's address out loud to ask a
question about it, which is worse than anything this module refuses to
return.

Nothing here writes to stdout, and failures are logged by exception TYPE
NAME only: an OSError from a browser state file carries a path holding the
operator's Windows account name.
"""
from __future__ import annotations

from pathlib import Path

from gsc_core import config, gauth, pairing, profiles, runlog

from . import envelopes, target

log = runlog.get(__name__)

_NOTE_NONE_FOUND = (
    "No Chromium browser was found on this machine. Install Google Chrome, "
    "Brave, Microsoft Edge, Vivaldi, Opera, or Chromium, sign in to it with "
    "the Google account that owns your Search Console properties, then run "
    "gsc_detect_browsers again."
)
_FIX_UNEXPECTED = (
    "Browser detection failed unexpectedly. The log file records the failure "
    "type; retry, and if it persists name the browser and profile you want "
    "to use instead."
)
_REDACTED_ACCOUNT = "the account you authorised"
_FIX_PIN_UNEXPECTED = (
    "The browser choice could not be saved. The log file records the failure "
    "type; retry, and if it persists check that the config directory is "
    "writable."
)
_NOTE_PINNED = (
    "This profile is now used by every tool that drives the browser, whatever "
    "the detector would have recommended. Load the bridge extension into it "
    "if you have not already — gsc_doctor will tell you. Call "
    "gsc_use_browser(clear=True) to go back to the recommendation."
)
_NOTE_CLEARED = (
    "No browser is pinned. The detector's recommendation is used again; run "
    "gsc_detect_browsers to see what it picks."
)


def detect_browsers() -> dict:
    """The tool body. Never raises — see the module docstring."""
    try:
        return _report()
    except Exception as exc:  # noqa: BLE001 — the tool must never raise
        # Its own fix string, passed in rather than copied into a fourth
        # envelope: browser detection's next step is genuinely not "run
        # gsc_doctor and retry".
        return envelopes.unexpected("gsc_detect_browsers", exc,
                                    _FIX_UNEXPECTED)


def _report() -> dict:
    account_email = _authorised_email()
    candidates = profiles.survey()

    # ONE call, and the only source of the answer. Re-ranking here, or
    # picking a "best" from the list below, is the two-call-sites-disagree
    # defect the whole stack is shaped to prevent — which is also why this
    # goes through target.select() rather than profiles.recommend(): a pin
    # the bridge honours and this report ignores would show the user a
    # green tick beside a profile the server is not driving.
    selection = target.select(candidates, account_email=account_email)
    best = selection.candidate

    # survey()'s order is already deterministic — BRANDS order, then
    # profiles.py's Default-first numeric sort. Re-sorting on the score
    # would be re-deriving the ranking; leaving it alone keeps two identical
    # runs identical.
    # Resolved ONCE for the whole survey, and passed down. Per-profile
    # resolution would re-extract the extension up to six times per call,
    # and — worse — could hand two entries different answers if an upgrade
    # landed mid-report.
    ext_dir = _extension_dir()
    listed = [_entry(candidate, best, account_email, ext_dir)
              for candidate in candidates]

    report = {
        "ok": True,
        "profiles": listed,
        # "the one that will be used", which is the question a caller is
        # actually asking. When a pin is in force that is the pinned entry
        # and `pinned` below says so; `reasons` is empty in that case
        # because the ranker was never consulted and inventing a reason for
        # a choice the user made would be a fabrication.
        "recommended": next((entry for entry in listed if entry["recommended"]),
                            None),
        "pinned": selection.pin,
        "reasons": [_redact(reason, account_email)
                    for reason in (best.reasons if best and not selection.pin
                                   else [])],
    }
    if not listed:
        report["note"] = _NOTE_NONE_FOUND
    elif selection.missing:
        report["note"] = (
            f"the browser pinned with gsc_use_browser ({selection.pin}) is "
            f"not among the profiles found on this machine, so no browser "
            f"will be driven at all. Pin one of the profiles listed here, or "
            f"call gsc_use_browser(clear=True)."
        )
    return report


def use_browser(browser: str | None = None, profile: str | None = None,
                clear: bool = False) -> dict:
    """gsc_use_browser's body: override the detector's choice. Never raises.

    The detector ranks what it finds; it cannot know which browser the
    operator keeps their Search Console account in. This is the override,
    and it is written to the config file rather than held in memory because
    an MCP server is respawned at every session start.

    Validated against the profiles that are actually on the machine, at pin
    time, and refused with the real list when it does not match. The
    alternative — accept anything and fail later — turns one typo into a
    browser that silently never opens, and the user has no way to tell a
    misspelling from a bug. The same list is already safe to return: it is
    brand keys and profile directory names, which gsc_detect_browsers
    returns too, and no address or path is among them.
    """
    try:
        return _pin(browser, profile, clear)
    except Exception as exc:  # noqa: BLE001 — the tool must never raise
        return envelopes.unexpected("gsc_use_browser", exc, _FIX_PIN_UNEXPECTED)


def _pin(browser: str | None, profile: str | None, clear: bool) -> dict:
    settings = config.load()
    if clear:
        return _write(settings, None, None, _NOTE_CLEARED)

    if not isinstance(browser, str) or not browser.strip():
        return envelopes.refuse(
            "no_browser_named", "no browser was named",
            "Call gsc_use_browser(browser=\"brave\", profile=\"Default\") "
            "using a browser_key and profile from gsc_detect_browsers, or "
            "gsc_use_browser(clear=True) to go back to the recommendation.")

    browser = browser.strip()
    profile = profile.strip() if isinstance(profile, str) and profile.strip() \
        else None

    candidates = profiles.survey()
    match = target.find(candidates, browser, profile)
    if match is None:
        return envelopes.refuse(
            "browser_not_found",
            f"no profile matching {target.pin_label(browser, profile)} was "
            f"found on this machine",
            f"Choose one of these instead: {_choices(candidates)}. The "
            f"browser is the browser_key from gsc_detect_browsers, not its "
            f"display label.")

    # Stored as the user typed it only after it matched, and normalised to
    # what the detector reports: a pin recorded as "Brave" would read back
    # correctly forever but never equal a brand key in a log line or a
    # config file the user is comparing by eye.
    return _write(settings, match.installed.brand.key, match.profile.directory,
                  _NOTE_PINNED)


def _write(settings: dict, brand_key: str | None, directory: str | None,
           note: str) -> dict:
    """Save the pin, and report back what a human can act on.

    The whole config is written, not a patch: config.save() replaces the
    file, so saving anything less would silently drop every other setting
    the user has.
    """
    settings["browser"] = brand_key
    settings["browser_profile"] = directory
    config.save(settings)
    label = target.pin_label(brand_key, directory) if brand_key else None
    return {"ok": True, "pinned": label, "note": note}


def _choices(candidates: list[profiles.Candidate]) -> str:
    """The pins that would work, as text. No paths, no names, no addresses."""
    if not candidates:
        return "none — no Chromium browser was found on this machine"
    return ", ".join(
        f'browser="{candidate.installed.brand.key}", '
        f'profile="{candidate.profile.directory}"'
        for candidate in candidates)


def _entry(candidate: profiles.Candidate, best: profiles.Candidate | None,
           account_email: str | None, ext_dir: Path | None = None) -> dict:
    """One profile, described without identifying anyone.

    `recommended` is decided by object identity against what recommend()
    returned, not by comparing scores: recommend() copies the candidate
    while scoring it, but carries the same Installed and Profile objects
    through, so identity is exact and needs no second ranking pass.
    """
    profile = candidate.profile
    brand = candidate.installed.brand
    return {
        "browser": brand.label,
        "browser_key": brand.key,
        # Read off the Brand, never rebuilt from the key: Chromium registers
        # no chromium:// scheme and genuinely uses chrome://extensions, so a
        # derived f"{key}://extensions" produces a URL that does not open.
        # Static, per-brand, no privacy cost — and the string Task 8 needs to
        # tell a user where to install the pairing extension. It ships now for
        # the same reason has_extension does: the entries below are a flat
        # list with no browser object above them, so each one has to carry
        # its own brand context, and adding it later would mutate a shape
        # Tasks 9 and 10 already consume.
        "extensions_url": brand.extensions_url,
        "profile": profile.directory,
        "display_name": _safe_name(profile),
        # "an account was found on disk", not "somebody is logged in": for a
        # brand that records nothing, False means only "not discoverable",
        # which is why account_discoverable travels beside it.
        "signed_in": bool(profile.email),
        "account_discoverable": brand.reports_google_account,
        "matches_authorised_account": _matches(profile, brand, account_email),
        # True, False, or None — and None is not a polite False here either.
        # None means the check could not be PERFORMED: either the extension
        # could not be extracted, so there is no directory to match against,
        # or this profile's preferences could not be read. False means it
        # WAS performed — every preferences file consulted was read, and our
        # extension was not among the entries. Collapsing the two would tell
        # a user to reinstall an extension that is sitting right there.
        "has_extension": _has_extension(candidate, ext_dir),
        "recommended": (best is not None
                        and candidate.profile is best.profile
                        and candidate.installed is best.installed),
    }


def _extension_dir() -> Path | None:
    """The extracted extension directory, or None when there isn't one.

    None is what makes ``has_extension`` honestly tri-state: with nothing to
    match a recorded load path against, no profile has been checked, and
    saying "not installed" about all six would be a fabrication. Never
    raises, and logs by exception TYPE NAME — an OSError from the config
    directory carries the operator's account name in its message.
    """
    try:
        return pairing.extension_dir()
    except Exception as exc:  # noqa: BLE001 — the flag is optional, the tool is not
        log.debug("extension directory unavailable (%s)", type(exc).__name__)
        return None


def _has_extension(candidate: profiles.Candidate, ext_dir: Path | None):
    """Is our bridge extension installed in this profile? True/False/None.

    The tri-state is pairing.has_extension's, passed through rather than
    re-derived: an unreadable preferences file must arrive here as None,
    and computing it from find_extension_id's str-or-None would flatten
    that back into False at the last step.

    The ID itself is deliberately NOT returned. It is 32 characters that
    name one person's install, it is of no use to a model choosing a
    profile, and this result goes into a transcript nobody here controls.
    """
    if ext_dir is None:
        return None
    try:
        return pairing.has_extension(candidate.installed, candidate.profile,
                                     ext_dir=ext_dir)
    except Exception as exc:  # noqa: BLE001 — one profile of many
        log.debug("extension check failed (%s)", type(exc).__name__)
        return None


def _matches(profile: profiles.Profile, brand, account_email: str | None):
    """True, False, or None — and None is not a polite False.

    None means the question could not be asked: nobody has authorised yet,
    or the brand records no account so nothing on disk could disagree.
    Collapsing that into False would tell a user their working browser is
    the wrong one.
    """
    if not account_email:
        return None
    if profile.email:
        return profile.email.strip().casefold() == account_email.strip().casefold()
    return False if brand.reports_google_account else None


def _safe_name(profile: profiles.Profile) -> str:
    """The user's label for the profile, unless the label is an address.

    Chrome labels a profile with the signed-in account often enough that
    returning display names unchecked would defeat every other rule here.
    The directory name is the fallback because it is what identifies the
    profile to the launcher anyway.
    """
    name = profile.name
    if not isinstance(name, str) or "@" in name:
        return profile.directory
    return name


def _redact(reason: str, account_email: str | None) -> str:
    """Strip the authorised address out of a reason sentence."""
    if not account_email or account_email not in reason:
        return reason
    return reason.replace(account_email, _REDACTED_ACCOUNT)


def _authorised_email() -> str | None:
    """The address that completed OAuth, if the stored token records one.

    Read here rather than accepted as a parameter so the caller never has
    to name an address to ask which profile to use. The token is Google's
    own response plus whatever the consent flow recorded alongside it; the
    current scope set (webmasters only) returns no identity claim, so this
    is normally None and every match flag is reported as "unknown" rather
    than as a wrong answer.

    Never raises, and never logs the value: an unreadable or malformed
    token costs the match flag, not the tool.
    """
    try:
        token = gauth.load_token()
    except Exception as exc:  # noqa: BLE001 — the flag is optional, the tool is not
        log.debug("authorised account unavailable (%s)", type(exc).__name__)
        return None
    if not isinstance(token, dict):
        return None
    address = token.get("account_email")
    if isinstance(address, str) and address.strip():
        return address.strip()
    return None
