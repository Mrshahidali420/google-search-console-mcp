"""Layer 2: profiles inside a browser, and the account signed into each.

Every path here is built under ``tmp_path``; nothing reads a real browser
profile. Every address is on an RFC 2606 reserved domain — this suite is
public, and a real address in a fixture is a leak that ships forever.
"""
from __future__ import annotations

import json
import shutil

from gsc_core import browsers, profiles


def _make_browser(tmp_path, profiles_spec):
    """Build a fake Chromium user-data dir. profiles_spec maps a profile
    directory to (display_name, email_or_None)."""
    root = tmp_path / "User Data"
    root.mkdir(parents=True)
    info_cache = {d: {"name": n} for d, (n, _) in profiles_spec.items()}
    (root / "Local State").write_text(
        json.dumps({"profile": {"info_cache": info_cache}}), encoding="utf-8")
    for directory, (_, email) in profiles_spec.items():
        pdir = root / directory
        pdir.mkdir()
        prefs = {"account_info": [{"email": email}]} if email else {}
        (pdir / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")
    return browsers.Installed(brand=browsers.BRANDS["chrome"],
                              exe_path="/nonexistent/chrome",
                              user_data_dir=str(root))


def _write(path, payload):
    """Write JSON, or raw text when the payload is already a string."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------

def test_lists_every_profile_with_its_display_name(tmp_path):
    installed = _make_browser(tmp_path, {
        "Default": ("Personal", "someone@example.com"),
        "Profile 2": ("Work", "work@example.net")})
    found = {p.directory: p for p in profiles.list_profiles(installed)}
    assert set(found) == {"Default", "Profile 2"}
    assert found["Profile 2"].name == "Work"
    assert found["Profile 2"].email == "work@example.net"


def test_the_profile_path_points_at_the_directory_on_disk(tmp_path):
    installed = _make_browser(tmp_path, {"Profile 2": ("Work", None)})
    only = profiles.list_profiles(installed)[0]
    assert only.path == str(tmp_path / "User Data" / "Profile 2")


def test_a_profile_with_no_signed_in_account_has_email_none(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    assert profiles.list_profiles(installed)[0].email is None


def test_an_empty_account_info_list_is_not_an_error(tmp_path):
    """The ordinary state of a profile nobody has signed into."""
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Default" / "Preferences",
           {"account_info": []})
    result = profiles.list_profiles(installed)
    assert len(result) == 1 and result[0].email is None


def test_falls_back_to_secure_preferences_for_the_account(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    secure = {"account_info": [{"email": "secure@example.com"}]}
    (tmp_path / "User Data" / "Default" / "Secure Preferences").write_text(
        json.dumps(secure), encoding="utf-8")
    assert profiles.list_profiles(installed)[0].email == "secure@example.com"


def test_preferences_wins_over_secure_preferences_when_both_have_an_account(
        tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal",
                                                     "plain@example.com")})
    _write(tmp_path / "User Data" / "Default" / "Secure Preferences",
           {"account_info": [{"email": "secure@example.net"}]})
    assert profiles.list_profiles(installed)[0].email == "plain@example.com"


def test_an_absent_preferences_file_still_yields_the_profile(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    (tmp_path / "User Data" / "Default" / "Preferences").unlink()
    result = profiles.list_profiles(installed)
    assert len(result) == 1 and result[0].email is None


# --------------------------------------------------------------------------
# Ordering — Task 5 ranks this output, so it may not flap
# --------------------------------------------------------------------------

def test_profiles_come_back_in_a_stable_human_order(tmp_path):
    """Default first, then Profile N numerically — not "Profile 10" before 2."""
    installed = _make_browser(tmp_path, {
        "Profile 10": ("Ten", None),
        "Profile 2": ("Two", None),
        "Default": ("Personal", None),
        "Profile 1": ("One", None)})
    order = [p.directory for p in profiles.list_profiles(installed)]
    assert order == ["Default", "Profile 1", "Profile 2", "Profile 10"]


def test_the_order_does_not_depend_on_the_order_in_local_state(tmp_path):
    installed = _make_browser(tmp_path, {
        "Profile 2": ("Two", None), "Default": ("Personal", None)})
    other = _make_browser(tmp_path / "b", {
        "Default": ("Personal", None), "Profile 2": ("Two", None)})
    assert ([p.directory for p in profiles.list_profiles(installed)] ==
            [p.directory for p in profiles.list_profiles(other)])


# --------------------------------------------------------------------------
# Missing and corrupt files — all ordinary states on a real machine
# --------------------------------------------------------------------------

def test_a_corrupt_local_state_yields_no_profiles_rather_than_raising(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    (tmp_path / "User Data" / "Local State").write_text("{not json",
                                                        encoding="utf-8")
    assert profiles.list_profiles(installed) == []


def test_an_absent_local_state_yields_no_profiles(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    (tmp_path / "User Data" / "Local State").unlink()
    assert profiles.list_profiles(installed) == []


def test_an_absent_user_data_dir_yields_no_profiles(tmp_path):
    installed = browsers.Installed(brand=browsers.BRANDS["chrome"],
                                   exe_path="/nonexistent/chrome",
                                   user_data_dir=str(tmp_path / "never here"))
    assert profiles.list_profiles(installed) == []


def test_a_profile_listed_in_local_state_but_missing_on_disk_is_skipped(
        tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    shutil.rmtree(tmp_path / "User Data" / "Default")
    assert profiles.list_profiles(installed) == []


def test_a_corrupt_preferences_file_still_yields_the_profile(tmp_path):
    """A profile whose account cannot be read is still a usable profile —
    it just has no email to rank on."""
    installed = _make_browser(tmp_path,
                              {"Default": ("Personal", "a@example.com")})
    (tmp_path / "User Data" / "Default" / "Preferences").write_text(
        "{not json", encoding="utf-8")
    result = profiles.list_profiles(installed)
    assert len(result) == 1 and result[0].email is None


def test_one_unreadable_profile_does_not_lose_the_others(tmp_path):
    installed = _make_browser(tmp_path, {
        "Default": ("Personal", "a@example.com"),
        "Profile 2": ("Work", "b@example.net")})
    _write(tmp_path / "User Data" / "Default" / "Preferences", "{not json")
    found = {p.directory: p.email for p in profiles.list_profiles(installed)}
    assert found == {"Default": None, "Profile 2": "b@example.net"}


# --------------------------------------------------------------------------
# Valid JSON of the wrong shape — the half that actually raises in production
# --------------------------------------------------------------------------

def test_a_local_state_that_is_a_list_yields_no_profiles(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Local State", ["not", "an", "object"])
    assert profiles.list_profiles(installed) == []


def test_an_info_cache_that_is_not_a_mapping_yields_no_profiles(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Local State",
           {"profile": {"info_cache": ["Default"]}})
    assert profiles.list_profiles(installed) == []


def test_a_profile_entry_that_is_not_a_mapping_falls_back_to_its_directory(
        tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Local State",
           {"profile": {"info_cache": {"Default": "Personal"}}})
    result = profiles.list_profiles(installed)
    assert len(result) == 1
    assert result[0].directory == "Default" and result[0].name == "Default"


def test_a_missing_display_name_falls_back_to_the_directory(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Local State",
           {"profile": {"info_cache": {"Default": {}}}})
    assert profiles.list_profiles(installed)[0].name == "Default"


def test_a_non_string_display_name_falls_back_to_the_directory(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Local State",
           {"profile": {"info_cache": {"Default": {"name": 17}}}})
    assert profiles.list_profiles(installed)[0].name == "Default"


def test_preferences_that_are_a_list_leave_the_email_unknown(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Default" / "Preferences", [{"email": "x"}])
    result = profiles.list_profiles(installed)
    assert len(result) == 1 and result[0].email is None


def test_account_info_that_is_a_string_leaves_the_email_unknown(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Default" / "Preferences",
           {"account_info": "someone@example.com"})
    assert profiles.list_profiles(installed)[0].email is None


def test_account_info_entries_that_are_not_mappings_leave_the_email_unknown(
        tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Default" / "Preferences",
           {"account_info": ["someone@example.com"]})
    assert profiles.list_profiles(installed)[0].email is None


def test_a_non_string_email_is_treated_as_unknown(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Default" / "Preferences",
           {"account_info": [{"email": 12345}]})
    assert profiles.list_profiles(installed)[0].email is None


def test_an_entry_without_an_email_does_not_hide_a_later_one(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Default" / "Preferences",
           {"account_info": [{"gaia": "1"}, {"email": "real@example.org"}]})
    assert profiles.list_profiles(installed)[0].email == "real@example.org"


def test_an_empty_email_string_is_treated_as_unknown(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    _write(tmp_path / "User Data" / "Default" / "Preferences",
           {"account_info": [{"email": "   "}]})
    assert profiles.list_profiles(installed)[0].email is None


# --------------------------------------------------------------------------
# Privacy — this module handles the most sensitive data in the project
# --------------------------------------------------------------------------

def test_no_email_or_path_reaches_a_log_line(tmp_path, caplog):
    """Failures are logged by exception TYPE NAME only.

    A JSON error quotes the offending document, and an OSError quotes a
    path carrying the operator's account name. Neither may be logged.
    """
    installed = _make_browser(tmp_path,
                              {"Default": ("Personal", "leak@example.com")})
    _write(tmp_path / "User Data" / "Default" / "Preferences",
           '{"account_info": [{"email": "leak@example.com"} ')
    with caplog.at_level("DEBUG"):
        profiles.list_profiles(installed)
    assert "leak@example.com" not in caplog.text
    assert "example.com" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_a_corrupt_local_state_is_logged_without_its_contents(tmp_path,
                                                              caplog):
    installed = _make_browser(tmp_path, {"Default": ("Personal", None)})
    (tmp_path / "User Data" / "Local State").write_text(
        '{"secret-marker": ', encoding="utf-8")
    with caplog.at_level("DEBUG"):
        profiles.list_profiles(installed)
    assert "secret-marker" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_nothing_is_written_to_stdout(tmp_path, capsys):
    """MCP frames JSON-RPC on stdout; one stray byte corrupts the session."""
    installed = _make_browser(tmp_path,
                              {"Default": ("Personal", "a@example.com")})
    _write(tmp_path / "User Data" / "Default" / "Preferences", "{not json")
    profiles.list_profiles(installed)
    assert capsys.readouterr().out == ""


def test_list_profiles_writes_nothing_to_the_user_data_dir(tmp_path):
    installed = _make_browser(tmp_path, {"Default": ("Personal",
                                                     "a@example.com")})
    root = tmp_path / "User Data"
    before = sorted(p.name for p in root.rglob("*"))
    profiles.list_profiles(installed)
    assert sorted(p.name for p in root.rglob("*")) == before
