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

import pytest

from gsc_core import browsers, profiles
from gsc_mcp import tools_browsers

ACCOUNT = "authorised@example.com"
OTHER = "someone-else@example.net"


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
    """No token on disk unless a test says otherwise."""
    monkeypatch.setattr(tools_browsers, "_authorised_email", lambda: None)


def _signed_in_account(monkeypatch, address):
    monkeypatch.setattr(tools_browsers, "_authorised_email", lambda: address)


def _blob(result) -> str:
    """Everything the transcript would carry, as one searchable string."""
    return json.dumps(result, default=str)


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


def test_two_identical_calls_return_identical_results(survey, monkeypatch):
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Profile 1", None),
            _candidate("chrome", "Profile 2", None)])
    assert tools_browsers.detect_browsers() == tools_browsers.detect_browsers()


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


def test_every_profile_carries_the_has_extension_placeholder(survey,
                                                             monkeypatch):
    """Task 8 populates has_extension once pairing exists. The key ships
    NOW, as None, so its type never changes on a consumer: a key added to
    an already-published shape changes the contract under anything reading
    it in the meantime. This test is what stops it being deleted as dead
    weight before Task 8 arrives.
    """
    _signed_in_account(monkeypatch, ACCOUNT)
    survey([_candidate("chrome", "Default", ACCOUNT),
            _candidate("brave", "Profile 2", None)])
    result = tools_browsers.detect_browsers()
    for entry in result["profiles"]:
        assert "has_extension" in entry
        assert entry["has_extension"] is None
    assert result["recommended"]["has_extension"] is None


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
