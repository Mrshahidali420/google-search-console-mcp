"""Which profiles a browser has, and which Google account is in each.

Layer 2 of three. browsers.py is Layer 1 and hands this module a resolved
``Installed``; the recommendation layer consumes what this module returns.

PRIVACY. This module reads the signed-in account address out of the
browser's own profile files so the tool can tell the user which profile to
use. That read is entirely local: the address is returned to the caller in
memory, it is never transmitted anywhere, never written to disk by this
module, and never logged. The same statement appears in the README and in
the privacy policy that OAuth verification requires.

Consequently every failure here is logged by exception TYPE NAME only. An
OSError carries the path it failed on — and that path contains both the
operator's account name and the profile they use. A parse error carries a
position today, but the file it came from is one holding the user's email
address, and no future exception message may be assumed safe to print.
Neither may reach a log line at any level, and nothing here writes to
stdout.

Every one of these files can be absent, truncated, invalid JSON, valid
JSON of the wrong shape, or simply missing the key. None of that is
exceptional: they are all ordinary states on a real machine, and each has
to produce a usable "unknown" rather than an error. So each read is guarded
individually — one unreadable profile must not lose the others, exactly as
one undetectable brand must not hide the other five in Layer 1.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from gsc_core import browsers, runlog

log = runlog.get("profiles")

# Preferences first, Secure Preferences as the fallback: which file holds
# account_info varies by brand and by version, so both are always tried.
_PREFERENCE_FILES = ("Preferences", "Secure Preferences")
_LOCAL_STATE = "Local State"
_DEFAULT_DIR = "Default"
_PROFILE_PREFIX = "Profile "


@dataclass(frozen=True)
class Profile:
    """One profile inside one installed browser."""

    directory: str        # on-disk directory name, e.g. "Profile 2"
    name: str             # display name the user set, e.g. "Work"
    email: str | None     # signed-in Google account, None when unknown
    path: str             # absolute path to the profile directory


def list_profiles(installed: browsers.Installed) -> list[Profile]:
    """Every profile in this browser, in a stable order.

    Never raises. A browser that has never been launched, whose Local State
    is truncated, or whose user-data directory is gone all return an empty
    list — that is "nothing to offer", not an error.

    The order is deterministic and independent of the order the browser
    happened to write into Local State, because the ranking layer above
    consumes this list and a flapping order would make its recommendation
    irreproducible between runs.
    """
    try:
        root = Path(installed.user_data_dir)
        entries = _read_info_cache(root)
    except Exception as exc:  # noqa: BLE001 — enumeration is best-effort
        log.debug("profile enumeration failed (%s)", type(exc).__name__)
        return []

    found: list[Profile] = []
    for directory in sorted(entries, key=_profile_sort_key):
        try:
            profile = _build_profile(root, directory, entries[directory])
        except Exception as exc:  # noqa: BLE001 — one profile of many
            log.debug("profile skipped (%s)", type(exc).__name__)
            continue
        if profile is not None:
            found.append(profile)
    return found


# ---------------------------------------------------------------------------
# Local State
# ---------------------------------------------------------------------------

def _read_info_cache(root: Path) -> dict[str, object]:
    """``profile.info_cache`` from Local State, or an empty mapping.

    Keys are directory names such as ``Default`` or ``Profile 2``. Anything
    that is not a mapping of string keys is discarded rather than trusted:
    a list here would turn a chained subscript into a TypeError deep in the
    caller, which is the failure this module exists to avoid.
    """
    state = _read_json(root / _LOCAL_STATE)
    if not isinstance(state, dict):
        return {}
    section = state.get("profile")
    if not isinstance(section, dict):
        return {}
    cache = section.get("info_cache")
    if not isinstance(cache, dict):
        return {}
    return {key: value for key, value in cache.items() if isinstance(key, str)}


def _build_profile(root: Path, directory: str, entry: object) -> Profile | None:
    """One Profile, or None when the directory is not actually on disk.

    A profile listed in Local State but absent from disk is skipped: the
    browser lists profiles it has forgotten to clean up, and offering the
    user a profile that cannot be launched is worse than omitting it.
    """
    path = root / directory
    if not path.is_dir():
        return None
    return Profile(directory=directory,
                   name=_display_name(entry, directory),
                   email=_read_email(path),
                   path=str(path))


def _display_name(entry: object, directory: str) -> str:
    """The user's label for this profile, falling back to its directory."""
    if isinstance(entry, dict):
        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            return name
    return directory


def _profile_sort_key(directory: str) -> tuple[int, int, str]:
    """Default first, then Profile N numerically, then anything else.

    Sorting the raw strings would put "Profile 10" before "Profile 2",
    which reads as a bug to anyone with ten profiles.
    """
    if directory == _DEFAULT_DIR:
        return (0, 0, "")
    if directory.startswith(_PROFILE_PREFIX):
        suffix = directory[len(_PROFILE_PREFIX):]
        if suffix.isdigit():
            return (1, int(suffix), directory)
    return (2, 0, directory)


# ---------------------------------------------------------------------------
# The signed-in account
# ---------------------------------------------------------------------------

def _read_email(profile_dir: Path) -> str | None:
    """The signed-in Google address for this profile, or None.

    None is the common case, not a failure: a profile nobody has signed
    into carries an empty ``account_info``, and so does one whose
    preferences file this process cannot read.
    """
    for filename in _PREFERENCE_FILES:
        email = _email_from_prefs(_read_json(profile_dir / filename))
        if email:
            return email
    return None


def _email_from_prefs(prefs: object) -> str | None:
    """``account_info[].email``, defended at every step.

    The first entry carrying a usable address wins. Indexing entry zero
    outright would return None for the real account whenever Chrome has
    written a leading entry with no email on it.
    """
    if not isinstance(prefs, dict):
        return None
    accounts = prefs.get("account_info")
    if not isinstance(accounts, list):
        return None
    for account in accounts:
        if not isinstance(account, dict):
            continue
        email = account.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()
    return None


# ---------------------------------------------------------------------------
# Guarded reads
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> object | None:
    """Parse a browser state file, or return None for any reason at all.

    Absent, truncated, invalid, or unreadable are all the same answer here:
    "nothing known". The exception is logged by TYPE NAME because its
    message would carry either the file's contents or its path.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        log.debug("state file unreadable (%s)", type(exc).__name__)
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        log.debug("state file unparsable (%s)", type(exc).__name__)
        return None
