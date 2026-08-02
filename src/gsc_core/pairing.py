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
propagating, and each yields None. ``find_extension_id`` never raises.

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
from importlib.resources import as_file, files
from pathlib import Path

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

def find_extension_id(installed: browsers.Installed,
                      profile: profiles.Profile,
                      *, ext_dir: Path | str | None = None) -> str | None:
    """The ID Chromium assigned our extension in this profile, or None.

    Never raises. None is the ordinary answer, and it means exactly one
    thing to the caller: not paired here, pair again. It covers the
    extension not being installed, the profile never having been launched,
    the preferences file being unreadable, and the extraction directory
    having moved since the last pairing — none of which are distinguishable
    from outside, and none of which need to be.

    ``ext_dir`` overrides the directory to match against; it exists so a
    caller resolving the extraction once for a whole survey does not pay
    for it per profile.
    """
    try:
        wanted = _key(Path(ext_dir) if ext_dir is not None else extension_dir())
    except Exception as exc:  # noqa: BLE001 — no directory, nothing to match
        log.debug("extension directory unavailable (%s)", type(exc).__name__)
        return None

    try:
        root = _profile_dir(installed, profile)
    except Exception as exc:  # noqa: BLE001 — one profile of many
        log.debug("profile directory unusable (%s)", type(exc).__name__)
        return None

    for filename in _PREFERENCE_FILES:
        found = _match_in(root / filename, root, wanted)
        if found is not None:
            return found
    return None


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


def _match_in(prefs_path: Path, root: Path, wanted: str) -> str | None:
    """The first ID in this file whose recorded path is ours.

    Iteration is sorted so that a preferences file listing two entries
    pointing at the same directory — which happens when a browser has been
    told to load the same unpacked extension twice — always yields the same
    answer, rather than one that depends on dict insertion order.
    """
    settings = _extension_settings(_read_json(prefs_path))
    for ext_id in sorted(settings):
        if not EXTENSION_ID_RE.match(ext_id):
            continue
        recorded = _recorded_path(settings[ext_id], root)
        if recorded is not None and recorded == wanted:
            return ext_id
    return None


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

def _read_json(path: Path) -> object | None:
    """Parse a browser state file, or return None for any reason at all.

    Logged by exception TYPE NAME: an OSError's message carries the path,
    and that path carries the operator's account name.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        log.debug("preferences unreadable (%s)", type(exc).__name__)
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as exc:
        log.debug("preferences unparsable (%s)", type(exc).__name__)
        return None
