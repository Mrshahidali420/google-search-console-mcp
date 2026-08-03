"""gsc_request_indexing: the synchronous tool.

Every test here pins a CONSEQUENCE. This tool spends a real, unrecoverable
resource (roughly eleven submissions per property per rolling day) and its
result travels to an MCP client this project does not control, so the two
questions that matter are what reached the ledger and what reached the
transcript. "the dict has the key the constructor put there" would pass
against an implementation that submitted six URLs and returned the user's
email address alongside them.

NOTHING HERE SLEEPS. The real gap between two sends is 130-180 seconds. The
clock is injected by replacing the module's own `time` attribute, which is
why the implementation must resolve `time.sleep` at call time rather than
letting submit.run bind the real one as a default argument.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import pytest

from _logcheck import capturing
from gsc_core import bridge, browsers, config, paths, profiles, store, submit
from gsc_mcp import jobs, target, tools_submit

PROPERTY = "sc-domain:example.com"

# Deliberately identifying: a profile Chrome labelled with the signed-in
# address, sitting under a path that names the operator's account. Both are
# real shapes, and both must be absent from anything the tool returns.
PROFILE_EMAIL = "owner@example.net"
PROFILE_PATH = "/home/secret-operator/browser/Profile 1"


class _Clock:
    """A stand-in for the `time` module that records instead of waiting."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


class _Sender:
    """Replies from a script and records what it was asked to submit.

    Popping from an empty script fails loudly: a test expecting one send
    and getting two should say so rather than silently reusing an outcome.
    """

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, str]] = []

    def submit(self, property: str, url: str, authuser: str) -> str:
        self.calls.append((property, url, authuser))
        assert self.outcomes, f"unscripted submit of {url}"
        return self.outcomes.pop(0)


def _fake_session(outcomes: list[str]):
    sender = _Sender(outcomes)

    @contextmanager
    def session(chosen, cfg):
        yield sender

    session.sender = sender
    return session


def _target() -> target.Target:
    brand = browsers.BRANDS["chrome"]
    installed = browsers.Installed(brand=brand, exe_path="/opt/chrome/chrome",
                                   user_data_dir="/home/secret-operator/browser")
    profile = profiles.Profile(directory="Profile 1", name=PROFILE_EMAIL,
                               email=PROFILE_EMAIL, path=PROFILE_PATH)
    return target.Target(installed=installed, profile=profile,
                         extension_id="abcdefghijklmnopabcdefghijklmnop")


def _seed_site(conn, property: str = PROPERTY) -> None:
    store.upsert_site(conn, property, property.removeprefix("sc-domain:"),
                      "siteOwner", [])


def _fill_slots(conn, count: int, *, property: str = PROPERTY,
                account: str = "default") -> None:
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    for index in range(count):
        conn.execute(
            "INSERT INTO quota_slots (account, property, used_at) VALUES (?,?,?)",
            (account, property, store.utc_iso(now - timedelta(minutes=index))))
    conn.commit()


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _boom(*args, **kwargs):
    raise AssertionError("this should never have been reached")


def _ready(monkeypatch, outcomes: list[str], clock: _Clock | None = None):
    """The happy-path wiring: a resolved target and a scripted bridge."""
    monkeypatch.setattr(tools_submit.target, "resolve", lambda *a, **k: _target())
    session = _fake_session(outcomes)
    monkeypatch.setattr(tools_submit.bridge, "bridge_session", session)
    monkeypatch.setattr(tools_submit, "time", clock or _Clock())
    return session


# --- the cap -----------------------------------------------------------------

def test_more_than_five_urls_is_refused_before_anything_opens(monkeypatch,
                                                              store_conn):
    """The cap is a refusal, not a truncation, and it costs nothing."""
    _seed_site(store_conn)
    monkeypatch.setattr(tools_submit.target, "resolve", _boom)
    monkeypatch.setattr(tools_submit.bridge, "bridge_session", _boom)

    result = tools_submit.request_indexing(
        [f"https://example.com/{index}" for index in range(6)])

    assert result["ok"] is False
    assert "5" in result["detail"]
    assert result["fix"]
    # Not one row opened and not one slot spent: refusing after reserving
    # would burn quota on URLs that were never sent.
    assert store.open_submissions(store_conn) == []
    assert _count(store_conn, "submissions") == 0
    assert _count(store_conn, "quota_slots") == 0


def test_exactly_five_urls_is_not_refused(monkeypatch, store_conn):
    """The boundary belongs to the caller: five is allowed, six is not."""
    _seed_site(store_conn)
    _ready(monkeypatch, ["submitted"] * 5)
    result = tools_submit.request_indexing(
        [f"https://example.com/{index}" for index in range(5)])
    assert result["ok"] is True
    assert result["submitted"] == 5


def test_config_may_lower_the_cap(monkeypatch, store_conn):
    paths.config_path().write_text(json.dumps({"sync_submit_cap": 2}),
                                   encoding="utf-8")
    monkeypatch.setattr(tools_submit.target, "resolve", _boom)
    result = tools_submit.request_indexing([f"https://example.com/{i}"
                                            for i in range(3)])
    assert result["ok"] is False
    assert "2" in result["detail"]


def test_config_may_not_raise_the_cap_above_the_hard_ceiling(monkeypatch,
                                                             store_conn):
    """A blocking tool that waits half an hour is a hung client, not a feature.

    Pinned by CONSEQUENCE, and every part of the wiring is a working happy
    path: a resolvable browser, a bridge that would yield, a sender with six
    "submitted" replies ready. Everything needed for six real submissions is
    in place, so the only thing that can stop them is the ceiling.

    target.resolve is a SPY, not a raiser. A raiser turns "the ceiling is
    gone" into a refusal of its own, and an assertion on the refusal then
    holds whatever the cap does — which is precisely how the earlier version
    of this test survived `max(1, configured)` with the min() deleted.
    """
    paths.config_path().write_text(json.dumps({"sync_submit_cap": 50}),
                                   encoding="utf-8")
    _seed_site(store_conn)

    resolved: list[int] = []
    monkeypatch.setattr(tools_submit.target, "resolve",
                        lambda *a, **k: resolved.append(1) or _target())
    opened: list[int] = []
    sender = _Sender(["submitted"] * 6)

    @contextmanager
    def session(chosen, cfg):
        opened.append(1)
        yield sender

    monkeypatch.setattr(tools_submit.bridge, "bridge_session", session)
    monkeypatch.setattr(tools_submit, "time", _Clock())

    result = tools_submit.request_indexing([f"https://example.com/{i}"
                                            for i in range(6)])

    assert result["ok"] is False
    # The number the caller is held to is the hard one, not their config's.
    assert "at most 5" in result["detail"]
    # Refused whole and refused early: no browser looked for, no bridge
    # opened, nothing sent.
    assert resolved == []
    assert opened == []
    assert sender.calls == []
    # And nothing reached the ledger — no open row, and no spent slot.
    assert _count(store_conn, "submissions") == 0
    assert _count(store_conn, "quota_slots") == 0
    assert tools_submit.HARD_SYNC_CAP == 5


@pytest.mark.parametrize("configured", [0, -1])
def test_a_nonsense_cap_still_lets_one_url_through(monkeypatch, store_conn,
                                                   configured):
    """config.load() does not run validate(), so 0 reaches this code.

    A cap of zero would refuse every call with "takes at most 0", which
    reads as a broken tool rather than as a configuration mistake.
    """
    _seed_site(store_conn)
    paths.config_path().write_text(json.dumps({"sync_submit_cap": configured}),
                                   encoding="utf-8")
    _ready(monkeypatch, ["submitted"])
    assert tools_submit.request_indexing(["https://example.com/a"])["ok"] is True


def test_a_cap_that_is_not_a_number_says_so_instead_of_failing_generically(
        monkeypatch, store_conn):
    _seed_site(store_conn)
    paths.config_path().write_text(json.dumps({"sync_submit_cap": None}),
                                   encoding="utf-8")
    monkeypatch.setattr(tools_submit.target, "resolve", _boom)
    result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["ok"] is False
    assert result["error"] == "bad_config"
    assert "sync_submit_cap" in result["fix"]


def test_an_empty_url_list_is_refused(monkeypatch, store_conn):
    monkeypatch.setattr(tools_submit.target, "resolve", _boom)
    result = tools_submit.request_indexing([])
    assert result["ok"] is False
    assert result["fix"]


# --- one run at a time -------------------------------------------------------

def test_a_sync_call_is_refused_while_a_background_job_holds_the_bridge(
        monkeypatch, store_conn):
    """Both entry points drive one browser tab through one fixed port.

    Allowed to proceed, this call would bind a port the job already holds —
    inside a daemon thread where the OSError is invisible — then sit out its
    whole connect timeout and blame the extension. The refusal has to be
    immediate and free: no browser resolved, no bridge opened, no slot spent.
    """
    _seed_site(store_conn)
    running = threading.Event()
    finish = threading.Event()

    def blocking(conn, job_id, urls, cfg, stop_event, on_progress):
        running.set()
        assert finish.wait(10), "the job was never released"
        return submit.RunResult([], True, "stopped_by_user")

    monkeypatch.setattr(jobs, "_execute", blocking)
    job_id = jobs.start(["https://example.com/job"], {})
    try:
        assert running.wait(10), "the job never started"
        monkeypatch.setattr(tools_submit.target, "resolve", _boom)
        monkeypatch.setattr(tools_submit.bridge, "bridge_session", _boom)

        result = tools_submit.request_indexing(["https://example.com/a"])

        assert result["ok"] is False
        assert result["error"] == "job_already_running"
        assert job_id in result["detail"]
        assert result["fix"]
        assert _count(store_conn, "submissions") == 0
        assert _count(store_conn, "quota_slots") == 0
    finally:
        finish.set()
        jobs.stop(job_id)
        assert jobs.join(job_id, timeout=10)
        jobs._threads.clear()
        jobs._stop_events.clear()
        jobs._holder = None


def test_the_sync_call_gives_the_bridge_back_when_it_is_done(monkeypatch,
                                                             store_conn):
    """Otherwise the first submission of the session would be the last."""
    _seed_site(store_conn)
    _ready(monkeypatch, ["submitted"])
    assert tools_submit.request_indexing(["https://example.com/a"])["ok"] is True
    assert jobs._holder is None


def test_the_bridge_is_given_back_even_when_the_run_fails(monkeypatch,
                                                          store_conn):
    _seed_site(store_conn)
    monkeypatch.setattr(tools_submit.target, "resolve", lambda *a, **k: _target())
    monkeypatch.setattr(tools_submit.bridge, "bridge_session",
                        _raising_session(RuntimeError("/home/someone/thing")))
    assert tools_submit.request_indexing(["https://example.com/a"])["ok"] is False
    assert jobs._holder is None


# --- setup failures ----------------------------------------------------------

def test_a_missing_browser_is_a_clean_error_not_a_traceback(monkeypatch,
                                                            store_conn):
    monkeypatch.setattr(tools_submit.target, "resolve", lambda *a, **k: None)
    monkeypatch.setattr(tools_submit.bridge, "bridge_session", _boom)
    result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["ok"] is False
    assert result["fix"]
    assert _count(store_conn, "submissions") == 0


def test_no_known_properties_is_its_own_error(monkeypatch, store_conn):
    """Distinct from "no browser": the fix is a different tool."""
    monkeypatch.setattr(tools_submit.target, "resolve", lambda *a, **k: _target())
    monkeypatch.setattr(tools_submit.bridge, "bridge_session", _boom)
    result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["ok"] is False
    assert result["error"] == "no_properties"


def _raising_session(exc: BaseException):
    @contextmanager
    def refuses(chosen, cfg):
        raise exc
        yield  # pragma: no cover

    return refuses


def test_an_extension_that_never_connects_is_a_clean_error(monkeypatch,
                                                           store_conn):
    _seed_site(store_conn)
    monkeypatch.setattr(tools_submit.target, "resolve", lambda *a, **k: _target())
    monkeypatch.setattr(tools_submit.bridge, "bridge_session",
                        _raising_session(bridge.ExtensionNotConnected(
                            "the extension never connected")))
    result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["ok"] is False
    assert result["error"] == "extension_not_connected"
    assert "extension" in result["detail"]
    assert _count(store_conn, "submissions") == 0


def test_only_the_dedicated_bridge_failure_gets_its_message_repeated(
        monkeypatch, store_conn):
    """The str(exc) exemption is scoped to ONE exception type we author.

    bridge.load_or_create_token raises a plain RuntimeError for a different
    problem entirely, and a plain RuntimeError is also what any future
    change might start raising with a filesystem path in it.
    """
    _seed_site(store_conn)
    monkeypatch.setattr(tools_submit.target, "resolve", lambda *a, **k: _target())
    monkeypatch.setattr(tools_submit.bridge, "bridge_session",
                        _raising_session(RuntimeError(
                            f"could not save the token under {PROFILE_PATH}")))
    result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["error"] != "extension_not_connected"
    assert result["detail"] == "RuntimeError"
    assert "secret-operator" not in json.dumps(result)


def test_a_failure_inside_the_run_is_never_mistaken_for_a_bridge_failure(
        monkeypatch, store_conn):
    """The exemption must not cover the run body.

    A RuntimeError out of submit.run is not a bridge problem, and its
    message is not one this project wrote — so neither the label nor the
    text may be reused. Unreachable today only because submit.run swallows
    sender exceptions; that is luck, and this pins the structure instead.
    """
    _seed_site(store_conn)
    _ready(monkeypatch, [])

    def explode(*args, **kwargs):
        raise RuntimeError(f"lost the socket at {PROFILE_PATH} ({PROFILE_EMAIL})")

    monkeypatch.setattr(tools_submit.submit, "run", explode)
    with capturing(tools_submit.log) as records:
        result = tools_submit.request_indexing(["https://example.com/a"])

    assert result["ok"] is False
    assert result["error"] == "unexpected"
    assert result["error"] != "extension_not_connected"
    assert result["detail"] == "RuntimeError"
    blob = json.dumps(result)
    assert "secret-operator" not in blob
    assert PROFILE_EMAIL not in blob
    assert "socket" not in blob
    records.assert_says_nothing_identifying("secret-operator", "lost the socket")


def test_an_unexpected_failure_returns_the_type_name_and_nothing_else(
        monkeypatch, store_conn):
    """A tool never raises, and never repeats an unauthored message."""
    def explode(*args, **kwargs):
        raise ValueError("failed for operator@example.net at /home/secret-operator")

    monkeypatch.setattr(tools_submit.config, "load", explode)
    with capturing(tools_submit.log) as records:
        result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["ok"] is False
    assert result["detail"] == "ValueError"
    assert "secret-operator" not in json.dumps(result)
    records.assert_says_nothing_identifying("secret-operator")


# --- a successful run --------------------------------------------------------

def test_a_successful_run_reports_per_url_results(monkeypatch, store_conn):
    _seed_site(store_conn)
    clock = _Clock()
    session = _ready(monkeypatch, ["submitted", "already_indexed"], clock)

    result = tools_submit.request_indexing(["https://example.com/a",
                                            "https://example.com/b"])

    assert result["ok"] is True
    assert result["submitted"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert result["stopped_early"] is False
    assert [entry["outcome"] for entry in result["results"]] == [
        "submitted", "already_indexed"]
    assert [entry["property"] for entry in result["results"]] == [PROPERTY] * 2
    assert [call[1] for call in session.sender.calls] == [
        "https://example.com/a", "https://example.com/b"]


def test_the_run_paces_with_this_module_s_clock_and_never_the_real_one(
        monkeypatch, store_conn):
    """The gap is minutes long: a test that waited it out is a broken suite.

    Pinned as a consequence — the sleep the loop was handed must be the one
    this module exposes, so submit.run cannot fall back to the real
    time.sleep it bound as a default argument at import.
    """
    _seed_site(store_conn)
    clock = _Clock()
    captured: dict = {}

    def spy(conn, sender, urls, **kwargs):
        captured.update(kwargs)
        return submit.RunResult([], False, None)

    _ready(monkeypatch, [], clock)
    monkeypatch.setattr(tools_submit.submit, "run", spy)
    tools_submit.request_indexing(["https://example.com/a"])

    # `==` and not `is`: a bound method is rebuilt on every attribute
    # access, so identity is false even for the same function on the same
    # object. Equality compares __self__ and __func__, which is the claim.
    assert captured["sleep"] == clock.sleep
    assert captured["account"] == tools_submit._account()
    assert captured["job_id"] is None
    assert captured["properties"] == [PROPERTY]


def test_two_sends_wait_one_proven_gap_between_them(monkeypatch, store_conn):
    _seed_site(store_conn)
    clock = _Clock()
    _ready(monkeypatch, ["submitted", "submitted"], clock)
    tools_submit.request_indexing(["https://example.com/a",
                                   "https://example.com/b"])
    assert len(clock.slept) == 1          # between, not after
    assert 130 <= clock.slept[0] <= 180


def test_an_unroutable_url_and_an_exhausted_property_read_differently(
        monkeypatch, store_conn):
    """Two different problems with two different fixes.

    Both arrive as "skipped" in the tally and neither has a disposition, so
    the only thing telling a user to refresh their property list rather
    than wait until tomorrow is that these stay distinguishable.
    """
    _seed_site(store_conn)
    _seed_site(store_conn, "sc-domain:example.net")
    _fill_slots(store_conn, 11, property="sc-domain:example.net")
    _ready(monkeypatch, [])

    result = tools_submit.request_indexing(["https://example.org/x",
                                            "https://example.net/b"])

    assert [entry["outcome"] for entry in result["results"]] == ["no_property",
                                                                 "no_quota"]
    assert result["skipped"] == 2
    assert result["submitted"] == 0
    notes = " ".join(result["notes"])
    assert "gsc_list_sites" in notes          # the no_property fix
    assert "quota" in notes                   # the no_quota fix
    # Neither ever opened a row, so neither may leave one behind for
    # reconcile() to close and charge a slot for.
    assert _count(store_conn, "submissions") == 0


def test_a_run_with_nothing_to_report_carries_no_notes(monkeypatch, store_conn):
    _seed_site(store_conn)
    _ready(monkeypatch, ["submitted"])
    result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["notes"] == []


# --- privacy -----------------------------------------------------------------

def test_the_response_names_no_profile_no_path_and_no_address(monkeypatch,
                                                              store_conn):
    """The result travels to a client this project does not control."""
    _seed_site(store_conn)
    _ready(monkeypatch, ["submitted"])
    blob = json.dumps(tools_submit.request_indexing(["https://example.com/a"]))
    assert PROFILE_EMAIL not in blob
    assert "secret-operator" not in blob
    assert PROFILE_PATH not in blob
    assert "@" not in blob


def test_the_bridge_token_never_appears_in_the_response(monkeypatch, store_conn):
    _seed_site(store_conn)
    monkeypatch.setattr(tools_submit.bridge, "load_or_create_token",
                        lambda: "SECRET-TOKEN-VALUE")
    _ready(monkeypatch, ["submitted"])
    result = tools_submit.request_indexing(["https://example.com/a"])
    assert "SECRET-TOKEN-VALUE" not in json.dumps(result)


def test_no_email_address_reaches_the_submissions_table(monkeypatch, store_conn):
    _seed_site(store_conn)
    _ready(monkeypatch, ["submitted"])
    tools_submit.request_indexing(["https://example.com/a"])
    accounts = [row["account"] for row in
                store_conn.execute("SELECT account FROM submissions")]
    assert accounts == ["default"]
    assert all("@" not in account for account in accounts)


def test_the_ledger_key_is_a_literal_and_not_an_identity():
    assert tools_submit._account() == "default"
    assert "@" not in tools_submit._account()


def test_the_failure_path_logs_a_type_name_and_no_address(monkeypatch,
                                                          store_conn):
    _seed_site(store_conn)
    monkeypatch.setattr(tools_submit.target, "resolve", lambda *a, **k: _target())

    @contextmanager
    def refuses(chosen, cfg):
        raise OSError(f"cannot reach {PROFILE_PATH} for {PROFILE_EMAIL}")
        yield  # pragma: no cover

    monkeypatch.setattr(tools_submit.bridge, "bridge_session", refuses)
    with capturing(tools_submit.log) as records:
        result = tools_submit.request_indexing(["https://example.com/a"])
    assert result["detail"] == "OSError"
    records.assert_says_nothing_identifying("secret-operator", PROFILE_PATH)


def test_the_capture_helper_would_see_a_leak_if_there_were_one(monkeypatch):
    """The canary: without it the negative assertions above pass over an
    empty buffer forever."""
    with capturing(tools_submit.log) as records:
        tools_submit.log.warning("leaking %s", PROFILE_EMAIL)
    assert PROFILE_EMAIL in records.text


# --- shared helpers ----------------------------------------------------------

def test_properties_reads_every_known_property(store_conn):
    _seed_site(store_conn)
    _seed_site(store_conn, "sc-domain:example.net")
    assert tools_submit._properties(store_conn) == ["sc-domain:example.com",
                                                    "sc-domain:example.net"]


def test_the_shipped_cap_default_matches_the_hard_ceiling():
    assert config.DEFAULTS["sync_submit_cap"] == tools_submit.HARD_SYNC_CAP
