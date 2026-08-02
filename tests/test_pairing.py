"""Extraction of the bridge extension, and read-back of its installed ID.

The ID cannot be computed: Chromium hashes the absolute load path, and the
extension ships with no manifest key on purpose. So every assertion here is
about reading the browser's own answer back correctly, and about refusing
to return anything that is not one.

The IDs below are synthetic runs of a single letter. A real extension ID
names one person's install and does not belong in a public repository —
the package scrub test enforces that for the extension directory, and the
same rule applies to fixtures.
"""
from __future__ import annotations

import json
import logging
import re

from gsc_core import browsers, pairing, profiles

VALID_ID = "a" * 32          # matches ^[a-p]{32}$
OTHER_ID = "b" * 32
# For the ordering tests only: a well-shaped id that sorts BEFORE the one
# we expect back. "a" * 32 is the smallest id there is, so a test using it
# as the answer cannot tell "matched on the path" apart from "returned the
# first id it saw".
LOW_ID = "a" * 32
HIGH_ID = "p" * 32


def _profile_with_extensions(tmp_path, settings, filename="Secure Preferences"):
    root = tmp_path / "User Data"
    pdir = root / "Default"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / filename).write_text(
        json.dumps({"extensions": {"settings": settings}}), encoding="utf-8")
    installed = browsers.Installed(brand=browsers.BRANDS["chrome"],
                                   exe_path="/nonexistent",
                                   user_data_dir=str(root))
    profile = profiles.Profile(directory="Default", name="Personal",
                               email=None, path=str(pdir))
    return installed, profile


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extension_dir_extracts_a_real_loadable_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    result = pairing.extension_dir()
    assert result.is_dir()
    assert (result / "manifest.json").is_file()


def test_extension_dir_extracts_every_packaged_file(tmp_path, monkeypatch):
    """A manifest alone is not loadable. Chromium reads every file the
    manifest names, so a copy that stopped at the manifest would produce an
    extension that installs and then does nothing."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    result = pairing.extension_dir()
    names = {path.name for path in result.iterdir()}
    assert {"manifest.json", "background.js", "content.js", "rpc-main.js",
            "connect.html", "connect.js", "options.html", "options.js",
            "popup.html", "popup.js"} <= names


def test_extension_dir_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    assert pairing.extension_dir() == pairing.extension_dir()


def test_extension_dir_leaves_a_current_extraction_alone(tmp_path, monkeypatch):
    """Re-extracting on every call would rewrite the directory Chromium
    hashed the ID from while the browser has it loaded."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    marker = pairing.extension_dir() / "background.js"
    stamp = marker.stat().st_mtime_ns
    marker.write_text("// locally edited\n", encoding="utf-8")
    pairing.extension_dir()
    assert marker.read_text(encoding="utf-8") == "// locally edited\n"
    assert stamp  # the file existed before the second call


def test_extension_dir_refreshes_when_the_packaged_version_moves_on(
        tmp_path, monkeypatch):
    """After `pip install --upgrade`, a stale extracted copy would keep
    last release's extension running against the new server, silently."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    extracted = pairing.extension_dir()
    manifest = json.loads((extracted / "manifest.json").read_text(
        encoding="utf-8"))
    manifest["version"] = "0.0.1"
    (extracted / "manifest.json").write_text(json.dumps(manifest),
                                             encoding="utf-8")
    (extracted / "background.js").write_text("// stale\n", encoding="utf-8")
    # A file the previous release shipped and this one dropped. Merging the
    # new copy over the old directory would leave it loaded forever.
    (extracted / "removed-last-release.js").write_text("// gone\n",
                                                       encoding="utf-8")

    refreshed = pairing.extension_dir()
    assert json.loads((refreshed / "manifest.json").read_text(
        encoding="utf-8"))["version"] != "0.0.1"
    assert refreshed.joinpath("background.js").read_text(
        encoding="utf-8") != "// stale\n"
    assert not (refreshed / "removed-last-release.js").exists()


def test_extension_dir_re_extracts_a_truncated_manifest(tmp_path, monkeypatch):
    """An upgrade interrupted mid-copy leaves an unreadable manifest. That
    is stale, not current: trusting it would leave a half-extracted
    extension in place forever."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    extracted = pairing.extension_dir()
    (extracted / "manifest.json").write_text("{not json", encoding="utf-8")
    manifest = json.loads((pairing.extension_dir() / "manifest.json")
                          .read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_finds_the_id_whose_path_matches_our_extension(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(tmp_path, {
        OTHER_ID: {"path": str(tmp_path / "some-other-extension")},
        VALID_ID: {"path": str(ours)}})
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_the_path_decides_the_match_not_the_order_of_the_ids(
        tmp_path, monkeypatch):
    """The one that matches wins even when another well-shaped id sorts
    ahead of it. Without this, "return the first id that looks like an id"
    passes the test above by coincidence and pairs against a stranger's
    extension."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(tmp_path, {
        LOW_ID: {"path": str(tmp_path / "some-other-extension")},
        HIGH_ID: {"path": str(ours)}})
    assert pairing.find_extension_id(installed, profile) == HIGH_ID


def test_two_entries_for_the_same_directory_answer_the_same_way_every_time(
        tmp_path, monkeypatch):
    """A browser told to load the same unpacked directory twice records two
    ids for it. Either is arguably right; answering differently between two
    identical runs is not, because the id is what every later message is
    addressed to."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {HIGH_ID: {"path": str(ours)}, LOW_ID: {"path": str(ours)}})
    assert pairing.find_extension_id(installed, profile) == LOW_ID


def test_path_matching_ignores_case_and_separators(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    # Both dimensions at once, and the separator swap is a no-op on POSIX
    # where a backslash is a legal filename character rather than a
    # separator — so the assertion means the same thing on every platform.
    spelling = str(ours).upper().replace("\\", "/")
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": spelling}})
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_a_trailing_separator_is_still_the_same_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours) + "/"}})
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_a_redundant_segment_is_still_the_same_directory(tmp_path, monkeypatch):
    """A browser launched with --load-extension records the spelling it was
    handed, and a launcher script composing one from a parent directory
    produces exactly this. Two spellings of one directory must not read as
    two extensions."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours / ".." / ours.name)}})
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_falls_back_to_plain_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours)}}, filename="Preferences")
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_returns_none_when_our_extension_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(
        tmp_path, {OTHER_ID: {"path": str(tmp_path / "elsewhere")}})
    assert pairing.find_extension_id(installed, profile) is None


def test_an_id_of_the_wrong_shape_is_never_returned(tmp_path, monkeypatch):
    """Chromium IDs are [a-p]{32}. Anything else in that dict is not one,
    and returning it would send a bogus id into the pairing handshake."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {"not-a-valid-id": {"path": str(ours)}})
    assert pairing.find_extension_id(installed, profile) is None


def test_an_id_outside_the_a_to_p_alphabet_is_never_returned(
        tmp_path, monkeypatch):
    """Thirty-two characters is not enough to be an extension id. The
    alphabet stops at p, because the id is hexadecimal with a..p standing
    for 0..f, and a 32-char hex digest sitting in that dict is not one."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {"z" * 32: {"path": str(ours)}})
    assert pairing.find_extension_id(installed, profile) is None


def test_an_id_of_the_wrong_length_is_never_returned(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {"a" * 31: {"path": str(ours)},
                   "a" * 33: {"path": str(ours)}})
    assert pairing.find_extension_id(installed, profile) is None


def test_a_moved_extraction_directory_is_a_re_pair_not_a_wrong_match(
        tmp_path, monkeypatch):
    """The extension carries no manifest key, so its id is a hash of the
    directory it was loaded from. If that directory moves — an upgrade
    relocating the config dir — the recorded id no longer describes our
    extension. It must read as "not paired", never as a match."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    old_home = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(old_home)}})

    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path / "relocated"))
    assert pairing.extension_dir() != old_home
    assert pairing.find_extension_id(installed, profile) is None


def test_an_explicit_ext_dir_overrides_the_extraction(tmp_path, monkeypatch):
    """The survey resolves the directory once and passes it down; that path
    has to be the one actually matched against, not a second extraction."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    elsewhere = tmp_path / "loaded-from-here"
    elsewhere.mkdir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(elsewhere)}})
    assert pairing.find_extension_id(installed, profile) is None
    assert pairing.find_extension_id(installed, profile,
                                     ext_dir=elsewhere) == VALID_ID


# ---------------------------------------------------------------------------
# Everything Chromium can do to a file it owns
# ---------------------------------------------------------------------------

def test_a_corrupt_preferences_file_yields_none_rather_than_raising(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(tmp_path, {})
    (tmp_path / "User Data" / "Default" / "Secure Preferences").write_text(
        "{not json", encoding="utf-8")
    assert pairing.find_extension_id(installed, profile) is None


def test_a_corrupt_secure_file_does_not_hide_the_plain_one(
        tmp_path, monkeypatch):
    """Both files are read. A browser caught mid-write on one of them must
    not cost the answer the other one holds."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours)}}, filename="Preferences")
    (tmp_path / "User Data" / "Default" / "Secure Preferences").write_text(
        "{not json", encoding="utf-8")
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_a_profile_that_has_never_been_launched_yields_none(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed = browsers.Installed(brand=browsers.BRANDS["chrome"],
                                   exe_path="/nonexistent",
                                   user_data_dir=str(tmp_path / "User Data"))
    profile = profiles.Profile(directory="Default", name="Personal",
                               email=None, path=str(tmp_path / "nope"))
    assert pairing.find_extension_id(installed, profile) is None


def test_preferences_of_an_unexpected_shape_yield_none(tmp_path, monkeypatch):
    """Valid JSON with the keys somewhere else, or of the wrong type. Every
    one of these is a chained-subscript TypeError if it is not guarded."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    root = tmp_path / "User Data"
    pdir = root / "Default"
    pdir.mkdir(parents=True)
    installed = browsers.Installed(brand=browsers.BRANDS["chrome"],
                                   exe_path="/nonexistent",
                                   user_data_dir=str(root))
    profile = profiles.Profile(directory="Default", name="Personal",
                               email=None, path=str(pdir))
    ours = str(pairing.extension_dir())
    for payload in ([], {"extensions": []}, {"extensions": {"settings": []}},
                    {"extensions": {"settings": {VALID_ID: "not a dict"}}},
                    {"extensions": {"settings": {VALID_ID: {}}}},
                    {"extensions": {"settings": {VALID_ID: {"path": None}}}},
                    {"extensions": {"settings": {VALID_ID: {"path": ""}}}}):
        (pdir / "Secure Preferences").write_text(json.dumps(payload),
                                                 encoding="utf-8")
        assert pairing.find_extension_id(installed, profile) is None, payload
    # ...and the same profile still answers once a real entry appears, so
    # the loop above is not passing because the fixture is broken.
    (pdir / "Secure Preferences").write_text(
        json.dumps({"extensions": {"settings": {VALID_ID: {"path": ours}}}}),
        encoding="utf-8")
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_a_relative_path_is_resolved_against_the_profile(tmp_path, monkeypatch):
    """Chromium records store-installed extensions relative to the profile.
    Resolving those against the process's working directory would compare
    two unrelated paths and could match by accident."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    root = tmp_path / "User Data"
    pdir = root / "Default"
    pdir.mkdir(parents=True)
    installed = browsers.Installed(brand=browsers.BRANDS["chrome"],
                                   exe_path="/nonexistent",
                                   user_data_dir=str(root))
    profile = profiles.Profile(directory="Default", name="Personal",
                               email=None, path=str(pdir))
    (pdir / "Secure Preferences").write_text(json.dumps(
        {"extensions": {"settings": {VALID_ID: {"path": "Extensions/x/1.0"}}}}),
        encoding="utf-8")
    assert pairing.find_extension_id(installed, profile) is None
    assert pairing.find_extension_id(
        installed, profile, ext_dir=pdir / "Extensions" / "x" / "1.0") == VALID_ID


def test_the_user_data_dir_is_used_when_the_profile_carries_no_path(
        tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, _ = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours)}})
    profile = profiles.Profile(directory="Default", name="Personal",
                               email=None, path="")
    assert pairing.find_extension_id(installed, profile) == VALID_ID


def test_nothing_is_logged_that_could_identify_anyone(tmp_path, monkeypatch):
    """The preferences file this reads is the one holding the operator's
    address, and its path holds their account name. Failures are logged by
    exception type name only.

    Captured with a handler attached straight to this module's logger
    rather than through caplog. runlog gives the package logger its own
    handlers and sets propagate = False, so whether pytest's capture sees
    anything here depends on which OTHER test file ran init() first — and a
    privacy assertion that quietly runs against an empty buffer is worse
    than no privacy assertion at all.
    """
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(tmp_path, {})
    (tmp_path / "User Data" / "Default" / "Secure Preferences").write_text(
        "{not json", encoding="utf-8")

    records = []
    handler = logging.Handler(level=logging.DEBUG)
    handler.emit = records.append
    previous = pairing.log.level
    pairing.log.setLevel(logging.DEBUG)
    pairing.log.addHandler(handler)
    try:
        pairing.find_extension_id(installed, profile)
    finally:
        pairing.log.removeHandler(handler)
        pairing.log.setLevel(previous)

    # Load-bearing: it proves capture is live, so the assertions below are
    # not passing over an empty list.
    assert records, "the corrupt file should have been logged at all"
    blob = "\n".join(record.getMessage() for record in records)
    assert "@" not in blob
    assert "User Data" not in blob
    assert str(tmp_path) not in blob
    # And the general rule rather than three examples of it: every line is
    # prose plus a bare exception TYPE NAME in parentheses. An exception
    # rendered whole would pass the three assertions above today and carry
    # a path the first time the failure mode changes.
    for record in records:
        assert re.fullmatch(r"[a-z ]+ \(\w+\)", record.getMessage()), \
            record.getMessage()
