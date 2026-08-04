"""Which browser, which profile, and which extension — resolved once.

Three call sites need this answer (the bridge's pairing check, the wake
poke, and the tools' error messages). Deriving it three times invites the
three to disagree, which is the defect the whole browser layer is shaped to
avoid; profiles.recommend() stays the single source of the ranking, exactly
as it already is for gsc_detect_browsers.

Nothing here launches a browser or writes anything. Resolving is a read of
what is already on the machine, so it is safe to call from a tool that is
only trying to explain why a submission cannot start.
"""
from __future__ import annotations

from dataclasses import dataclass

from gsc_core import browsers, config, pairing, profiles, runlog

log = runlog.get(__name__)


@dataclass(frozen=True)
class Selection:
    """Which profile won, and whether the user or the ranker chose it.

    Three states, and the third is the reason this is a dataclass rather
    than a Candidate-or-None:

      * ``candidate`` set, ``pin`` None — nobody expressed a preference and
        profiles.recommend() ranked the ones it found.
      * ``candidate`` set, ``pin`` set — the user's pin matched a profile
        that is on the machine, and it wins outright. recommend() is not
        consulted at all; a ranking that can override an explicit choice is
        not a preference, it is a suggestion.
      * ``candidate`` None, ``pin`` set — the pinned profile is GONE. This
        must never quietly become "so use the recommended one instead". The
        pin exists because the operator's Search Console account lives in
        that browser and not the other one, so falling back would drive a
        profile signed in as somebody else, and the first anyone would know
        of it is a submission against the wrong property. Callers refuse;
        the doctor explains.
    """

    candidate: profiles.Candidate | None
    pin: str | None

    @property
    def missing(self) -> bool:
        """A pin was set and nothing on this machine matches it."""
        return self.candidate is None and self.pin is not None


def select(candidates: list[profiles.Candidate],
           account_email: str | None = None,
           settings: dict | None = None) -> Selection:
    """The pin if there is one, otherwise the ranking. Never raises.

    Every call site that used to call profiles.recommend() directly goes
    through here instead, for the reason this module already exists: three
    places deriving the same answer is three places that can disagree, and
    a pin honoured by the bridge but ignored by the doctor would produce a
    green check for a browser the server is not driving.

    profiles.py stays a pure ranker and never learns that config exists —
    it is given a list and returns the best of it, which is testable with
    no filesystem at all. Reconciling that ranking with a stored preference
    is a server concern, so it lives on this side of the line.
    """
    settings = config.load() if settings is None else settings
    brand_key = settings.get("browser")
    if not isinstance(brand_key, str) or not brand_key.strip():
        return Selection(profiles.recommend(candidates,
                                            account_email=account_email), None)

    directory = settings.get("browser_profile")
    directory = directory.strip() if isinstance(directory, str) and directory.strip() \
        else None
    return Selection(find(candidates, brand_key.strip(), directory),
                     pin_label(brand_key.strip(), directory))


def pin_label(brand_key: str, directory: str | None) -> str:
    """How a pin is named back to a human.

    The brand KEY, not the label, because when the pin matches nothing there
    is no Installed to read a label off, and a pin that is described one way
    when it works and another way when it breaks is a pin the user cannot
    find in their config file to correct.
    """
    return f"{brand_key} / {directory}" if directory else brand_key


def find(candidates: list[profiles.Candidate], brand_key: str,
          directory: str | None) -> profiles.Candidate | None:
    """The pinned profile among the detected ones, or None.

    Case-folded on both sides: a user typing "Brave" into gsc_use_browser
    means the brand whose key is "brave", and being told their own browser
    does not exist over a capital letter is indefensible.

    With no profile pinned, the FIRST candidate of that brand wins, and
    survey()'s order is what makes that meaningful rather than arbitrary —
    it is Default-first, which is the profile a user who named only a brand
    is asking for.
    """
    wanted = brand_key.casefold()
    slot = directory.casefold() if directory else None
    for candidate in candidates:
        if candidate.installed.brand.key.casefold() != wanted:
            continue
        if slot is None or candidate.profile.directory.casefold() == slot:
            return candidate
    return None


@dataclass(frozen=True)
class Target:
    """The browser profile the bridge should drive.

    Holds `profile`, whose `path` and often whose `name` identify the user
    — the path contains their operating-system account name and Chrome
    labels profiles with the signed-in address more often than not. The
    object is fine to pass around; putting it in a log line or a tool
    response is not. Use describe() for anything a human or a client sees.
    """

    installed: browsers.Installed
    profile: profiles.Profile
    extension_id: str | None


def resolve(account_email: str | None = None) -> Target | None:
    """The browser profile the bridge should drive, or None if there is none.

    extension_id is None when the extension is not installed there yet —
    still a usable target, because answering a pair_request is how it gets
    installed in the first place. It is also None when the lookup could not
    be completed at all: a profile we cannot read is not a reason to refuse
    to drive the browser, it is a reason to fall back to pairing.

    account_email reaches profiles.recommend() and nothing else. It is the
    only way the ranker can prefer the profile that is signed in as the
    user, and it is never logged here.

    A pin that matches nothing returns None too, and deliberately does not
    fall back to the ranking — see Selection. "No target" stops a run with
    an explanation; the wrong target submits somebody else's URLs.
    """
    try:
        selection = select(profiles.survey(), account_email=account_email)
    except Exception as exc:  # noqa: BLE001 — detection reads other apps' files
        log.debug("no browser could be resolved (%s)", type(exc).__name__)
        return None
    best = selection.candidate
    if best is None:
        if selection.missing:
            log.warning("the pinned browser profile was not found; "
                        "run gsc_doctor")
        return None

    try:
        extension_id = pairing.look_up_extension(best.installed,
                                                 best.profile).extension_id
    except Exception as exc:  # noqa: BLE001 — an unreadable profile is not fatal
        log.debug("extension lookup failed (%s)", type(exc).__name__)
        extension_id = None
    return Target(best.installed, best.profile, extension_id)


def describe(target: Target | None) -> dict:
    """The safe half of a target, for a tool response or an error message.

    The path never appears: it contains the user's operating-system account
    name. The profile's display label never appears either unless it is
    demonstrably not an address — Chrome sets that label to the signed-in
    account often enough that passing it through unchecked would defeat
    every other rule. The directory name is the fallback because it is what
    identifies the profile to the launcher anyway.
    """
    if target is None:
        return {"browser": None, "profile": None, "paired": False}
    name = target.profile.name
    safe_name = name if isinstance(name, str) and "@" not in name \
        else target.profile.directory
    return {"browser": target.installed.brand.label,
            "profile": safe_name,
            "paired": target.extension_id is not None}
