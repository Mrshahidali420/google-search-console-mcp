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

from gsc_core import browsers, pairing, profiles, runlog

log = runlog.get(__name__)


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
    """
    try:
        candidates = profiles.survey()
        best = profiles.recommend(candidates, account_email=account_email)
    except Exception as exc:  # noqa: BLE001 — detection reads other apps' files
        log.debug("no browser could be resolved (%s)", type(exc).__name__)
        return None
    if best is None:
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
