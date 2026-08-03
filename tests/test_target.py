"""Which browser, which profile, which extension — resolved once.

The value of this module is that it does NOT re-derive the ranking. Every
test below is therefore about deferring correctly: to profiles.recommend()
for the choice, and to pairing.look_up_extension() for the ID. The second
group is about what a resolved target is allowed to say out loud — a
profile path contains the user's Windows account name, and a profile label
is frequently the signed-in address.
"""
from __future__ import annotations

import pytest

from _logcheck import capturing
from gsc_core import browsers, profiles
from gsc_mcp import target

EXT_ID = "a" * 32


def _candidate(brand_key: str = "chrome", directory: str = "Default",
               email: str | None = None,
               name: str | None = None) -> profiles.Candidate:
    installed = browsers.Installed(brand=browsers.BRANDS[brand_key],
                                   exe_path="/nonexistent",
                                   user_data_dir="/nonexistent")
    profile = profiles.Profile(directory=directory,
                               name=name if name is not None else directory,
                               email=email, path="/nonexistent/profile")
    return profiles.Candidate(installed=installed, profile=profile,
                              score=0, reasons=[])


@pytest.fixture
def one(monkeypatch):
    """A machine with exactly one candidate, and the extension installed."""
    candidate = _candidate()
    monkeypatch.setattr(target.profiles, "survey", lambda: [candidate])
    monkeypatch.setattr(target.profiles, "recommend",
                        lambda candidates, account_email=None: candidate)
    monkeypatch.setattr(target.pairing, "look_up_extension",
                        lambda *a, **k: target.pairing.Lookup(EXT_ID, True))
    return candidate


# --- resolving ---------------------------------------------------------------

def test_resolve_returns_none_when_no_browser_is_installed(monkeypatch):
    monkeypatch.setattr(target.profiles, "survey", lambda: [])
    assert target.resolve() is None


def test_resolve_uses_the_recommended_candidate(one):
    resolved = target.resolve()
    assert resolved.installed is one.installed
    assert resolved.profile is one.profile
    assert resolved.extension_id == EXT_ID


def test_resolve_does_not_re_rank_what_recommend_chose(monkeypatch):
    """The defect this module exists to prevent is two rankings disagreeing."""
    first, second = _candidate(directory="Profile 1"), _candidate(
        directory="Profile 2")
    monkeypatch.setattr(target.profiles, "survey", lambda: [first, second])
    monkeypatch.setattr(target.profiles, "recommend",
                        lambda candidates, account_email=None: second)
    monkeypatch.setattr(target.pairing, "look_up_extension",
                        lambda *a, **k: target.pairing.Lookup(EXT_ID, True))
    assert target.resolve().profile.directory == "Profile 2"


def test_the_account_is_passed_to_the_ranking(monkeypatch):
    """Without it the ranker cannot prefer the profile signed in as the user."""
    seen: dict = {}
    candidate = _candidate()
    monkeypatch.setattr(target.profiles, "survey", lambda: [candidate])

    def recommend(candidates, account_email=None):
        seen["account_email"] = account_email
        return candidate

    monkeypatch.setattr(target.profiles, "recommend", recommend)
    monkeypatch.setattr(target.pairing, "look_up_extension",
                        lambda *a, **k: target.pairing.Lookup(EXT_ID, True))
    target.resolve("me@example.com")
    assert seen["account_email"] == "me@example.com"


def test_resolve_still_returns_a_target_when_the_extension_is_absent(monkeypatch):
    # A target with no extension is exactly what the bridge needs in order to
    # answer a pair_request — refusing to build one would make first-time
    # pairing impossible.
    candidate = _candidate()
    monkeypatch.setattr(target.profiles, "survey", lambda: [candidate])
    monkeypatch.setattr(target.profiles, "recommend",
                        lambda candidates, account_email=None: candidate)
    monkeypatch.setattr(target.pairing, "look_up_extension",
                        lambda *a, **k: target.pairing.Lookup(None, True))
    resolved = target.resolve()
    assert resolved is not None
    assert resolved.extension_id is None


def test_a_survey_that_explodes_is_not_a_crash(monkeypatch):
    """Detection touches other programs' files. It is allowed to fail."""
    def boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(target.profiles, "survey", boom)
    with capturing(target.log) as records:
        assert target.resolve() is None
    records.assert_says_nothing_identifying()


def test_a_failed_extension_lookup_still_yields_a_target(monkeypatch):
    candidate = _candidate()
    monkeypatch.setattr(target.profiles, "survey", lambda: [candidate])
    monkeypatch.setattr(target.profiles, "recommend",
                        lambda candidates, account_email=None: candidate)

    def boom(*a, **k):
        raise RuntimeError("preferences unreadable")

    monkeypatch.setattr(target.pairing, "look_up_extension", boom)
    with capturing(target.log) as records:
        resolved = target.resolve()
    assert resolved is not None
    assert resolved.extension_id is None
    records.assert_says_nothing_identifying()


# --- what a target may say out loud ------------------------------------------

def test_describe_names_the_browser_and_profile_without_the_path(one):
    described = target.describe(target.resolve())
    assert described["browser"] == one.installed.brand.label
    assert described["profile"] == "Default"
    assert "/nonexistent/profile" not in str(described)


def test_describe_never_repeats_a_profile_label_that_is_an_address(monkeypatch):
    """Chrome labels profiles with the signed-in account often enough."""
    candidate = _candidate(directory="Profile 2", name="me@example.com",
                           email="me@example.com")
    monkeypatch.setattr(target.profiles, "survey", lambda: [candidate])
    monkeypatch.setattr(target.profiles, "recommend",
                        lambda candidates, account_email=None: candidate)
    monkeypatch.setattr(target.pairing, "look_up_extension",
                        lambda *a, **k: target.pairing.Lookup(EXT_ID, True))
    described = target.describe(target.resolve())
    assert "@" not in str(described)
    assert described["profile"] == "Profile 2"


def test_describe_says_so_when_there_is_no_target():
    described = target.describe(None)
    assert described["browser"] is None
    assert described["paired"] is False


def test_describe_reports_pairing_state(one, monkeypatch):
    assert target.describe(target.resolve())["paired"] is True
    monkeypatch.setattr(target.pairing, "look_up_extension",
                        lambda *a, **k: target.pairing.Lookup(None, True))
    assert target.describe(target.resolve())["paired"] is False


def test_resolving_says_nothing_identifying_on_the_happy_path(one):
    with capturing(target.log) as records:
        target.resolve("me@example.com")
    blob = records.text
    assert "me@example.com" not in blob
    assert "/nonexistent/profile" not in blob
