"""`gsc_doctor`'s two new checks: `browser` and `extension`.

Every test runs against a temporary GSC_MCP_HOME and a fabricated browser
install on disk, so nothing reads the operator's real profiles, token, or
config directory. The extension lookup is the REAL one — a fake would let
the three states drift apart from what `pairing` actually reports, which is
the only thing these checks are for.

The negative assertions here are the point of the file. A diagnostic tool's
output is read by a model, rendered into a transcript, and kept somewhere
nobody here controls; a `detail` that quotes an exception message leaks a
filesystem path carrying the operator's account name, and a `detail` that
quotes a profile's display name leaks their address, because Chrome labels
profiles with the account signed into them.
"""
from __future__ import annotations

import json
import logging
import re
import traceback
from pathlib import Path

import pytest

from gsc_core import browsers, pairing, profiles
from gsc_mcp import onboarding, server

EXTENSION_ID = "b" * 32
ADDRESS = "operator@example.com"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def captured_log():
    """Records from onboarding's own logger, at every level.

    Attached to the logger object directly and pinned to DEBUG: runlog sets
    propagate=False, so caplog's root handler never sees these records, and
    a negative assertion written against an empty buffer would guard
    nothing. `test_the_log_capture_is_live` proves this fixture captures.
    """
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Collector(level=logging.DEBUG)
    logger = onboarding.log
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def logged_text(records) -> str:
    """Everything a record could carry a secret in, joined.

    Task 9's helper, reused unchanged. `getMessage()` alone is NOT enough:
    a site changed from `log.debug` to `log.exception` puts the exception —
    and with it an address or an OSError's filesystem path — into
    `exc_info`, where the default formatter writes it to the log file and a
    message-only assertion never sees it.
    """
    parts: list[str] = []
    for record in records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
        if record.exc_text:
            parts.append(record.exc_text)
        if record.stack_info:
            parts.append(record.stack_info)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# A browser on disk
# ---------------------------------------------------------------------------

def _candidate(tmp_path, *, email=None, brand_key="brave", name="Person 1"):
    """One profile of one browser, on disk, ready to be surveyed."""
    user_data = tmp_path / "user-data" / brand_key
    profile_dir = user_data / "Default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    installed = browsers.Installed(brand=browsers.BRANDS[brand_key],
                                   exe_path=str(tmp_path / "browser.exe"),
                                   user_data_dir=str(user_data))
    profile = profiles.Profile(directory="Default", name=name,
                               email=email, path=str(profile_dir))
    return profiles.Candidate(installed=installed, profile=profile,
                              score=0, reasons=[])


@pytest.fixture
def surveyed(monkeypatch):
    """Install a fixed survey result; returns the setter."""
    def _set(candidates):
        monkeypatch.setattr(onboarding.profiles, "survey",
                            lambda: list(candidates))
    return _set


def _write_prefs(candidate, ext_dir, *, version="1.10.0", ext_id=EXTENSION_ID):
    """The Preferences entry Chromium writes for a loaded unpacked extension.

    Chromium records the whole manifest it read at load time, which is what
    makes "the copy on disk has moved on since you loaded it" detectable at
    all: the extracted manifest is refreshed by pip, this snapshot is not.
    """
    entry: dict = {"path": str(ext_dir)}
    if version is not None:
        entry["manifest"] = {"version": version}
    prefs = {"extensions": {"settings": {ext_id: entry}}}
    (Path(candidate.profile.path) / "Preferences").write_text(
        json.dumps(prefs), encoding="utf-8")


def _packaged_version() -> str:
    version = pairing.extension_version(pairing.extension_dir())
    assert version, "the packaged extension must declare a version"
    return version


# ---------------------------------------------------------------------------
# The browser check
# ---------------------------------------------------------------------------

def test_browser_check_names_the_recommended_profile(tmp_path, surveyed):
    surveyed([_candidate(tmp_path, brand_key="chrome")])
    check = onboarding.check_browser()
    assert check["name"] == "browser"
    assert check["ok"] is True
    assert "Google Chrome" in check["detail"]
    assert "Default" in check["detail"]
    assert check["fix"] == ""


def test_browser_check_fails_with_a_fix_when_no_browser_is_installed(surveyed):
    surveyed([])
    check = onboarding.check_browser()
    assert check["ok"] is False
    assert check["fix"]
    assert "Chrome" in check["fix"]


def test_browser_check_reports_only_the_exception_type(monkeypatch,
                                                       captured_log):
    """A raise is a failed check, not a leaked path.

    The message here is shaped like the real hazard: an OSError from a
    browser state file quotes a path containing the operator's account
    name.
    """
    def boom():
        raise OSError(f"cannot open C:/Users/{ADDRESS}/Local State")

    monkeypatch.setattr(onboarding.profiles, "survey", boom)
    check = onboarding.check_browser()
    assert check["ok"] is False
    assert check["detail"] == "OSError"
    assert ADDRESS not in check["detail"]
    assert ADDRESS not in check["fix"]
    assert ADDRESS not in logged_text(captured_log)


def test_browser_check_never_quotes_the_signed_in_address(tmp_path, surveyed):
    """Chrome labels profiles with the account signed into them, so the
    display NAME is as dangerous as the address field."""
    surveyed([_candidate(tmp_path, brand_key="chrome",
                         email=ADDRESS, name=ADDRESS)])
    check = onboarding.check_browser()
    assert ADDRESS not in check["detail"]
    assert ADDRESS not in check["fix"]


def test_edge_sign_in_is_not_presented_as_confirmed(tmp_path, surveyed):
    """Edge stores MICROSOFT accounts in the same key Chrome uses for
    Google ones, so an address found there is not evidence of a Google
    sign-in — and if the two addresses coincide it is a false MATCH, not
    merely a false "signed in"."""
    assert browsers.BRANDS["edge"].account_may_be_non_google is True
    surveyed([_candidate(tmp_path, brand_key="edge", email=ADDRESS)])
    check = onboarding.check_browser()
    assert "could not be confirmed" in check["detail"]


def test_chrome_sign_in_carries_no_spurious_caveat(tmp_path, surveyed):
    surveyed([_candidate(tmp_path, brand_key="chrome", email=ADDRESS)])
    check = onboarding.check_browser()
    assert "could not be confirmed" not in check["detail"]


# ---------------------------------------------------------------------------
# The extension check: the three states, plus the one the brief's table
# cannot express
# ---------------------------------------------------------------------------

def test_extension_never_installed_says_load_unpacked(tmp_path, surveyed):
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    check = onboarding.check_extension()
    assert check["name"] == "extension"
    assert check["ok"] is False
    assert "not installed" in check["detail"]
    assert "chrome://extensions" in check["fix"]
    assert "Developer mode" in check["fix"]
    assert "Load unpacked" in check["fix"]


def test_extension_version_mismatch_asks_for_a_reload(tmp_path, surveyed):
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    _write_prefs(candidate, pairing.extension_dir(), version="0.0.1-stale")
    check = onboarding.check_extension()
    assert check["ok"] is False
    assert "Reload" in check["fix"]
    assert "chrome://extensions" in check["fix"]
    # The stale version is ours, not the user's — quoting it is what makes
    # the message actionable, and it identifies nobody.
    assert "0.0.1-stale" in check["detail"]
    assert "Load unpacked" not in check["fix"]


def test_extension_present_is_ok_and_defers_worker_liveness(tmp_path,
                                                            surveyed):
    """Presence is all 3A can see. Saying "working" here would be a claim
    only a live bridge connection could support, and that is 3B's."""
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    _write_prefs(candidate, pairing.extension_dir(),
                 version=_packaged_version())
    check = onboarding.check_extension()
    assert check["ok"] is True
    assert check["fix"] == ""
    assert "installed" in check["detail"]
    assert "not checked" in check["detail"]


def test_an_unrecorded_version_is_not_a_mismatch(tmp_path, surveyed):
    """"Cannot compare" is not "differs".

    A preferences entry that carries no manifest snapshot is an ordinary
    state — Chromium's file, Chromium's rules. Reporting a mismatch there
    would tell the user to click Reload for nothing, and they would do it,
    and it would not help, and they would stop trusting the tool.
    """
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    _write_prefs(candidate, pairing.extension_dir(), version=None)
    check = onboarding.check_extension()
    assert check["ok"] is True
    assert "Reload" not in check["fix"]


def test_unreadable_preferences_is_not_reported_as_not_installed(
        tmp_path, surveyed, monkeypatch):
    """The copy rule this whole tri-state exists for.

    A user whose preferences file was briefly unreadable — EACCES, a
    cloud-synced profile directory, antivirus holding the file open — must
    not be told to reinstall an extension that is sitting right there.
    """
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    (Path(candidate.profile.path) / "Preferences").write_text(
        "{not json", encoding="utf-8")
    check = onboarding.check_extension()
    assert check["ok"] is False
    assert "not installed" not in check["detail"]
    assert "could not be checked" in check["detail"]
    assert check["fix"]


def test_a_lookup_that_could_not_check_is_distinguishable(tmp_path, surveyed):
    """Guards the wiring, not just the wording: if `check_extension` called
    `find_extension_id` instead of `look_up_extension`, this case and the
    never-installed case would return the same `detail`."""
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    absent = onboarding.check_extension()
    (Path(candidate.profile.path) / "Preferences").write_text(
        "{not json", encoding="utf-8")
    unknown = onboarding.check_extension()
    assert absent["detail"] != unknown["detail"]
    assert absent["fix"] != unknown["fix"]


def test_extension_check_fails_when_there_is_no_browser(surveyed):
    surveyed([])
    check = onboarding.check_extension()
    assert check["ok"] is False
    assert check["fix"]


def test_extension_check_reports_only_the_exception_type(monkeypatch,
                                                         captured_log):
    def boom():
        raise OSError(f"cannot open C:/Users/{ADDRESS}/Preferences")

    monkeypatch.setattr(onboarding.profiles, "survey", boom)
    check = onboarding.check_extension()
    assert check["ok"] is False
    assert check["detail"] == "OSError"
    assert ADDRESS not in check["fix"]
    assert ADDRESS not in logged_text(captured_log)


def test_extension_check_survives_an_unextractable_directory(
        tmp_path, surveyed, monkeypatch):
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    monkeypatch.setattr(onboarding.pairing, "extension_dir",
                        lambda: (_ for _ in ()).throw(OSError("denied")))
    check = onboarding.check_extension()
    assert check["ok"] is False
    # Not the generic outer handler's "OSError": an unpackable extension
    # has its own diagnosis and its own fix.
    assert "could not be checked" in check["detail"]
    assert "not installed" not in check["detail"]
    assert "writable" in check["fix"]


def test_extension_check_never_returns_the_extension_id(tmp_path, surveyed):
    """32 characters naming one person's install, of no use to a caller."""
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    _write_prefs(candidate, pairing.extension_dir(),
                 version=_packaged_version())
    check = onboarding.check_extension()
    assert EXTENSION_ID not in json.dumps(check)


def test_extension_check_never_quotes_the_signed_in_address(tmp_path,
                                                            surveyed):
    surveyed([_candidate(tmp_path, brand_key="chrome",
                         email=ADDRESS, name=ADDRESS)])
    check = onboarding.check_extension()
    assert ADDRESS not in check["detail"]
    assert ADDRESS not in check["fix"]


# ---------------------------------------------------------------------------
# The shape both checks owe gsc_doctor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("check_fn", [onboarding.check_browser,
                                      onboarding.check_extension])
def test_every_check_returns_the_doctor_shape(check_fn, tmp_path, surveyed):
    surveyed([_candidate(tmp_path, brand_key="chrome")])
    check = check_fn()
    assert set(check) == {"name", "ok", "detail", "fix"}
    assert isinstance(check["ok"], bool)
    assert check["detail"]


@pytest.mark.parametrize("check_fn", [onboarding.check_browser,
                                      onboarding.check_extension])
def test_a_failing_check_always_carries_a_fix(check_fn, surveyed):
    surveyed([])
    check = check_fn()
    assert check["ok"] is False
    assert check["fix"].strip()


# ---------------------------------------------------------------------------
# No absolute path may leave in a check result
#
# A doctor result travels into an MCP client nobody here controls, to be
# retained, logged or synced. On Windows an absolute path under the config
# directory carries the operator's account name inside it, so a `fix` that
# says "select C:\Users\<name>\..." exports the username to third-party
# storage. gsc_setup solves this by naming the folder in `next.path` and
# referring to it by name; the doctor must not undo that.
# ---------------------------------------------------------------------------

#: `C:\`, `D:/`, ... — the shape that carries a username on this platform.
#: The lookbehind is load-bearing: without it this matches the `e:/` inside
#: `chrome://extensions`, which every fix string legitimately names.
_DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")


def _assert_no_path(check, ext_dir, tmp_path):
    text = check["detail"] + " " + check["fix"]
    assert str(ext_dir) not in text, text
    # Every path this test can reach lives under tmp_path, so this catches a
    # POSIX absolute path too, where there is no drive letter to match on.
    assert str(tmp_path) not in text, text
    assert not _DRIVE.search(text), text


def test_the_extension_fix_names_no_absolute_path_when_not_installed(
        tmp_path, surveyed):
    """The regression this test exists for.

    `_absent` used to interpolate the extraction directory straight into
    its fix string. This is the reachable state a first-time user is most
    likely to hit, so it is the one most likely to be pasted somewhere.
    """
    surveyed([_candidate(tmp_path, brand_key="chrome")])
    ext_dir = pairing.extension_dir()  # extracted, but never loaded

    check = onboarding.check_extension()

    assert check["ok"] is False
    assert "not installed" in check["detail"]
    assert "gsc_setup()" in check["fix"]  # how the user finds the folder
    _assert_no_path(check, ext_dir, tmp_path)


@pytest.mark.parametrize("state", ["absent", "unreadable", "mismatch",
                                   "present"])
def test_no_extension_check_state_returns_an_absolute_path(state, tmp_path,
                                                           surveyed):
    """Every reachable state, not just the one that leaked."""
    candidate = _candidate(tmp_path, brand_key="chrome")
    surveyed([candidate])
    ext_dir = pairing.extension_dir()
    if state == "absent":
        pass  # nothing recorded the extension
    elif state == "unreadable":
        for name in ("Preferences", "Secure Preferences"):
            (Path(candidate.profile.path) / name).write_text(
                "{ not json", encoding="utf-8")
    elif state == "mismatch":
        _write_prefs(candidate, ext_dir, version="0.0.1-not-the-packaged-one")
    else:
        _write_prefs(candidate, ext_dir, version=_packaged_version())

    _assert_no_path(onboarding.check_extension(), ext_dir, tmp_path)


def test_the_browser_check_returns_no_absolute_path(tmp_path, surveyed):
    surveyed([_candidate(tmp_path, brand_key="chrome")])
    _assert_no_path(onboarding.check_browser(), pairing.extension_dir(),
                    tmp_path)


@pytest.mark.parametrize("check_fn", [onboarding.check_browser,
                                      onboarding.check_extension])
def test_the_checks_write_nothing_to_stdout(check_fn, capsys, tmp_path,
                                            surveyed):
    surveyed([_candidate(tmp_path, brand_key="chrome")])
    check_fn()
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# gsc_doctor
# ---------------------------------------------------------------------------

def test_gsc_doctor_runs_seven_checks_in_order(tmp_path, surveyed,
                                               monkeypatch):
    surveyed([_candidate(tmp_path, brand_key="chrome")])
    monkeypatch.delenv("GSC_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GSC_MCP_CLIENT_SECRET", raising=False)
    out = server.gsc_doctor()
    assert [check["name"] for check in out["checks"]] == [
        "oauth_client", "token", "config", "store", "properties",
        "browser", "extension"]


def test_gsc_doctor_still_fails_overall_when_only_a_new_check_fails(
        surveyed, monkeypatch):
    """The five original checks all pass; only `browser` fails.

    Forcing the originals green is the whole point. If they are left to
    fail too — which they do in an unconfigured environment — then an
    overall `ok` computed over the first five only would still come out
    False, and this test would pass over an implementation that ignores
    the two new checks entirely.
    """
    for name in ("_check_oauth_client", "_check_token", "_check_config",
                 "_check_store", "_check_properties"):
        monkeypatch.setattr(
            server, name,
            lambda _n=name: {"name": _n[7:], "ok": True, "detail": "", "fix": ""})
    surveyed([])  # no browser -> the `browser` check is the only failure

    out = server.gsc_doctor()

    failed = [check["name"] for check in out["checks"] if not check["ok"]]
    assert failed == ["browser", "extension"]
    assert out["ok"] is False


def test_gsc_doctor_writes_nothing_to_stdout(capsys, tmp_path, surveyed,
                                             monkeypatch):
    surveyed([_candidate(tmp_path, brand_key="chrome")])
    monkeypatch.delenv("GSC_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GSC_MCP_CLIENT_SECRET", raising=False)
    server.gsc_doctor()
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# The fixture that makes the negative assertions above mean anything
# ---------------------------------------------------------------------------

def test_the_log_capture_is_live(captured_log):
    """Without this, every `not in logged_text(...)` above would pass over
    an empty buffer. runlog sets propagate=False, so this is not a
    hypothetical failure mode — this codebase has shipped it once."""
    onboarding.log.debug("canary %s", "value")
    assert "canary value" in logged_text(captured_log)
