"""gsc_detect_browsers: the first tool whose answer leaves the process.

Every layer beneath this one kept the signed-in address in memory only. An
MCP tool result is different in kind: a language model consumes it, a client
renders it into a transcript, and something the operator does not control
keeps it. So the assertions below are as much about what the result does NOT
contain as about what it does.

The other half is that "no Chromium browser is installed" is an ordinary
state on a real machine, not a fault, and must read as a friendly answer
with a next step rather than an error the model has to explain away.
"""
from __future__ import annotations

import json
import os

import pytest

from gsc_core import browsers, profiles
from gsc_mcp import tools_browsers

ACCOUNT = "authorised@example.com"
OTHER = "someone-else@example.net"
# A profile LABEL that is itself an address — Chrome does this. It is a
# third leak route, distinct from the two addresses above, and the privacy
# assertions below check all three rather than only the obvious one.
LABELLED = "labelled@example.org"


def _installed(brand_key):
    return browsers.Installed(brand=browsers.BRANDS[brand_key],
                              exe_path="/nonexistent",
                              user_data_dir="/nonexistent")


def _candidate(brand_key, directory, email, name=None):
    profile = profiles.Profile(directory=directory, name=name or directory,
                               email=email, path="/nonexistent")
    return profiles.Candidate(installed=_installed(brand_key), profile=profile,
                              score=0, reasons=[])


@pytest.fixture
def survey(monkeypatch):
    """Replace the machine's real browsers with a stated set."""

    def _install(candidates):
        monkeypatch.setattr(profiles, "survey", lambda: list(candidates))

    return _install


@pytest.fixture(autouse=True)
def no_stored_account(monkeypatch):
    """No token on disk unless a test says otherwise.

    The four tests exercising the REAL _authorised_email() drop this stub
    with `monkeypatch.undo()`. That reverts every setattr on the shared
    per-test monkeypatch — including the `survey` fixture's, if it has
    already run — so each of those tests re-installs its own survey
    afterwards and none of them calls the `survey` callable first. Keep it
    that way, or undo() will silently restore the real browser detection
    and the test will start reading the machine it runs on.
    """
    monkeypatch.setattr(tools_browsers, "_authorised_email", lambda: None)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path):
    """Point GSC_MCP_HOME at a scratch directory for every test here.

    The extension check extracts into config_dir(), so without this the
    suite would write into the developer's real application-data folder.

    Set through os.environ rather than monkeypatch on purpose: the four
    tests exercising the real _authorised_email() call monkeypatch.undo(),
    which reverts every setattr AND setenv on the shared per-test
    monkeypatch — including this one — and would put the extraction back
    into the real home for exactly those tests.
    """
    previous = os.environ.get("GSC_MCP_HOME")
    os.environ["GSC_MCP_HOME"] = str(tmp_path / "home")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GSC_MCP_HOME", None)
        else:
            os.environ["GSC_MCP_HOME"] = previous


def _signed_in_account(monkeypatch, address):
    monkeypatch.setattr(tools_browsers, "_authorised_email", lambda: address)


def _blob(result) -> str:
    """Everything the transcript would carry, as one searchable string."""
    return json.dumps(result, default=str)


def _every_leak_route():
    """One survey carrying all three routes an address can escape by.

    A privacy test built on a profile email alone passes while a display
    name or a redacted reason leaks, so the fixture the log and stdout
    assertions run against carries every route at once: an address on a
    profile, the authorised address (which recommend() writes into its own
    reason string), and a display name that is an address.
    """
    return [_candidate("chrome", "Default", ACCOUNT, name=LABELLED),
            _candidate("chrome", "Profile 2", OTHER),
            _candidate("brave", "Default", None)]


# ---------------------------------------------------------------------------
# Privacy: what a transcript of this tool would contain
# ---------------------------------------------------------------------------

def test_no_profile_address_appears_anywhere_in_the_result(survey, monkeypatch):
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", OTHER)])
    assert OTHER not in _blob(tools_browsers.detect_browsers())


def test_the_authorised_address_is_not_echoed_back_either(survey, monkeypatch):
    """profiles.recommend writes the supplied address into its own reason
    string. Passing that text through unfiltered would put the operator's
    Search Console account into the transcript by the back door."""
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", ACCOUNT)])
    assert ACCOUNT not in _blob(tools_browsers.detect_browsers())


def test_the_match_is_still_reported_without_naming_the_account(survey,
                                                               monkeypatch):
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", ACCOUNT)])
    entry = tools_browsers.detect_browsers()["profiles"][0]
    assert entry["matches_authorised_account"] is True
    assert entry["signed_in"] is True


def test_a_display_name_that_is_an_address_is_replaced(survey):
    """Chrome will happily use the account address as the profile label."""
    survey([_candidate("chrome", "Profile 2", None, name=OTHER)])
    result = tools_browsers.detect_browsers()
    assert OTHER not in _blob(result)
    assert result["profiles"][0]["display_name"] == "Profile 2"


def test_no_filesystem_path_is_returned(survey):
    """A profile path carries the operator's account name on every OS."""
    candidate = _candidate("chrome", "Default", None)
    profile = candidate.profile
    marked = profiles.Profile(directory=profile.directory, name=profile.name,
                              email=None, path="/home/a-real-person/x")
    survey([profiles.Candidate(installed=candidate.installed, profile=marked,
                               score=0, reasons=[])])
    assert "a-real-person" not in _blob(tools_browsers.detect_browsers())


def test_no_address_reaches_a_log_line(survey, monkeypatch, caplog):
    """The layers below both carry this assertion; the one layer whose
    output actually leaves the process must not be the exception.

    Paired with the failure test below, which proves capture is live —
    without it a negative assertion over an empty caplog passes vacuously.
    """
    _signed_in_account(monkeypatch, ACCOUNT)
    survey(_every_leak_route())
    with caplog.at_level("DEBUG"):
        tools_browsers.detect_browsers()
    for address in (ACCOUNT, OTHER, LABELLED):
        assert address not in caplog.text
    for domain in ("example.com", "example.net", "example.org"):
        assert domain not in caplog.text


def test_a_logged_failure_carries_the_type_name_and_nothing_else(monkeypatch,
                                                                 caplog):
    """An OSError's message is a filesystem path holding the operator's
    account name. Only the exception TYPE may be logged.

    The positive assertion is load-bearing: it proves caplog really is
    capturing this logger despite runlog setting propagate = False, which
    is what stops the negative assertions here and above from passing on an
    empty buffer.
    """
    def boom():
        raise OSError(rf"C:\Users\a-real-person\{OTHER}\State")

    monkeypatch.setattr(profiles, "survey", boom)
    with caplog.at_level("DEBUG"):
        tools_browsers.detect_browsers()
    assert "OSError" in caplog.text
    assert "a-real-person" not in caplog.text
    assert OTHER not in caplog.text


def test_nothing_is_written_to_stdout(survey, monkeypatch, capsys):
    """MCP frames JSON-RPC on stdout; one stray byte corrupts the session,
    and the failure surfaces to a client as an unrelated parse error."""
    _signed_in_account(monkeypatch, ACCOUNT)
    survey(_every_leak_route())
    tools_browsers.detect_browsers()
    assert capsys.readouterr().out == ""


def test_nothing_is_written_to_stdout_on_the_failure_path(monkeypatch, capsys):
    def boom():
        raise OSError("state unreadable")

    monkeypatch.setattr(profiles, "survey", boom)
    tools_browsers.detect_browsers()
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Per-browser context: every entry has to stand on its own
# ---------------------------------------------------------------------------

def test_each_entry_carries_its_brands_extensions_url(survey, monkeypatch):
    """A static per-brand constant with no privacy cost, and the exact
    string Task 8 needs to tell a user where to install the pairing
    extension. It ships now for the same reason has_extension does: adding
    it later mutates a shape Tasks 9 and 10 already consume.

    Pinned per brand, because the failure worth catching is not a missing
    key — it is chrome://extensions handed to a Brave user, which simply
    does not open.
    """
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", None),
            _candidate("brave", "Default", None),
            _candidate("vivaldi", "Default", None)])
    listed = {entry["browser_key"]: entry["extensions_url"]
              for entry in tools_browsers.detect_browsers()["profiles"]}
    assert listed == {"chrome": "chrome://extensions",
                      "brave": "brave://extensions",
                      "vivaldi": "vivaldi://extensions"}


def test_the_extensions_url_comes_from_the_brand_not_a_local_table(survey,
                                                                   monkeypatch):
    """Deriving the URL here rather than reading Brand.extensions_url is
    the re-derive-at-the-call-site defect browsers.py exists to prevent —
    Chromium is the case that catches it, since it registers no
    chromium:// scheme and genuinely uses chrome://extensions.
    """
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chromium", "Default", None)])
    entry = tools_browsers.detect_browsers()["profiles"][0]
    assert entry["extensions_url"] == browsers.BRANDS["chromium"].extensions_url
    assert entry["extensions_url"] == "chrome://extensions"


# ---------------------------------------------------------------------------
# The recommendation comes from profiles.recommend, and only from there
# ---------------------------------------------------------------------------

def test_exactly_one_profile_is_flagged_as_recommended(survey, monkeypatch):
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", None),
            _candidate("brave", "Profile 2", ACCOUNT)])
    result = tools_browsers.detect_browsers()
    flagged = [entry for entry in result["profiles"] if entry["recommended"]]
    assert len(flagged) == 1


def test_the_flagged_profile_is_the_one_recommend_chose(survey, monkeypatch):
    """The account match must beat Chrome's brand preference, exactly as the
    ranking layer decides it — this tool must not re-derive a winner."""
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", None),
            _candidate("brave", "Profile 2", ACCOUNT)])
    result = tools_browsers.detect_browsers()
    assert result["recommended"]["browser_key"] == "brave"
    assert result["recommended"]["profile"] == "Profile 2"


def test_the_recommendation_explains_itself(survey, monkeypatch):
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("brave", "Profile 2", ACCOUNT)])
    reasons = tools_browsers.detect_browsers()["reasons"]
    assert reasons and all(isinstance(reason, str) for reason in reasons)


def test_profile_order_is_the_surveys_order_not_a_resort(survey, monkeypatch):
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", None),
            _candidate("chrome", "Profile 2", None),
            _candidate("brave", "Default", ACCOUNT)])
    listed = [(entry["browser_key"], entry["profile"])
              for entry in tools_browsers.detect_browsers()["profiles"]]
    assert listed == [("chrome", "Default"), ("chrome", "Profile 2"),
                      ("brave", "Default")]


def test_two_calls_over_freshly_built_candidates_agree(monkeypatch):
    """Rebuilt objects every call, not the same list twice.

    Comparing one survey against itself restates dict construction and
    kills nothing. Building fresh Candidates each call is what exercises
    the real risk in `recommended`: it is decided by object IDENTITY
    against what recommend() returned, so an implementation that cached a
    winner, or compared against a survey it did not just take, flips the
    flag on the second call while the first still looks right.
    """
    _signed_in_account(monkeypatch, ACCOUNT)
    monkeypatch.setattr(profiles, "survey",
                        lambda: [_candidate("chrome", "Profile 1", None),
                                 _candidate("chrome", "Profile 2", ACCOUNT),
                                 _candidate("brave", "Default", None)])
    first = tools_browsers.detect_browsers()
    second = tools_browsers.detect_browsers()
    assert first == second
    assert [entry["recommended"] for entry in first["profiles"]].count(True) == 1


# ---------------------------------------------------------------------------
# The three account states survive into the result
# ---------------------------------------------------------------------------

def test_a_brand_that_records_no_account_reports_unknown_not_false(survey):
    """Brave ships no Google Sync: "not discoverable" is not "signed out"."""
    survey([_candidate("brave", "Default", None)])
    entry = tools_browsers.detect_browsers()["profiles"][0]
    assert entry["account_discoverable"] is False
    assert entry["matches_authorised_account"] is None


def test_an_unknowable_brand_is_unknown_even_once_an_account_authorised(
        survey, monkeypatch):
    """The one case the two tests either side of this cannot reach: an
    account HAS authorised, so the question can be asked, but Brave records
    nothing to answer it with. "We cannot tell" must survive that, or a user
    whose working browser is Brave is told it is the wrong one."""
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("brave", "Default", None)])
    entry = tools_browsers.detect_browsers()["profiles"][0]
    assert entry["matches_authorised_account"] is None


def test_a_signed_out_chrome_profile_reports_signed_out(survey, monkeypatch):
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", None)])
    entry = tools_browsers.detect_browsers()["profiles"][0]
    assert entry["signed_in"] is False
    assert entry["matches_authorised_account"] is False


def test_matching_is_unknown_when_no_account_has_authorised_yet(survey):
    """Nothing has been authorised, so nothing can be matched against."""
    survey([_candidate("chrome", "Default", OTHER)])
    entry = tools_browsers.detect_browsers()["profiles"][0]
    assert entry["signed_in"] is True
    assert entry["matches_authorised_account"] is None


def test_a_profile_without_the_extension_reports_false_not_none(survey,
                                                                monkeypatch):
    """The check ran and the answer is no. False, not None: None here means
    "could not check", and the two lead a user to different next steps.
    """
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", ACCOUNT),
            _candidate("brave", "Profile 2", None)])
    result = tools_browsers.detect_browsers()
    for entry in result["profiles"]:
        assert entry["has_extension"] is False
    assert result["recommended"]["has_extension"] is False


def test_the_profile_holding_the_extension_reports_true(survey, tmp_path):
    """The whole point of the field, against real files rather than a stub:
    a preferences file recording our extraction directory under a
    well-formed id."""
    from gsc_core import pairing

    root = tmp_path / "User Data"
    pdir = root / "Default"
    pdir.mkdir(parents=True)
    (pdir / "Secure Preferences").write_text(json.dumps(
        {"extensions": {"settings": {
            "a" * 32: {"path": str(pairing.extension_dir())}}}}),
        encoding="utf-8")

    paired = profiles.Profile(directory="Default", name="Personal",
                              email=None, path=str(pdir))
    survey([profiles.Candidate(installed=_installed("chrome"), profile=paired,
                               score=0, reasons=[]),
            _candidate("brave", "Profile 2", None)])

    by_key = {entry["browser_key"]: entry["has_extension"]
              for entry in tools_browsers.detect_browsers()["profiles"]}
    assert by_key == {"chrome": True, "brave": False}


def test_the_extension_id_itself_is_never_returned(survey, tmp_path):
    """32 characters naming one person's install. The tool answers whether
    the extension is there, not which install it is."""
    from gsc_core import pairing

    ext_id = "a" * 32
    root = tmp_path / "User Data"
    pdir = root / "Default"
    pdir.mkdir(parents=True)
    (pdir / "Secure Preferences").write_text(json.dumps(
        {"extensions": {"settings": {
            ext_id: {"path": str(pairing.extension_dir())}}}}),
        encoding="utf-8")
    paired = profiles.Profile(directory="Default", name="Personal",
                              email=None, path=str(pdir))
    survey([profiles.Candidate(installed=_installed("chrome"), profile=paired,
                               score=0, reasons=[])])

    result = tools_browsers.detect_browsers()
    assert result["profiles"][0]["has_extension"] is True
    assert ext_id not in _blob(result)


def test_has_extension_stays_none_when_the_check_cannot_be_performed(
        survey, monkeypatch):
    """No extraction directory means no profile was checked. Reporting
    False for all of them would be a fabrication that sends a user to
    reinstall an extension that is sitting right there."""
    def _explode():
        raise OSError("config dir is read-only")

    monkeypatch.setattr(tools_browsers.pairing, "extension_dir", _explode)
    survey([_candidate("chrome", "Default", None),
            _candidate("brave", "Profile 2", None)])
    result = tools_browsers.detect_browsers()
    assert result["ok"] is True
    for entry in result["profiles"]:
        assert entry["has_extension"] is None


# ---------------------------------------------------------------------------
# A machine with no browser is an ordinary state, not an error
# ---------------------------------------------------------------------------

def test_no_browsers_installed_is_a_friendly_answer(survey):
    survey([])
    result = tools_browsers.detect_browsers()
    assert result["ok"] is True
    assert result["profiles"] == []
    assert result["recommended"] is None
    assert result["note"]


# ---------------------------------------------------------------------------
# Nothing escapes to the MCP layer
# ---------------------------------------------------------------------------

def test_a_failing_survey_returns_the_structured_error_shape(monkeypatch):
    def boom():
        raise OSError(r"C:\Users\a-real-person\AppData\Local\State")

    monkeypatch.setattr(profiles, "survey", boom)
    result = tools_browsers.detect_browsers()
    assert result["ok"] is False
    assert result["error"] == "unexpected"
    assert result["detail"] == "OSError"
    assert result["fix"]


def test_a_failing_survey_leaks_no_path_from_the_exception_message(monkeypatch):
    def boom():
        raise OSError(r"C:\Users\a-real-person\AppData\Local\State")

    monkeypatch.setattr(profiles, "survey", boom)
    assert "a-real-person" not in _blob(tools_browsers.detect_browsers())


def test_a_failing_token_read_does_not_sink_the_tool(monkeypatch, survey):
    """An unreadable token file costs the match flag, not the answer."""

    def boom(path=None):
        raise OSError("unreadable")

    monkeypatch.undo()  # drop the autouse stub; exercise the real reader
    monkeypatch.setattr(tools_browsers.gauth, "load_token", boom)
    monkeypatch.setattr(profiles, "survey",
                        lambda: [_candidate("chrome", "Default", OTHER)])
    result = tools_browsers.detect_browsers()
    assert result["ok"] is True
    assert result["profiles"][0]["matches_authorised_account"] is None


# ---------------------------------------------------------------------------
# Where the authorised address comes from
# ---------------------------------------------------------------------------

def test_the_authorised_address_is_read_from_the_stored_token(monkeypatch,
                                                              survey):
    """The caller never names an address; the tool takes it from the token
    it already holds, so the match can be flagged without the address ever
    crossing the MCP boundary in either direction."""
    monkeypatch.undo()
    monkeypatch.setattr(tools_browsers.gauth, "load_token",
                        lambda path=None: {"account_email": ACCOUNT})
    monkeypatch.setattr(profiles, "survey",
                        lambda: [_candidate("chrome", "Default", ACCOUNT)])
    result = tools_browsers.detect_browsers()
    assert result["profiles"][0]["matches_authorised_account"] is True
    assert ACCOUNT not in _blob(result)


def test_no_token_file_means_matching_is_simply_unknown(monkeypatch, survey):
    monkeypatch.undo()
    monkeypatch.setattr(tools_browsers.gauth, "load_token",
                        lambda path=None: None)
    monkeypatch.setattr(profiles, "survey",
                        lambda: [_candidate("chrome", "Default", OTHER)])
    result = tools_browsers.detect_browsers()
    assert result["profiles"][0]["matches_authorised_account"] is None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_the_server_delegates_rather_than_reimplementing(survey, monkeypatch):
    from gsc_mcp import server

    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", ACCOUNT)])
    assert server.gsc_detect_browsers() == tools_browsers.detect_browsers()
