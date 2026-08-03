import re

import pytest

from gsc_core import browsers, pairing, profiles

VALID_ID = "a" * 32


def _installed():
    return browsers.Installed(brand=browsers.BRANDS["chrome"],
                              exe_path="C:/x/chrome.exe",
                              user_data_dir="C:/x/User Data")


def _profile():
    return profiles.Profile(directory="Default", name="Person 1",
                            email=None, path="C:/x/User Data/Default")


def test_malformed_id_is_refused_without_touching_the_profile(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not look up the profile for a malformed id")
    monkeypatch.setattr(pairing, "look_up_extension", explode)
    verdict = pairing.verify_pair_request(
        _installed(), _profile(), "not-an-id", None)
    assert verdict.allowed is False
    assert "malformed" in verdict.reason
    assert verdict.code is pairing.PairCode.MALFORMED_ID


def test_origin_that_disagrees_with_the_claim_is_refused(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(VALID_ID, True))
    verdict = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, "chrome-extension://" + "b" * 32)
    assert verdict.allowed is False
    assert "Origin" in verdict.reason
    assert verdict.code is pairing.PairCode.BAD_ORIGIN


def test_absent_extension_is_refused_with_a_load_unpacked_hint(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(None, True))
    verdict = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, None)
    assert verdict.allowed is False
    assert "Load unpacked" in verdict.reason
    assert verdict.code is pairing.PairCode.DIR_MISMATCH


def test_a_different_installed_extension_is_refused(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup("b" * 32, True))
    verdict = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, None)
    assert verdict.allowed is False
    assert "b" * 32 in verdict.reason
    assert verdict.code is pairing.PairCode.ID_MISMATCH


def test_matching_id_and_origin_is_allowed(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(VALID_ID, True))
    verdict = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, "chrome-extension://" + VALID_ID)
    assert verdict.allowed is True
    assert verdict.reason
    assert verdict.code is pairing.PairCode.OK


# ------------------------------------------------------- the closed vocabulary

def test_the_reason_code_vocabulary_is_closed_and_pinned():
    """The whole point of a code is that it CANNOT be built by interpolation.

    Pinned by name and by wire value: adding a branch without adding its
    member reddens here, and renaming a member breaks a log consumer, so
    both are deliberate acts rather than accidents.
    """
    assert {member.name: member.value for member in pairing.PairCode} == {
        "OK": "ok",
        "NO_TARGET": "no_target",
        "MALFORMED_ID": "malformed_id",
        "BAD_ORIGIN": "bad_origin",
        "DIR_MISMATCH": "dir_mismatch",
        "ID_MISMATCH": "id_mismatch",
    }


def test_a_code_cannot_be_invented_at_runtime():
    """A caller that can mint a code can mint one out of a path."""
    with pytest.raises(ValueError):
        pairing.PairCode("no_target_" + "a-real-person")
    with pytest.raises(AttributeError):
        pairing.PairCode.NO_TARGET.value = "leaked"


@pytest.mark.parametrize("member", list(pairing.PairCode))
def test_every_code_is_a_bare_lowercase_token(member):
    """No separators a path could hide in, no formatting placeholders."""
    assert re.fullmatch(r"[a-z][a-z_]*", member.value), member.value


def test_wake_reports_a_hint_and_never_raises_when_the_browser_will_not_start(
        monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(VALID_ID, True))

    def boom(*a, **k):
        raise OSError("C:/Users/a-real-person/chrome.exe is missing")
    monkeypatch.setattr(pairing.subprocess, "Popen", boom)

    result = pairing.wake(_installed(), _profile())
    assert result["ok"] is False
    assert "a-real-person" not in result["hint"]


def test_wake_launches_the_extension_page(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(VALID_ID, True))
    calls = []
    monkeypatch.setattr(pairing.subprocess, "Popen",
                        lambda argv, **k: calls.append(argv))
    result = pairing.wake(_installed(), _profile())
    assert result["ok"] is True
    assert calls[0][1] == f"chrome-extension://{VALID_ID}/connect.html"
