"""Layer 3: ranking candidates and recommending one.

The interesting case is not the happy path. It is Brave: it ships no Google
Sync, so ``account_info`` stays empty even for a user who is signed into
Google in that browser. "We cannot tell" is a fact about the BRAND, and a
ranker that scores it as "signed out" buries the browser this toolkit
actually drives.
"""
from __future__ import annotations

import dataclasses

import pytest

from gsc_core import browsers, profiles


def _candidate(brand_key, directory, email):
    installed = browsers.Installed(brand=browsers.BRANDS[brand_key],
                                   exe_path="/nonexistent",
                                   user_data_dir="/nonexistent")
    profile = profiles.Profile(directory=directory, name=directory,
                               email=email, path="/nonexistent")
    return profiles.Candidate(installed=installed, profile=profile,
                              score=0, reasons=[])


# ---------------------------------------------------------------------------
# The brief's rules
# ---------------------------------------------------------------------------

def test_the_profile_signed_in_as_the_oauth_account_wins():
    candidates = [_candidate("chrome", "Default", "other@example.com"),
                  _candidate("brave", "Profile 2", "me@example.com")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.installed.brand.key == "brave"
    assert best.profile.directory == "Profile 2"


def test_the_match_beats_brand_preference():
    """Chrome ranks above Vivaldi on brand alone; the account must override."""
    candidates = [_candidate("chrome", "Default", None),
                  _candidate("vivaldi", "Default", "me@example.com")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.installed.brand.key == "vivaldi"


def test_a_signed_in_profile_beats_a_signed_out_one():
    candidates = [_candidate("chrome", "Profile 1", None),
                  _candidate("chrome", "Profile 2", "someone@example.com")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.profile.directory == "Profile 2"


def test_the_recommendation_explains_itself():
    candidates = [_candidate("brave", "Profile 2", "me@example.com")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert any("me@example.com" in reason for reason in best.reasons)


def test_no_browsers_means_no_recommendation_not_a_crash():
    assert profiles.recommend([], account_email="me@example.com") is None


def test_ranking_is_deterministic_for_identical_candidates():
    candidates = [_candidate("chrome", "Profile 1", None),
                  _candidate("chrome", "Profile 2", None)]
    first = profiles.recommend(list(candidates))
    second = profiles.recommend(list(reversed(candidates)))
    assert first.profile.directory == second.profile.directory


def test_email_matching_is_case_insensitive():
    candidates = [_candidate("chrome", "Default", "Me@Example.COM")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.score >= 100


def test_brand_order_breaks_a_tie_between_equally_signed_out_profiles():
    """Both brands record accounts, so both are genuinely signed out."""
    candidates = [_candidate("edge", "Default", None),
                  _candidate("chrome", "Default", None)]
    best = profiles.recommend(candidates)
    assert best.installed.brand.key == "chrome"


def test_brand_order_breaks_a_tie_between_equally_unknowable_profiles():
    candidates = [_candidate("chromium", "Default", None),
                  _candidate("brave", "Default", None)]
    best = profiles.recommend(candidates)
    assert best.installed.brand.key == "brave"


def test_a_brand_outside_the_table_sorts_last_rather_than_first():
    """The only pair that can tie on score alone.

    Every brand in BRANDS carries a distinct preference bonus, so two known
    brands can never tie. An unknown brand earns no bonus — the same as
    Chromium — and must still lose to it rather than winning by accident of
    list order.
    """
    stranger = dataclasses.replace(browsers.BRANDS["chromium"],
                                   key="stranger", label="Stranger")
    known = _candidate("chromium", "Default", None)
    unknown = profiles.Candidate(
        installed=browsers.Installed(brand=stranger, exe_path="/nonexistent",
                                     user_data_dir="/nonexistent"),
        profile=known.profile, score=0, reasons=[])

    assert profiles.recommend([unknown, known]).installed.brand.key == "chromium"
    assert profiles.recommend([known, unknown]).installed.brand.key == "chromium"


def test_the_default_profile_breaks_the_final_tie():
    candidates = [_candidate("chrome", "Profile 1", None),
                  _candidate("chrome", "Default", None)]
    best = profiles.recommend(candidates)
    assert best.profile.directory == "Default"


def test_numbered_profiles_sort_numerically_not_lexically():
    candidates = [_candidate("chrome", "Profile 10", None),
                  _candidate("chrome", "Profile 2", None)]
    best = profiles.recommend(candidates)
    assert best.profile.directory == "Profile 2"


# ---------------------------------------------------------------------------
# "We cannot tell" is not "nobody is signed in"
# ---------------------------------------------------------------------------

def test_a_brand_that_cannot_report_accounts_is_not_scored_as_signed_out():
    """The real-hardware case: Brave reports no account for any profile.

    Chrome, signed into an unrelated account, must NOT outrank it — that
    would bury the browser the submission toolkit actually drives on a fact
    about Brave rather than about the profile.
    """
    candidates = [_candidate("chrome", "Default", "someone@example.com"),
                  _candidate("brave", "Default", None)]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.installed.brand.key == "brave"


def test_an_undiscoverable_account_never_outranks_a_confirmed_match():
    candidates = [_candidate("brave", "Default", None),
                  _candidate("chromium", "Profile 9", "me@example.com")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.installed.brand.key == "chromium"


def test_a_signed_out_profile_of_a_reporting_brand_stays_signed_out():
    """Chrome does record accounts, so None there really means signed out."""
    candidates = [_candidate("chrome", "Default", None),
                  _candidate("chromium", "Default", "someone@example.com")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.installed.brand.key == "chromium"


def test_the_undiscoverable_case_says_so_rather_than_hiding_in_a_number():
    candidates = [_candidate("brave", "Default", None)]
    best = profiles.recommend(candidates, account_email="me@example.com")
    joined = " ".join(best.reasons).lower()
    assert "brave" in joined
    assert "not record" in joined or "cannot" in joined


def test_a_reporting_brand_signed_out_does_not_claim_it_could_not_check():
    candidates = [_candidate("chrome", "Default", None)]
    best = profiles.recommend(candidates)
    joined = " ".join(best.reasons).lower()
    assert "not record" not in joined and "cannot" not in joined


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------

def test_an_unrelated_address_is_not_echoed_into_the_reasons():
    candidates = [_candidate("chrome", "Default", "someone@example.com")]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert not any("someone@example.com" in reason for reason in best.reasons)


def test_ranking_writes_nothing_to_stdout(capsys):
    profiles.recommend([_candidate("brave", "Default", None)],
                       account_email="me@example.com")
    assert capsys.readouterr().out == ""


def test_no_address_reaches_the_log(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    profiles.recommend([_candidate("chrome", "Default", "me@example.com")],
                       account_email="me@example.com")
    assert "me@example.com" not in caplog.text


# ---------------------------------------------------------------------------
# survey()
# ---------------------------------------------------------------------------

def test_survey_pairs_every_profile_with_its_browser(monkeypatch):
    chrome = browsers.Installed(brand=browsers.BRANDS["chrome"],
                                exe_path="/nonexistent",
                                user_data_dir="/nonexistent")
    profile = profiles.Profile(directory="Default", name="Default",
                               email=None, path="/nonexistent")
    monkeypatch.setattr(browsers, "detect", lambda: [chrome])
    monkeypatch.setattr(profiles, "list_profiles", lambda inst: [profile])

    found = profiles.survey()

    assert [(c.installed.brand.key, c.profile.directory) for c in found] == [
        ("chrome", "Default")]


def test_survey_is_empty_rather_than_raising_when_nothing_is_installed(
        monkeypatch):
    monkeypatch.setattr(browsers, "detect", lambda: [])
    assert profiles.survey() == []


def test_one_unreadable_browser_does_not_lose_the_others(monkeypatch):
    good = browsers.Installed(brand=browsers.BRANDS["edge"],
                              exe_path="/nonexistent",
                              user_data_dir="/nonexistent")
    bad = browsers.Installed(brand=browsers.BRANDS["opera"],
                             exe_path="/nonexistent",
                             user_data_dir="/nonexistent")
    profile = profiles.Profile(directory="Default", name="Default",
                               email=None, path="/nonexistent")

    def _listing(installed):
        if installed.brand.key == "opera":
            raise OSError("boom")
        return [profile]

    monkeypatch.setattr(browsers, "detect", lambda: [good, bad])
    monkeypatch.setattr(profiles, "list_profiles", _listing)

    assert [c.installed.brand.key for c in profiles.survey()] == ["edge"]


def test_survey_survives_detection_blowing_up(monkeypatch):
    def _boom():
        raise OSError("boom")

    monkeypatch.setattr(browsers, "detect", _boom)
    assert profiles.survey() == []


def test_recommend_surveys_the_machine_when_given_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(profiles, "survey", lambda: called.append(1) or [])
    assert profiles.recommend() is None
    assert called == [1]


# ---------------------------------------------------------------------------
# No authorised address — the state EVERY real machine is in today
# ---------------------------------------------------------------------------
#
# The current scope set (webmasters only) returns no identity claim, so
# nothing ever writes account_email and every call below arrives with None.
# Until these tests existed, all thirteen ranking tests supplied an address,
# and the untested branch is the one that shipped wrong: an unknowable brand
# outranked a profile with a Google account visibly signed in.

def test_a_visible_google_account_beats_an_unknowable_brand_when_none_is_authorised():
    # The whole point. Chrome shows a Google account; Brave shows nothing
    # because Brave cannot show anything. Preferring Brave here means
    # preferring the browser we know LESS about, and on a machine where
    # Chrome holds the only Search Console session it is simply wrong.
    candidates = [_candidate("chrome", "Default", "someone@example.com"),
                  _candidate("brave", "Default", None)]
    best = profiles.recommend(candidates, account_email=None)
    assert best.installed.brand.key == "chrome"


def test_no_reason_claims_a_mismatch_that_was_never_checked():
    # "though not the one you authorised" asserts a comparison. With no
    # authorised address there was no comparison, so the sentence is a
    # statement the code cannot support -- and it is shown to the user.
    candidates = [_candidate("chrome", "Default", "someone@example.com")]
    best = profiles.recommend(candidates, account_email=None)
    assert not any("you authorised" in reason for reason in best.reasons)


def test_an_unrelated_address_is_still_not_echoed_when_none_is_authorised():
    candidates = [_candidate("chrome", "Default", "someone@example.com")]
    best = profiles.recommend(candidates, account_email=None)
    assert not any("someone@example.com" in reason for reason in best.reasons)


def test_an_unknowable_brand_still_beats_a_reporting_brand_signed_out():
    # The original rationale, which is correct and must survive the fix:
    # Brave's empty account_info is a fact about the BRAND, so it must not
    # lose to a Chrome profile that is provably signed out.
    candidates = [_candidate("chrome", "Default", None),
                  _candidate("brave", "Profile 2", None)]
    best = profiles.recommend(candidates, account_email=None)
    assert best.installed.brand.key == "brave"


def test_a_known_mismatch_still_ranks_below_an_unknowable_brand():
    # The other half of the original rationale. When an address IS known,
    # "signed in as somebody else" is worse than "cannot tell", because
    # the wrong account is positive evidence of a session that will fail.
    candidates = [_candidate("chrome", "Default", "someone@example.com"),
                  _candidate("brave", "Profile 2", None)]
    best = profiles.recommend(candidates, account_email="me@example.com")
    assert best.installed.brand.key == "brave"


# ---------------------------------------------------------------------------
# Brand facts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["chrome", "edge"])
def test_google_sync_brands_are_marked_as_reporting_accounts(key):
    assert browsers.BRANDS[key].reports_google_account is True


@pytest.mark.parametrize("key", ["brave", "vivaldi", "opera", "chromium"])
def test_brands_without_google_sync_are_marked_as_not_reporting(key):
    assert browsers.BRANDS[key].reports_google_account is False
