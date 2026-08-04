"""gsc_use_browser (D8): the operator's override of the detector's ranking.

The detector ranks the profiles it can find. It cannot know which browser
the operator keeps their Search Console account in, and on the machine this
was written for it recommended Chrome while the extension had been loaded
into Brave. That is the whole feature.

Two claims are worth more than the rest of this file put together, and both
are about what must NOT happen:

  * a pin that matches nothing must never quietly fall back to the
    recommendation. Falling back drives a profile signed in as somebody
    else, and a Request Indexing submission cannot be taken back.
  * a pin that is refused must not be written. "Accept it and fail later"
    turns one typo into a browser that silently never opens, with nothing
    to tell the user which of the two it was.

Everything runs against a temporary GSC_MCP_HOME and a fabricated browser
install, so no real profile, config file or token is read or written.
"""
from __future__ import annotations

import pytest

from gsc_core import browsers, config, profiles
from gsc_mcp import onboarding, target, tools_browsers


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path / "home"))
    yield


def _candidate(tmp_path, *, brand_key="brave", directory="Default",
               email=None):
    user_data = tmp_path / "user-data" / brand_key
    profile_dir = user_data / directory
    profile_dir.mkdir(parents=True, exist_ok=True)
    installed = browsers.Installed(brand=browsers.BRANDS[brand_key],
                                   exe_path=str(tmp_path / "browser.exe"),
                                   user_data_dir=str(user_data))
    profile = profiles.Profile(directory=directory, name="Person 1",
                               email=email, path=str(profile_dir))
    return profiles.Candidate(installed=installed, profile=profile,
                              score=0, reasons=[])


@pytest.fixture
def surveyed(monkeypatch):
    """Install a fixed survey result; returns the setter.

    One patch covers every caller: onboarding, target and tools_browsers
    all hold the same gsc_core.profiles module object.
    """
    def _set(candidates):
        monkeypatch.setattr(profiles, "survey", lambda: list(candidates))
    return _set


def _pin(brand_key, directory="Default"):
    settings = config.load()
    settings["browser"] = brand_key
    settings["browser_profile"] = directory
    config.save(settings)


# ---------------------------------------------------------------------------
# The config field
# ---------------------------------------------------------------------------

def test_no_browser_is_pinned_by_default():
    """The detector's ranking is right for almost everyone, and a default
    that pins anything would make a first run depend on the developer's
    own machine."""
    assert config.DEFAULTS["browser"] is None
    assert config.DEFAULTS["browser_profile"] is None
    assert config.validate(config.DEFAULTS) == []


def test_a_profile_without_a_browser_is_rejected():
    """"Default" names a directory in every brand installed, so a profile
    pinned without a brand would resolve to whichever browser the survey
    happened to list first."""
    problems = config.validate(dict(config.DEFAULTS, browser_profile="Default"))
    assert any("browser_profile" in problem and "browser" in problem
               for problem in problems)


@pytest.mark.parametrize("value", ["", "   ", 5, True, []])
def test_a_pin_that_is_not_a_usable_name_is_rejected(value):
    assert config.validate(dict(config.DEFAULTS, browser=value))


def test_a_pinned_pair_validates():
    assert config.validate(dict(config.DEFAULTS, browser="brave",
                                browser_profile="Profile 3")) == []


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_without_a_pin_the_ranking_decides(tmp_path, monkeypatch):
    """The pin is an override, not a replacement — with none set, nothing
    about the existing ranking changes."""
    chrome = _candidate(tmp_path, brand_key="chrome")
    brave = _candidate(tmp_path, brand_key="brave")
    called = {}

    def _recommend(candidates, account_email=None):
        called["yes"] = True
        return chrome

    monkeypatch.setattr(profiles, "recommend", _recommend)
    selection = target.select([chrome, brave])
    assert called
    assert selection.candidate is chrome
    assert selection.pin is None
    assert selection.missing is False


def test_a_pin_beats_the_recommendation(tmp_path, monkeypatch):
    """A ranking that can override an explicit choice is not a preference,
    it is a suggestion. recommend() is not even called."""
    chrome = _candidate(tmp_path, brand_key="chrome")
    brave = _candidate(tmp_path, brand_key="brave")
    monkeypatch.setattr(profiles, "recommend",
                        lambda *a, **k: pytest.fail("the ranker was consulted"))
    _pin("brave")

    selection = target.select([chrome, brave])
    assert selection.candidate is brave
    assert selection.pin == "brave / Default"


def test_a_pin_is_matched_without_regard_to_case(tmp_path):
    """Being told your own browser does not exist over a capital letter is
    indefensible."""
    brave = _candidate(tmp_path, brand_key="brave")
    _pin("BRAVE", "DEFAULT")
    assert target.select([brave]).candidate is brave


def test_pinning_only_a_browser_takes_its_first_profile(tmp_path):
    """survey() is Default-first, which is what makes "just Brave" mean
    something rather than being arbitrary."""
    first = _candidate(tmp_path, brand_key="brave", directory="Default")
    second = _candidate(tmp_path, brand_key="brave", directory="Profile 2")
    settings = config.load()
    settings["browser"] = "brave"
    config.save(settings)

    selection = target.select([first, second])
    assert selection.candidate is first
    assert selection.pin == "brave"


def test_a_pin_that_matches_nothing_never_falls_back(tmp_path, monkeypatch):
    """THE claim of this file. The pin exists because the operator's Search
    Console account lives in one browser and not the other; silently using
    the recommended profile instead would submit URLs from whichever
    account happens to be signed in there, and that cannot be undone."""
    chrome = _candidate(tmp_path, brand_key="chrome")
    monkeypatch.setattr(profiles, "recommend", lambda *a, **k: chrome)
    _pin("brave")

    selection = target.select([chrome])
    assert selection.candidate is None
    assert selection.missing is True
    assert selection.pin == "brave / Default"


def test_resolve_refuses_rather_than_driving_a_different_profile(tmp_path,
                                                                surveyed,
                                                                monkeypatch):
    """The same claim one layer out, where it reaches the bridge."""
    chrome = _candidate(tmp_path, brand_key="chrome")
    surveyed([chrome])
    monkeypatch.setattr(profiles, "recommend", lambda *a, **k: chrome)
    _pin("brave")

    assert target.resolve() is None


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

def test_pinning_survives_the_process(tmp_path, surveyed):
    """An MCP server is respawned at every session start, so a preference
    held in memory is not a preference. Read back through config.load()
    rather than off the returned dict for exactly that reason."""
    surveyed([_candidate(tmp_path, brand_key="brave")])

    result = tools_browsers.use_browser(browser="brave", profile="Default")
    assert result["ok"] is True
    assert result["pinned"] == "brave / Default"

    settings = config.load()
    assert settings["browser"] == "brave"
    assert settings["browser_profile"] == "Default"


def test_the_stored_pin_is_normalised_to_what_the_detector_reports(tmp_path,
                                                                   surveyed):
    """Stored as the DETECTOR spells it, not as the user typed it: a pin
    recorded as "BRAVE" would read back correctly forever and still never
    equal a brand key in a config file the user is comparing by eye."""
    surveyed([_candidate(tmp_path, brand_key="brave")])
    tools_browsers.use_browser(browser="BrAvE", profile="default")
    assert config.load()["browser"] == "brave"
    assert config.load()["browser_profile"] == "Default"


def test_an_unknown_browser_is_refused_with_the_ones_that_exist(tmp_path,
                                                                surveyed):
    """Checked before it is saved. Accept-and-fail-later gives the user no
    way to tell a typo from a bug."""
    surveyed([_candidate(tmp_path, brand_key="chrome", directory="Profile 3")])

    result = tools_browsers.use_browser(browser="firefox")
    assert result["ok"] is False
    assert result["error"] == "browser_not_found"
    assert "chrome" in result["fix"] and "Profile 3" in result["fix"]
    assert config.load()["browser"] is None


def test_a_refused_pin_does_not_disturb_the_one_already_set(tmp_path,
                                                            surveyed):
    """A failed change must not be a change."""
    surveyed([_candidate(tmp_path, brand_key="brave")])
    tools_browsers.use_browser(browser="brave")

    tools_browsers.use_browser(browser="vivaldi")
    assert config.load()["browser"] == "brave"


def test_calling_it_with_nothing_says_what_to_pass(tmp_path, surveyed):
    surveyed([_candidate(tmp_path, brand_key="brave")])
    result = tools_browsers.use_browser()
    assert result["ok"] is False
    assert "gsc_use_browser" in result["fix"]
    assert config.load()["browser"] is None


def test_clearing_returns_to_the_recommendation(tmp_path, surveyed):
    surveyed([_candidate(tmp_path, brand_key="brave")])
    tools_browsers.use_browser(browser="brave")

    result = tools_browsers.use_browser(clear=True)
    assert result["ok"] is True
    assert result["pinned"] is None
    assert config.load()["browser"] is None
    assert config.load()["browser_profile"] is None


def test_saving_a_pin_keeps_every_other_setting(tmp_path, surveyed):
    """config.save() replaces the file, so a partial write would silently
    drop settings the user had tuned."""
    surveyed([_candidate(tmp_path, brand_key="brave")])
    settings = config.load()
    settings["property_slots"] = 4
    config.save(settings)

    tools_browsers.use_browser(browser="brave")
    assert config.load()["property_slots"] == 4


def test_the_pin_is_reported_by_detect_browsers(tmp_path, surveyed):
    """`recommended` answers "which one will be used", so under a pin it
    must be the pinned entry — and `pinned` says why, since otherwise the
    entry contradicts the ranking with no explanation."""
    chrome = _candidate(tmp_path, brand_key="chrome")
    brave = _candidate(tmp_path, brand_key="brave")
    surveyed([chrome, brave])
    tools_browsers.use_browser(browser="brave")

    report = tools_browsers.detect_browsers()
    assert report["pinned"] == "brave / Default"
    assert report["recommended"]["browser_key"] == "brave"


def test_detect_browsers_says_so_when_the_pin_is_dangling(tmp_path, surveyed):
    surveyed([_candidate(tmp_path, brand_key="brave")])
    tools_browsers.use_browser(browser="brave")
    surveyed([_candidate(tmp_path, brand_key="chrome")])

    report = tools_browsers.detect_browsers()
    assert report["ok"] is True
    assert report["recommended"] is None
    assert "gsc_use_browser" in report["note"]


def test_no_path_or_display_name_reaches_the_refusal(tmp_path, surveyed):
    """The refusal lists the pins that would work, and that list is the one
    new place a profile is described outward. Brand key plus profile
    directory, nothing else — a path carries the operator's account name."""
    candidate = _candidate(tmp_path, brand_key="chrome",
                           email="operator@example.com")
    surveyed([candidate])

    text = str(tools_browsers.use_browser(browser="firefox"))
    assert candidate.profile.path not in text
    assert "operator@example.com" not in text


# ---------------------------------------------------------------------------
# What setup and the doctor say about it
# ---------------------------------------------------------------------------

def test_the_doctor_names_the_pin_when_one_is_in_force(tmp_path, surveyed):
    """Without this the doctor reports a profile the ranker would not have
    picked and gives no hint why, which reads as a detection bug."""
    chrome = _candidate(tmp_path, brand_key="chrome")
    brave = _candidate(tmp_path, brand_key="brave")
    surveyed([chrome, brave])
    tools_browsers.use_browser(browser="brave")

    check = onboarding.check_browser()
    assert check["ok"] is True
    assert "Brave" in check["detail"]
    assert "pinned" in check["detail"]


def test_the_doctor_fails_loudly_on_a_dangling_pin(tmp_path, surveyed):
    """Green would be the dangerous answer here: nothing is going to drive
    a browser at all, and the fix is one call the user cannot guess."""
    surveyed([_candidate(tmp_path, brand_key="brave")])
    tools_browsers.use_browser(browser="brave")
    surveyed([_candidate(tmp_path, brand_key="chrome")])

    check = onboarding.check_browser()
    assert check["ok"] is False
    assert "brave" in check["detail"]
    assert "gsc_use_browser" in check["fix"]


def test_the_dangling_pin_remedy_is_not_the_no_browser_remedy(tmp_path,
                                                              surveyed):
    """Told to "install Chrome, Brave, Edge..." — the remedy for a machine
    with no browser at all — the user goes looking for a fault that is not
    there, when the fix is one call to gsc_use_browser."""
    assert onboarding._ACTION_PIN_MISSING != onboarding._ACTION_BROWSER
    assert "gsc_use_browser" in onboarding._ACTION_PIN_MISSING
    assert "install" not in onboarding._ACTION_PIN_MISSING

    surveyed([_candidate(tmp_path, brand_key="brave")])
    tools_browsers.use_browser(browser="brave")
    surveyed([_candidate(tmp_path, brand_key="chrome")])

    # Both checks that resolve a profile must agree; one green and one red
    # would be worse than either answer alone.
    assert onboarding.check_extension()["fix"] == onboarding._ACTION_PIN_MISSING
    assert onboarding.check_browser()["fix"] == onboarding._ACTION_PIN_MISSING


def test_the_extension_check_names_the_profile_that_does_have_it(tmp_path,
                                                                 surveyed,
                                                                 monkeypatch):
    """The single most likely way to reach "not installed": the extension
    goes wherever the browser was open at the time. Told only that it is
    missing from the selected profile, a user who has just installed it
    concludes the check is broken and installs it twice."""
    chrome = _candidate(tmp_path, brand_key="chrome")
    brave = _candidate(tmp_path, brand_key="brave")
    surveyed([chrome, brave])
    monkeypatch.setattr(profiles, "recommend", lambda *a, **k: chrome)
    monkeypatch.setattr(onboarding.pairing, "has_extension",
                        lambda installed, profile, ext_dir=None:
                        profile is brave.profile)

    check = onboarding.check_extension()
    assert check["ok"] is False
    assert "Brave" in check["detail"]
    assert "gsc_use_browser" in check["detail"]
