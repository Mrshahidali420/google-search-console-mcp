"""Where the bridge extension lives on disk, and what ID Chromium gave it.

Two jobs, and they are the two halves of one handshake.

EXTRACTION. The extension ships as package data inside the wheel
(``gsc_mcp/extension/``). A wheel's contents may live inside a zip, may sit
on a read-only share, and are wiped by the next upgrade — none of which a
browser can load an unpacked extension from. So it is copied out to
``config_dir()/extension``, which is writable, stable, and ours. The copy
is refreshed whenever the packaged manifest's version differs from the
extracted one: after ``pip install --upgrade`` a stale extracted copy would
otherwise keep running last release's extension forever, silently, with the
new server talking to it.

DISCOVERY. Chromium derives an unpacked extension's ID by hashing the
absolute path it was loaded from, then rendering that hash in a base-16
alphabet of ``a``-``p``. The extension deliberately ships with no manifest
``key`` (ruled in 3A), so there is no ID to hardcode and no way to compute
one that is worth trusting: it is read back out of the browser's own
preferences instead, by matching the recorded load path against the
directory we extracted to.

Two consequences fall out of that, and both are deliberate:

  * The ID is stable for as long as the extraction directory is stable, and
    CHANGES if that directory ever moves — an upgrade that relocates the
    config dir, a different GSC_MCP_HOME, a machine migration. Because the
    ID is always read back rather than remembered, a moved directory simply
    stops matching: ``find_extension_id`` returns None, which the caller
    reads as "not paired" and handles by pairing again. There is no state to
    corrupt, and no path by which a stale ID gets returned as a live one.
  * The shape check is not decoration. Chromium's ``extensions.settings``
    mapping carries keys that are not extension IDs at all, and handing a
    non-ID into the pairing handshake would address messages to something
    that does not exist. Anything failing ``EXTENSION_ID_RE`` is skipped.

ROBUSTNESS. These files belong to Chromium, which rewrites them whenever it
pleases. Absent, locked, truncated mid-write, valid JSON with the key
missing — every one of those is an ordinary state, not an error worth
propagating. Nothing here raises.

But they are not all the same ordinary state, and that is why there are two
entry points. ``find_extension_id`` answers "which ID", and its return type
can only conflate "not installed" with "could not check". ``has_extension``
answers the question a user is actually asking, and keeps those apart:
False means the preferences were read and our extension was not among
them; None means the read did not happen — EACCES, a cloud-synced profile
directory, antivirus holding the file open. Telling a user with a working
install to install it again is the one wrong answer worth code to avoid.

PRIVACY. A preferences file here is the same file that holds the operator's
signed-in address, and its path contains their account name on every
supported OS. Nothing in this module logs a path, a value, or an exception
MESSAGE — failures are logged by exception TYPE NAME only, exactly as in
``browsers.py`` and ``profiles.py``. Nothing here writes to stdout.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from importlib.resources import as_file, files
from pathlib import Path
from typing import NamedTuple

from gsc_core import browsers, paths, profiles, runlog

log = runlog.get("pairing")

#: Chromium extension IDs are 32 characters of a 16-letter alphabet: the
#: SHA-256 of the load path, rendered with a..p standing for 0..f.
EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")

_PACKAGE = "gsc_mcp"
_RESOURCE_DIR = "extension"
_EXTRACT_DIR = "extension"
_MANIFEST = "manifest.json"
# Secure Preferences first: Chromium moves extension settings there on the
# platforms that support preference signing, and a machine that has both
# keeps the authoritative copy in the secure file.
_PREFERENCE_FILES = ("Secure Preferences", "Preferences")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extension_dir() -> Path:
    """The extracted, loadable extension directory.

    Idempotent: the same path every time, extracted on the first call and
    left alone afterwards unless the packaged version has moved on.

    Raises ``OSError`` only when the copy itself cannot be made — an
    unwritable config dir is a real failure the caller must be able to
    tell apart from "the extension is not installed in this profile", so
    it is not swallowed here. Every caller in this package guards it.
    """
    target = paths.config_dir() / _EXTRACT_DIR
    with as_file(files(_PACKAGE) / _RESOURCE_DIR) as source:
        source_path = Path(source)
        if _is_current(source_path, target):
            return target
        _replace(source_path, target)
    return target


def extension_version(ext_dir: Path | str) -> str | None:
    """The version of the extension sitting in ``ext_dir``, or None.

    Public because a diagnostic has to compare this against the version the
    browser recorded when it loaded that same directory, and reading the
    manifest a second time in a second module would put two spellings of
    "what counts as a usable version" in the codebase. Never raises: an
    absent or unreadable manifest is None, which callers read as "cannot
    compare".
    """
    return _manifest_version(Path(ext_dir) / _MANIFEST)


def _is_current(source: Path, target: Path) -> bool:
    """Is the extracted copy present and built from this package version?

    A missing or unreadable extracted manifest counts as stale, so a
    half-written extraction from an interrupted upgrade is redone rather
    than trusted.
    """
    if not (target / _MANIFEST).is_file():
        return False
    extracted = _manifest_version(target / _MANIFEST)
    if extracted is None:
        return False
    return extracted == _manifest_version(source / _MANIFEST)


def _manifest_version(path: Path) -> str | None:
    """``version`` from a manifest, or None when it cannot be established.

    None never compares equal to anything, including another None, because
    ``_is_current`` rejects a None extracted version before comparing. That
    is the safe direction: an unreadable manifest re-extracts.
    """
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        log.debug("extension manifest unreadable (%s)", type(exc).__name__)
        return None
    if not isinstance(manifest, dict):
        return None
    version = manifest.get("version")
    return version if isinstance(version, str) and version.strip() else None


def _replace(source: Path, target: Path) -> None:
    """Copy the packaged extension over whatever is there now.

    The old directory goes first rather than being merged into: a file
    dropped between releases would otherwise stay loaded forever, and
    Chromium reads every file the manifest names, not just the new ones.
    """
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Lookup:
    """What one profile had to say, and whether it managed to say it.

    ``complete`` is the difference between "we looked and it is not there"
    and "we could not look". Collapsing the two tells a user with the
    extension correctly installed that it is missing, which is the one
    wrong answer this whole module exists to avoid.

    ``version`` is the version Chromium recorded for the entry that
    matched, and it is NOT the same fact as the extracted copy's version.
    Chromium snapshots the manifest it read at load time; a later
    ``pip install --upgrade`` refreshes the extracted directory underneath
    it and the browser keeps running the old code until someone clicks
    Reload. The gap between the two numbers is the only way to see that
    from outside the browser. None when nothing matched, or when the entry
    recorded no usable version — an absent number is not a mismatch, and
    must not be reported as one.
    """

    extension_id: str | None
    complete: bool
    version: str | None = None


def look_up_extension(installed: browsers.Installed,
                      profile: profiles.Profile,
                      *, ext_dir: Path | str | None = None) -> Lookup:
    """Search one profile for our extension. Never raises.

    ``complete`` is False when the answer rests on something we could not
    read: the extraction directory could not be resolved, the profile
    directory could not be worked out, or a preferences file existed and
    could not be read or parsed. A preferences file that is simply ABSENT
    does not spoil it — a profile that has never recorded any extension is
    a complete answer of "no".

    A hit always reports ``complete=True``: an ID that was found is found,
    whatever the other file did.
    """
    try:
        wanted = _key(Path(ext_dir) if ext_dir is not None else extension_dir())
    except Exception as exc:  # noqa: BLE001 — no directory, nothing to match
        log.debug("extension directory unavailable (%s)", type(exc).__name__)
        return Lookup(None, False)

    try:
        root = _profile_dir(installed, profile)
    except Exception as exc:  # noqa: BLE001 — one profile of many
        log.debug("profile directory unusable (%s)", type(exc).__name__)
        return Lookup(None, False)

    complete = True
    for filename in _PREFERENCE_FILES:
        found, version, readable = _match_in(root / filename, root, wanted)
        if found is not None:
            return Lookup(found, True, version)
        complete = complete and readable
    return Lookup(None, complete)


def find_extension_id(installed: browsers.Installed,
                      profile: profiles.Profile,
                      *, ext_dir: Path | str | None = None) -> str | None:
    """The ID Chromium assigned our extension in this profile, or None.

    Never raises. None is the ordinary answer, and it means exactly one
    thing to the caller: not paired here, pair again. It covers the
    extension not being installed, the profile never having been launched,
    the preferences file being unreadable, and the extraction directory
    having moved since the last pairing.

    A caller that has to TELL the user which of those happened wants
    ``has_extension`` instead — "not installed" and "could not check" lead
    to different next steps, and only this function's return type is
    obliged to conflate them.

    ``ext_dir`` overrides the directory to match against; it exists so a
    caller resolving the extraction once for a whole survey does not pay
    for it per profile.
    """
    return look_up_extension(installed, profile, ext_dir=ext_dir).extension_id


def has_extension(installed: browsers.Installed, profile: profiles.Profile,
                  *, ext_dir: Path | str | None = None) -> bool | None:
    """Is our extension installed in this profile? True, False, or None.

    None is not a polite False. It means the question could not be
    answered — an unreadable preferences file, most often EACCES, a
    cloud-synced profile directory, or antivirus holding the file open.
    Reporting False there would tell a user whose extension is correctly
    installed to install it again.

    Never raises.
    """
    result = look_up_extension(installed, profile, ext_dir=ext_dir)
    if result.extension_id is not None:
        return True
    return False if result.complete else None


def _profile_dir(installed: browsers.Installed,
                 profile: profiles.Profile) -> Path:
    """Where this profile's state files live.

    ``Profile.path`` is authoritative when set; the user-data-dir join is
    the fallback for a Profile built by hand. Note ``user_data_dir`` is a
    str, not a Path.
    """
    path = getattr(profile, "path", None)
    if isinstance(path, str) and path.strip():
        return Path(path)
    return Path(installed.user_data_dir) / profile.directory


def _match_in(prefs_path: Path, root: Path,
              wanted: str) -> tuple[str | None, str | None, bool]:
    """The first ID in this file whose recorded path is ours, the version
    recorded alongside it, and whether the file could be read at all.

    Iteration is sorted so that a preferences file listing two entries
    pointing at the same directory — which happens when a browser has been
    told to load the same unpacked extension twice — always yields the same
    answer, rather than one that depends on dict insertion order.
    """
    prefs, readable = _read_json(prefs_path)
    settings = _extension_settings(prefs)
    for ext_id in sorted(settings):
        if not EXTENSION_ID_RE.match(ext_id):
            continue
        entry = settings[ext_id]
        recorded = _recorded_path(entry, root)
        if recorded is not None and recorded == wanted:
            return ext_id, _recorded_version(entry), readable
    return None, None, readable


def _recorded_version(entry: object) -> str | None:
    """The version from the manifest Chromium snapshotted at load time.

    Guarded the same way everything else here is: this is a browser state
    file, and a chained subscript on one is a TypeError waiting for the
    first user whose profile is mid-migration. An entry with no usable
    version yields None, which callers must treat as "cannot compare",
    never as "different".
    """
    if not isinstance(entry, dict):
        return None
    manifest = entry.get("manifest")
    if not isinstance(manifest, dict):
        return None
    version = manifest.get("version")
    return version if isinstance(version, str) and version.strip() else None


def _extension_settings(prefs: object) -> dict[str, object]:
    """``extensions.settings``, or an empty mapping.

    Guarded at every step: a chained subscript on a browser state file is
    a TypeError waiting for the first user whose profile is mid-migration.
    """
    if not isinstance(prefs, dict):
        return {}
    section = prefs.get("extensions")
    if not isinstance(section, dict):
        return {}
    settings = section.get("settings")
    if not isinstance(settings, dict):
        return {}
    return {key: value for key, value in settings.items()
            if isinstance(key, str)}


def _recorded_path(entry: object, root: Path) -> str | None:
    """The comparison key for one entry's ``path``, or None.

    Chromium records an absolute path for an unpacked extension and a path
    relative to the profile directory for one installed from the store, so
    a relative value is joined onto the profile before comparing rather
    than onto the process's working directory.
    """
    if not isinstance(entry, dict):
        return None
    path = entry.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return _key(candidate)


def _key(path: Path) -> str:
    """A comparison key that survives case and separator differences.

    Chromium writes back whatever spelling it was given, and that can
    differ from ours in case, in separator, in trailing slash and in
    redundant segments for the same directory. ``Path`` has already
    settled the separators and the trailing slash by the time the value
    arrives here; ``normpath`` collapses the ``..`` segments it leaves
    alone. The casefold is done explicitly rather than through
    ``os.path.normcase`` so that it applies on every platform — a match
    that worked on the developer's machine and not on the user's is the
    failure this exists to prevent.
    """
    return os.path.normpath(str(path)).casefold()


# ---------------------------------------------------------------------------
# Guarded reads
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> tuple[object | None, bool]:
    """Parse a browser state file; also say whether that succeeded.

    The flag distinguishes the two kinds of nothing. A file that is not
    there is a complete answer — the browser has recorded no extensions —
    and reports True. A file that exists and cannot be read or parsed is
    an incomplete answer and reports False, so the caller can say "could
    not check" rather than "not installed".

    Logged by exception TYPE NAME: an OSError's message carries the path,
    and that path carries the operator's account name.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return None, True
    except OSError as exc:
        log.debug("preferences unreadable (%s)", type(exc).__name__)
        return None, False
    try:
        return json.loads(text), True
    except (ValueError, RecursionError) as exc:
        log.debug("preferences unparsable (%s)", type(exc).__name__)
        return None, False


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

class PairCode(Enum):
    """Which rule decided a pair_request. A CLOSED vocabulary, on purpose.

    The prose ``reason`` cannot be logged: its commonest form interpolates
    ``extension_dir()``, an absolute path carrying the operator's account
    name, and the bridge's logger writes to a file that outlives the
    session and gets pasted into bug reports. Redacting the prose was
    rejected — a redaction pattern would have to anticipate every string
    this module can return — so the log gets a code instead.

    An ``Enum`` rather than a string constant because the property that
    makes this safe is that a code cannot be BUILT. Nothing can format a
    path into a member, and a caller inventing one gets a ValueError from
    ``PairCode(...)`` rather than a leak. Adding a denial branch means
    adding a member here, which is exactly the review moment wanted.

    Values are the wire/log spelling and are pinned by test: a log line is
    read by people and by grep, and renaming one silently is a breakage.
    """

    OK = "ok"
    #: The bridge was started without a browser target — run gsc_pair.
    NO_TARGET = "no_target"
    #: The claim is not shaped like a Chromium extension ID at all.
    MALFORMED_ID = "malformed_id"
    #: The connection's Origin disagrees with the ID it claims to be.
    BAD_ORIGIN = "bad_origin"
    #: Nothing in this profile is loaded from our extraction directory —
    #: Chrome is pointed at some other unpacked directory, or at none.
    DIR_MISMATCH = "dir_mismatch"
    #: Something IS loaded from our directory, under a different ID: the
    #: extension was reloaded from elsewhere and Chromium rehashed it.
    ID_MISMATCH = "id_mismatch"


class Verdict(NamedTuple):
    """The answer to one pair_request, in the two forms it is needed in.

    ``reason`` goes on the wire to the extension, which shows it to the
    user who already owns the path inside it. ``code`` is the only half
    that may be logged. Keeping them on one object is what stops a future
    caller from logging the wrong one by reflex.
    """

    allowed: bool
    reason: str
    code: PairCode


def verify_pair_request(installed: browsers.Installed,
                        profile: profiles.Profile,
                        claimed_id: str,
                        origin: str | None) -> Verdict:
    """May the bridge hand its token to a client claiming to be our extension?

    Returns a ``Verdict``: the reason is shown to the user either way, so a
    refusal always says what to do about it, and the code says which rule
    fired in a form that is safe to write to a log file.

    This is NOT trust-on-first-use. The browser profile records the ID
    Chromium assigned to the unpacked extension loaded from OUR directory,
    so the claim is checked against a fact we can read rather than against
    whoever knocked first. A local process wanting the token would already
    need read access to the browser profile — at which point it holds the
    Google session cookies too, and the token buys it nothing.
    """
    if not EXTENSION_ID_RE.match(claimed_id or ""):
        return Verdict(False, "malformed extension id", PairCode.MALFORMED_ID)
    if origin and origin != f"chrome-extension://{claimed_id}":
        return Verdict(
            False,
            f"Origin {origin} does not match the claimed extension id",
            PairCode.BAD_ORIGIN,
        )

    real = look_up_extension(installed, profile).extension_id
    if not real:
        return Verdict(
            False,
            f"no extension is loaded from {extension_dir()} in "
            f"{installed.brand.label} — open {installed.brand.extensions_url}, "
            "turn on Developer mode, and Load unpacked from that directory",
            PairCode.DIR_MISMATCH,
        )
    if real != claimed_id:
        return Verdict(
            False,
            f"that is not the extension installed from {extension_dir()} "
            f"(this browser profile has {real})",
            PairCode.ID_MISMATCH,
        )
    return Verdict(True, "verified against the browser profile", PairCode.OK)


def wake_url(ext_id: str) -> str:
    """A page inside the extension. Opening it starts an evicted MV3 service
    worker, which is how we ask a sleeping extension to come and connect."""
    return f"chrome-extension://{ext_id}/connect.html"


def wake(installed: browsers.Installed, profile: profiles.Profile) -> dict:
    """Nudge the extension awake by opening one of its own pages. Never raises.

    MV3 kills an idle service worker after ~30s and the cheapest legal way
    back is an alarm, whose floor is also 30s. That gap is the whole delay
    between "the bridge started listening" and "the extension noticed".
    Opening an extension page boots the worker at once; connect.js closes
    the tab again, so the poke leaves nothing behind.
    """
    result: dict = {"ok": False, "browser": installed.brand.label}
    ext_id = look_up_extension(installed, profile).extension_id
    if not ext_id:
        result["hint"] = f"extension not loaded in {installed.brand.label}"
        return result
    result["extension_id"] = ext_id
    result["url"] = wake_url(ext_id)
    try:
        subprocess.Popen(
            [installed.exe_path, result["url"]],
            creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                           | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
            close_fds=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 — a failed poke is never fatal
        # The exception TYPE only: an OSError message carries a filesystem
        # path containing the user's account name, and this hint is shown.
        log.warning("could not wake the extension (%s)", type(exc).__name__)
        result["hint"] = (f"could not launch {installed.brand.label} "
                          f"({type(exc).__name__})")
        return result
    result["ok"] = True
    return result
