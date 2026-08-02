"""gsc_setup: the idempotent onboarding tool.

Every test here runs against a temporary GSC_MCP_HOME and starts from an
empty pending-consent table, so nothing touches the operator's real token,
config directory, or browsers. The consent tests do bind a real ephemeral
loopback port — that is `start_consent`'s whole job and it makes no
network call — and the autouse fixture closes them again afterwards.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from gsc_core import browsers, profiles
from gsc_mcp import onboarding

CLIENT_ID = "client-id-123"
CLIENT_SECRET = "client-secret-456"
EXTENSION_ID = "a" * 32


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    onboarding._reset_pending()          # test seam, documented in the module
    yield
    onboarding._reset_pending()          # release the loopback sockets


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GSC_MCP_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GSC_MCP_CLIENT_SECRET", CLIENT_SECRET)


@pytest.fixture
def signed_in(monkeypatch):
    """A stored token that verifies, with no network call."""
    monkeypatch.setattr(onboarding.gauth, "load_token",
                        lambda *a, **k: {"refresh_token": "rt",
                                         "access_token": "at"})
    monkeypatch.setattr(onboarding.gauth, "verify_token", lambda *a, **k: 3)


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


def _brave_candidate(tmp_path, *, email=None, brand_key="brave"):
    """One Brave profile on disk, ready to be surveyed."""
    user_data = tmp_path / "user-data"
    profile_dir = user_data / "Default"
    profile_dir.mkdir(parents=True, exist_ok=True)
    installed = browsers.Installed(brand=browsers.BRANDS[brand_key],
                                   exe_path=str(tmp_path / "browser.exe"),
                                   user_data_dir=str(user_data))
    profile = profiles.Profile(directory="Default", name="Person 1",
                               email=email, path=str(profile_dir))
    return profiles.Candidate(installed=installed, profile=profile,
                              score=0, reasons=[])


def _install_extension(candidate, ext_dir):
    """Write the Preferences entry Chromium writes for a loaded unpacked
    extension, so the real pairing lookup finds it."""
    prefs = {"extensions": {"settings": {
        EXTENSION_ID: {"path": str(ext_dir)}}}}
    (Path(candidate.profile.path) / "Preferences").write_text(
        json.dumps(prefs), encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 1: the OAuth client
# ---------------------------------------------------------------------------

def test_reports_the_missing_oauth_client_first(monkeypatch):
    monkeypatch.delenv("GSC_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GSC_MCP_CLIENT_SECRET", raising=False)
    result = onboarding.setup()
    assert result["ok"] is False
    assert result["next"]["step"] == "oauth_client"
    assert "GSC_MCP_CLIENT_ID" in result["next"]["action"]
    assert result["done"] == []
    assert result["pending"] == ["oauth_client", "consent", "browser",
                                 "extension"]


def test_a_missing_client_never_starts_a_consent(monkeypatch):
    """No client means no flow to start — and a receiver opened here would
    hold a port for a sign-in that can never complete."""
    monkeypatch.delenv("GSC_MCP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GSC_MCP_CLIENT_SECRET", raising=False)
    onboarding.setup()
    assert onboarding._peek_pending("") is None
    assert onboarding._peek_pending(CLIENT_ID) is None


# ---------------------------------------------------------------------------
# Step 2: consent
# ---------------------------------------------------------------------------

def test_returns_a_consent_url_without_blocking(configured):
    result = onboarding.setup(open_browser=False)
    assert result["next"]["step"] == "consent"
    assert result["next"]["url"].startswith("https://accounts.google.com/")
    assert result["done"] == ["oauth_client"]


def test_calling_twice_returns_the_same_consent_url(configured):
    """A second call before the user clicks must not open a second
    receiver — the first one's state would then never match."""
    first = onboarding.setup(open_browser=False)
    second = onboarding.setup(open_browser=False)
    assert first["next"]["url"] == second["next"]["url"]


def test_the_second_call_does_not_reopen_the_browser(configured, monkeypatch):
    """Idempotent means callable in a loop; a tab per call is not that."""
    opened = []
    monkeypatch.setattr(onboarding.webbrowser, "open", opened.append)
    onboarding.setup(open_browser=True)
    onboarding.setup(open_browser=True)
    assert len(opened) == 1


def test_open_browser_false_opens_nothing(configured, monkeypatch):
    opened = []
    monkeypatch.setattr(onboarding.webbrowser, "open", opened.append)
    onboarding.setup(open_browser=False)
    assert opened == []


def test_the_response_never_leaks_the_pkce_verifier(configured):
    """The verifier never leaves this process, in any form.

    The `state` is a different animal and cannot be held to the same rule:
    OAuth carries it TO Google in the authorization URL and back in the
    redirect, so a consent URL without it would have no CSRF binding at all
    and `poll()` would have nothing to check the redirect against. It is a
    public nonce by construction. What it must never be is a field of its
    own — see the test below.
    """
    result = onboarding.setup(open_browser=False)
    pending = onboarding._peek_pending(CLIENT_ID)
    assert pending.verifier not in repr(result)
    assert pending.verifier not in result["next"]["url"]


def test_the_state_appears_only_inside_the_consent_url(configured):
    result = onboarding.setup(open_browser=False)
    state = onboarding._peek_pending(CLIENT_ID).state
    without_url = dict(result["next"])
    url = without_url.pop("url")
    assert state in url            # OAuth requires it there
    assert state not in repr(without_url)
    assert state not in repr({k: v for k, v in result.items()
                              if k != "next"})


def test_the_response_never_leaks_the_client_secret(configured):
    result = onboarding.setup(open_browser=False)
    assert CLIENT_SECRET not in repr(result)


def test_an_expired_pending_consent_is_replaced(configured):
    first = onboarding.setup(open_browser=False)
    onboarding._age_pending(CLIENT_ID, seconds=601)
    second = onboarding.setup(open_browser=False)
    assert first["next"]["url"] != second["next"]["url"]


def test_a_pending_consent_just_short_of_the_ttl_is_kept(configured):
    """The boundary in the other direction: a user who is merely slow must
    not have the screen they are looking at invalidated."""
    first = onboarding.setup(open_browser=False)
    onboarding._age_pending(CLIENT_ID, seconds=599)
    second = onboarding.setup(open_browser=False)
    assert first["next"]["url"] == second["next"]["url"]


def test_an_expired_pending_consent_closes_its_receiver(configured):
    """The receiver owns a socket and a thread; replacing without closing
    leaks both for the life of the process."""
    onboarding.setup(open_browser=False)
    stale = onboarding._peek_pending(CLIENT_ID)
    onboarding._age_pending(CLIENT_ID, seconds=601)
    onboarding.setup(open_browser=False)
    assert stale.receiver._closed is True


def test_a_completed_consent_moves_on_to_the_browser_step(
        configured, monkeypatch, signed_in):
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [])
    result = onboarding.setup(open_browser=False)
    assert "consent" in result["done"]
    assert result["next"]["step"] == "browser"
    assert "Chrome" in result["next"]["action"]


def test_a_verified_token_discards_a_pending_consent(configured, monkeypatch):
    """Signing in by any other route must release the port this tool is
    holding, not leave it open for a flow nobody will finish."""
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [])
    onboarding.setup(open_browser=False)
    assert onboarding._peek_pending(CLIENT_ID) is not None
    monkeypatch.setattr(onboarding.gauth, "load_token",
                        lambda *a, **k: {"refresh_token": "rt",
                                         "access_token": "at"})
    monkeypatch.setattr(onboarding.gauth, "verify_token", lambda *a, **k: 3)
    onboarding.setup(open_browser=False)
    assert onboarding._peek_pending(CLIENT_ID) is None


def test_a_redirect_that_landed_completes_the_step_in_the_same_call(
        configured, monkeypatch):
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [])
    onboarding.setup(open_browser=False)
    monkeypatch.setattr(onboarding.gauth, "finish_consent",
                        lambda *a, **k: {"refresh_token": "rt"})
    result = onboarding.setup(open_browser=False)
    assert "consent" in result["done"]
    assert result["next"]["step"] == "browser"
    assert onboarding._peek_pending(CLIENT_ID) is None


def test_a_failed_exchange_discards_the_pending_and_asks_for_a_retry(
        configured, monkeypatch):
    onboarding.setup(open_browser=False)

    def boom(*a, **k):
        raise RuntimeError("state mismatch")

    monkeypatch.setattr(onboarding.gauth, "finish_consent", boom)
    result = onboarding.setup(open_browser=False)
    assert result["next"]["step"] == "consent"
    assert "url" not in result["next"]
    assert onboarding._peek_pending(CLIENT_ID) is None


def test_an_unreachable_google_does_not_force_a_fresh_consent(
        configured, monkeypatch):
    """A network fault while CHECKING a stored token is not evidence the
    token is bad; pushing the user through consent for it would be wrong."""
    monkeypatch.setattr(onboarding.gauth, "load_token",
                        lambda *a, **k: {"refresh_token": "rt",
                                         "access_token": "at"})

    def unreachable(*a, **k):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(onboarding.gauth, "verify_token", unreachable)
    result = onboarding.setup(open_browser=False)
    assert result["next"]["step"] == "consent"
    assert "url" not in result["next"]
    assert "reached" in result["next"]["action"]
    assert onboarding._peek_pending(CLIENT_ID) is None


def test_a_rejected_grant_does_start_a_fresh_consent(configured, monkeypatch):
    """The other side of the same coin: a token Google has refused is not
    a network problem, and re-consent is the fix."""
    monkeypatch.setattr(onboarding.gauth, "load_token",
                        lambda *a, **k: {"refresh_token": "rt",
                                         "access_token": "at"})

    def refused(*a, **k):
        raise onboarding.gauth.ConsentFailed("no properties")

    monkeypatch.setattr(onboarding.gauth, "verify_token", refused)
    result = onboarding.setup(open_browser=False)
    assert result["next"]["url"].startswith("https://accounts.google.com/")


# ---------------------------------------------------------------------------
# Step 3 and 4: the browser and the extension
# ---------------------------------------------------------------------------

def test_the_extension_step_names_the_right_extensions_page(
        configured, monkeypatch, signed_in, tmp_path):
    """The known bug this prevents: telling a Brave user to open
    chrome://extensions."""
    candidate = _brave_candidate(tmp_path)
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    result = onboarding.setup(open_browser=False)
    assert result["next"]["step"] == "extension"
    assert "brave://extensions" in result["next"]["action"]
    assert "chrome://extensions" not in result["next"]["action"]
    assert result["next"]["path"]
    assert result["done"] == ["oauth_client", "consent", "browser"]


def test_an_unreadable_preferences_file_is_never_called_not_installed(
        configured, monkeypatch, signed_in, tmp_path):
    """Task 8's tri-state, defended in the copy layer: a user whose
    preferences file could not be read must not be told to reinstall an
    extension that is sitting right there."""
    candidate = _brave_candidate(tmp_path)
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    monkeypatch.setattr(onboarding.pairing, "look_up_extension",
                        lambda *a, **k: onboarding.pairing.Lookup(None, False))
    action = onboarding.setup(open_browser=False)["next"]["action"]
    assert "could not be checked" in action
    assert "is not installed" not in action


def test_a_complete_miss_does_say_it_is_not_installed(
        configured, monkeypatch, signed_in, tmp_path):
    """The other half of the tri-state: a read that succeeded and found
    nothing must state that plainly, not hedge."""
    candidate = _brave_candidate(tmp_path)
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    monkeypatch.setattr(onboarding.pairing, "look_up_extension",
                        lambda *a, **k: onboarding.pairing.Lookup(None, True))
    action = onboarding.setup(open_browser=False)["next"]["action"]
    assert "is not installed" in action
    assert "could not be checked" not in action


def test_an_account_on_a_brand_that_records_no_google_account_is_hedged(
        configured, monkeypatch, signed_in, tmp_path):
    """Edge writes an account_info entry that may hold a MICROSOFT account,
    and the profile scoring cannot tell the difference — so a recommended
    profile must not be presented as a confirmed Google sign-in."""
    candidate = _brave_candidate(tmp_path, email="someone@example.com",
                                 brand_key="brave")
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    action = onboarding.setup(open_browser=False)["next"]["action"]
    assert "could not be confirmed" in action


def test_a_google_account_brand_is_not_hedged(
        configured, monkeypatch, signed_in, tmp_path):
    candidate = _brave_candidate(tmp_path, email="someone@example.com",
                                 brand_key="chrome")
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    action = onboarding.setup(open_browser=False)["next"]["action"]
    assert "could not be confirmed" not in action


def test_no_email_address_reaches_the_response(
        configured, monkeypatch, signed_in, tmp_path):
    candidate = _brave_candidate(tmp_path, email="someone@example.com")
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    result = onboarding.setup(open_browser=False)
    assert "someone@example.com" not in repr(result)
    assert "@" not in result["next"]["action"]


def test_everything_satisfied_reports_ok(
        configured, monkeypatch, signed_in, tmp_path):
    candidate = _brave_candidate(tmp_path)
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    _install_extension(candidate, onboarding.pairing.extension_dir())
    result = onboarding.setup(open_browser=False)
    assert result["ok"] is True
    assert result["next"] is None
    assert set(result["done"]) == {"oauth_client", "consent", "browser",
                                   "extension"}
    assert result["pending"] == []


def test_the_shape_never_claims_ok_with_steps_outstanding():
    """`_result`'s guard, pinned directly.

    Found by mutation: dropping the `len(done)` half of the `ok` condition
    killed no test, because today no path returns `next: null` early. That
    makes the guard the kind of code a future step — added to STEPS but not
    to a code path — silently walks past. Asserted here rather than deleted,
    because "ok means all four are done" is the tool's whole contract.
    """
    assert onboarding._result(["oauth_client"], None)["ok"] is False
    assert onboarding._result(list(onboarding.STEPS), None)["ok"] is True


def test_the_extension_id_is_never_returned(
        configured, monkeypatch, signed_in, tmp_path):
    """32 characters naming one person's install, of no use to a model."""
    candidate = _brave_candidate(tmp_path)
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])
    _install_extension(candidate, onboarding.pairing.extension_dir())
    assert EXTENSION_ID not in repr(onboarding.setup(open_browser=False))


def test_an_unwritable_extension_directory_says_so(
        configured, monkeypatch, signed_in, tmp_path):
    candidate = _brave_candidate(tmp_path)
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])

    def boom(*a, **k):
        raise OSError(rf"cannot write C:\Users\a-real-person\gsc-mcp")

    monkeypatch.setattr(onboarding.pairing, "extension_dir", boom)
    result = onboarding.setup(open_browser=False)
    assert result["next"]["step"] == "extension"
    assert "path" not in result["next"]
    assert "a-real-person" not in repr(result)


# ---------------------------------------------------------------------------
# The safety net
# ---------------------------------------------------------------------------

def test_an_unexpected_failure_returns_a_shape_instead_of_raising(
        configured, monkeypatch, signed_in):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(onboarding.profiles, "survey", boom)
    result = onboarding.setup(open_browser=False)
    assert result["ok"] is False
    assert result["next"]["step"] == "unexpected"
    assert "kaboom" not in repr(result)


def test_nothing_is_written_to_stdout(configured, capsys):
    """MCP frames JSON-RPC on stdout; one stray byte corrupts the session."""
    onboarding.setup(open_browser=False)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Logging privacy
# ---------------------------------------------------------------------------

def test_the_log_capture_is_live(captured_log):
    """Proves the fixture below is not asserting over an empty buffer.

    runlog sets propagate=False and this codebase has already shipped a
    privacy test that passed because it captured nothing.
    """
    onboarding.log.debug("canary %s", "value")
    assert any("canary" in record.getMessage() for record in captured_log)


def test_no_secret_ever_reaches_a_log_line(configured, monkeypatch,
                                           captured_log, tmp_path):
    def boom(*a, **k):
        raise RuntimeError(f"failed with {CLIENT_SECRET}")

    monkeypatch.setattr(onboarding.gauth, "finish_consent", boom)
    onboarding.setup(open_browser=False)
    pending = onboarding._peek_pending(CLIENT_ID)
    verifier, state = pending.verifier, pending.state
    onboarding.setup(open_browser=False)

    text = "\n".join(record.getMessage() for record in captured_log)
    assert text  # the failure above must have logged something
    assert CLIENT_SECRET not in text
    assert CLIENT_ID not in text
    assert verifier not in text
    assert state not in text


def test_no_email_address_reaches_a_log_line(configured, monkeypatch,
                                             signed_in, captured_log,
                                             tmp_path):
    candidate = _brave_candidate(tmp_path, email="someone@example.com")
    monkeypatch.setattr(onboarding.profiles, "survey", lambda: [candidate])

    def boom(*a, **k):
        raise OSError("denied for someone@example.com")

    monkeypatch.setattr(onboarding.pairing, "extension_dir", boom)
    onboarding.setup(open_browser=False)

    text = "\n".join(record.getMessage() for record in captured_log)
    assert text  # the failure above must have logged something
    assert "someone@example.com" not in text
    assert "@" not in text
