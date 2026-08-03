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
    allowed, reason = pairing.verify_pair_request(
        _installed(), _profile(), "not-an-id", None)
    assert allowed is False
    assert "malformed" in reason


def test_origin_that_disagrees_with_the_claim_is_refused(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(VALID_ID, True))
    allowed, reason = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, "chrome-extension://" + "b" * 32)
    assert allowed is False
    assert "Origin" in reason


def test_absent_extension_is_refused_with_a_load_unpacked_hint(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(None, True))
    allowed, reason = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, None)
    assert allowed is False
    assert "Load unpacked" in reason


def test_a_different_installed_extension_is_refused(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup("b" * 32, True))
    allowed, reason = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, None)
    assert allowed is False
    assert "b" * 32 in reason


def test_matching_id_and_origin_is_allowed(monkeypatch):
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(VALID_ID, True))
    allowed, reason = pairing.verify_pair_request(
        _installed(), _profile(), VALID_ID, "chrome-extension://" + VALID_ID)
    assert allowed is True
    assert reason


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
