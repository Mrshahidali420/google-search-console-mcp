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

from _logcheck import Captured
from gsc_core import browsers, pairing, profiles

VALID_ID = "a" * 32          # matches ^[a-p]{32}$
OTHER_ID = "b" * 32
# For the ordering tests only: a well-shaped id that sorts BEFORE the one
# we expect back. "a" * 32 is the smallest id there is, so a test using it
# as the answer cannot tell "matched on the path" apart from "returned the
# first id it saw".
LOW_ID = "a" * 32
HIGH_ID = "p" * 32


# The privacy guard lives in one place for the whole suite; see
# tests/_logcheck.py for why `getMessage()` alone is not enough and why
# caplog cannot be used here.
_Captured = Captured

# Every log line in this module is prose plus a bare exception TYPE NAME.
# The shape check is the general rule rather than three examples of it.
SHAPE = r"[a-z ]+ \(\w+\)"


def _assert_clean(records, *secrets):
    records.assert_says_nothing_identifying(*secrets, shape=SHAPE)


# An exception message shaped like the ones this code really meets: an
# OSError's message is a filesystem path, and on a real machine that path
# holds the operator's Windows account name and often their address.
LEAKY = r"C:\Users\a-real-person\leak@example.com\Secure Preferences"


def _raise_leaky(*args, **kwargs):
    raise OSError(LEAKY)


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


# ---------------------------------------------------------------------------
# Not installed, versus could not be checked
# ---------------------------------------------------------------------------

def _unreadable(path):
    """Make a path that exists and cannot be read as a file.

    A directory in a file's place raises PermissionError on Windows and
    IsADirectoryError on POSIX — both OSError, neither FileNotFoundError,
    and no privilege needed to arrange. It stands in for the real causes:
    EACCES, a cloud-sync placeholder, antivirus holding the file open.
    """
    path.mkdir(parents=True, exist_ok=True)


def test_the_extension_being_present_reads_as_true(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours)}})
    assert pairing.has_extension(installed, profile) is True


def test_preferences_that_were_read_and_lack_it_read_as_false(tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(
        tmp_path, {OTHER_ID: {"path": str(tmp_path / "elsewhere")}})
    assert pairing.has_extension(installed, profile) is False


def test_a_profile_that_has_never_been_launched_reads_as_false(tmp_path,
                                                               monkeypatch):
    """Absent is not unreadable. A browser that has recorded no extensions
    at all is a complete answer of "no", and reporting "could not check"
    there would make the honest None meaningless by crying wolf on every
    fresh profile."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed = browsers.Installed(brand=browsers.BRANDS["chrome"],
                                   exe_path="/nonexistent",
                                   user_data_dir=str(tmp_path / "User Data"))
    profile = profiles.Profile(directory="Default", name="Personal",
                               email=None, path=str(tmp_path / "never-run"))
    assert pairing.has_extension(installed, profile) is False


def test_preferences_that_could_not_be_read_read_as_none_not_false(
        tmp_path, monkeypatch):
    """The finding this exists for: a user with the extension correctly
    installed, whose preferences file this process cannot open, must not be
    told the extension is missing."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(pairing.extension_dir())}})
    for filename in ("Secure Preferences", "Preferences"):
        target = tmp_path / "User Data" / "Default" / filename
        if target.exists():
            target.unlink()
        _unreadable(target)
    assert pairing.has_extension(installed, profile) is None


def test_a_truncated_preferences_file_reads_as_none_not_false(tmp_path,
                                                              monkeypatch):
    """Caught mid-write. Nothing is known about this profile, which is not
    the same as knowing the extension is absent."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(tmp_path, {})
    (tmp_path / "User Data" / "Default" / "Secure Preferences").write_text(
        "{not json", encoding="utf-8")
    assert pairing.has_extension(installed, profile) is None


def test_one_unreadable_file_does_not_spoil_a_hit_in_the_other(tmp_path,
                                                               monkeypatch):
    """Found is found. The unreadable sibling costs nothing once the answer
    is positive, so a locked Secure Preferences must not downgrade a real
    hit in Preferences to "could not check"."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours)}}, filename="Preferences")
    _unreadable(tmp_path / "User Data" / "Default" / "Secure Preferences")
    assert pairing.has_extension(installed, profile) is True


def test_one_unreadable_file_does_spoil_a_miss_in_the_other(tmp_path,
                                                            monkeypatch):
    """The other half of the same rule. A miss is only trustworthy when
    every file that exists was read."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(
        tmp_path, {OTHER_ID: {"path": str(tmp_path / "elsewhere")}},
        filename="Preferences")
    _unreadable(tmp_path / "User Data" / "Default" / "Secure Preferences")
    assert pairing.has_extension(installed, profile) is None


def test_an_unresolvable_extraction_reads_as_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(tmp_path, {})
    monkeypatch.setattr(pairing, "extension_dir", _raise_leaky)
    assert pairing.has_extension(installed, profile) is None


def test_secure_preferences_wins_when_both_files_name_a_different_id(
        tmp_path, monkeypatch):
    """Chromium moves extension settings into the signed file on the
    platforms that support it, so that copy is the authoritative one. With
    every other fixture writing only ONE of the two files, nothing else
    here would notice the precedence being reversed."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    ours = pairing.extension_dir()
    installed, profile = _profile_with_extensions(
        tmp_path, {VALID_ID: {"path": str(ours)}})
    (tmp_path / "User Data" / "Default" / "Preferences").write_text(
        json.dumps({"extensions": {"settings": {
            OTHER_ID: {"path": str(ours)}}}}), encoding="utf-8")
    assert pairing.find_extension_id(installed, profile) == VALID_ID


# ---------------------------------------------------------------------------
# Every log site in this module, one test each
# ---------------------------------------------------------------------------
#
# An OSError's message is a filesystem path holding the operator's Windows
# account name, and the file it names holds their address. So the rule is
# not "do not log the exception we happen to raise in this test" — it is
# that NO log site here may render an exception, and every site is pinned
# individually. Five sites, five tests.

def test_the_log_capture_is_live():
    """Proves the five assertions below are not running over an empty buffer.

    runlog sets propagate=False, so caplog's root handler never sees these
    records, and `caplog.at_level(0)` is NOTSET, which drops debug records.
    A negative log assertion written either way passes forever.
    """
    with Captured(pairing.log) as records:
        pairing.log.debug("canary %s", "value")
    assert any("canary" in record.getMessage() for record in records)


def test_an_unreadable_manifest_is_logged_by_type_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    extracted = pairing.extension_dir()
    (extracted / "manifest.json").write_text("{not json", encoding="utf-8")
    with _Captured(pairing.log) as records:
        pairing.extension_dir()
    _assert_clean(records, tmp_path)


def test_an_unavailable_extraction_is_logged_by_type_only(tmp_path,
                                                          monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(tmp_path, {})
    monkeypatch.setattr(pairing, "extension_dir", _raise_leaky)
    with _Captured(pairing.log) as records:
        assert pairing.find_extension_id(installed, profile) is None
    _assert_clean(records, tmp_path, "a-real-person")


def test_an_unusable_profile_directory_is_logged_by_type_only(tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, _ = _profile_with_extensions(tmp_path, {})
    # directory is a str everywhere in the real stack; a None here is what
    # a hand-built Profile or a future field rename produces, and the join
    # raises TypeError rather than returning something wrong.
    broken = profiles.Profile(directory=None, name="Personal", email=None,
                              path="")
    with _Captured(pairing.log) as records:
        assert pairing.find_extension_id(installed, broken) is None
    _assert_clean(records, tmp_path)


def test_an_unreadable_preferences_file_is_logged_by_type_only(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(tmp_path, {})
    target = tmp_path / "User Data" / "Default" / "Secure Preferences"
    target.unlink()
    _unreadable(target)
    with _Captured(pairing.log) as records:
        pairing.find_extension_id(installed, profile)
    _assert_clean(records, tmp_path, "User Data")


def test_an_unparsable_preferences_file_is_logged_by_type_only(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    installed, profile = _profile_with_extensions(tmp_path, {})
    (tmp_path / "User Data" / "Default" / "Secure Preferences").write_text(
        "{not json", encoding="utf-8")
    with _Captured(pairing.log) as records:
        pairing.find_extension_id(installed, profile)
    _assert_clean(records, tmp_path, "User Data")
