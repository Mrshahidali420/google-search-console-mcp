"""The run loop: pacing, routing, and when a run is allowed to stop.

Every test here pins a CONSEQUENCE rather than a return shape. The loop
spends a real, unrecoverable resource — roughly eleven submissions per
property per rolling day — so the questions that matter are what reached the
sender, what reached the ledger, and what was left untouched. An assertion
that a dataclass field holds the value the constructor put there would pass
against an implementation that submitted every URL twice.

Nothing here sleeps. The real gap between two submissions is 130-180
seconds; a suite that waited it out would take an hour. The sleep is
injected and the tests assert the delay was REQUESTED, with the real default
pinned separately so shortening it to make testing easier reddens a test.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from _logcheck import capturing
from gsc_core import config, store, submit

PROPERTY = "sc-domain:example.com"
OTHER_PROPERTY = "sc-domain:example.net"
ACCOUNT = "account-a"

CFG = {"property_slots": 11, "account_slots": None, "daily_reserve": 0,
       "submit_delay_range": [130, 180], "stop_on_throttle": True,
       "authuser": "0"}


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "gsc.sqlite3")
    yield connection
    connection.close()


class FakeSender:
    """Records what it was asked to submit and replies from a script.

    Popping from an empty script is a failure, not a default: a test that
    expects two sends and gets three should say so loudly rather than
    silently reusing the last outcome.
    """

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, str, str]] = []

    def submit(self, property: str, url: str, authuser: str) -> str:
        self.calls.append((property, url, authuser))
        assert self.outcomes, f"unscripted submit of {url}"
        return self.outcomes.pop(0)


def _run(conn: sqlite3.Connection, sender, urls: list[str], **kwargs):
    params = {"properties": [PROPERTY], "account": ACCOUNT, "job_id": None,
              "cfg": CFG, "sleep": lambda seconds: None}
    params.update(kwargs)
    return submit.run(conn, sender, urls, **params)


def _fill_slots(conn: sqlite3.Connection, count: int, *,
                property: str = PROPERTY) -> None:
    now = datetime.now(UTC)
    for index in range(count):
        conn.execute(
            "INSERT INTO quota_slots (account, property, used_at) VALUES (?,?,?)",
            (ACCOUNT, property, store.utc_iso(now - timedelta(minutes=index))))
    conn.commit()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


# --- routing -----------------------------------------------------------------

def test_each_url_is_routed_to_its_own_property(conn):
    sender = FakeSender(["submitted", "submitted"])
    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.net/b"],
                  properties=[PROPERTY, OTHER_PROPERTY])
    assert [call[0] for call in sender.calls] == [PROPERTY, OTHER_PROPERTY]
    assert [attempt.outcome for attempt in result.attempts] == ["submitted",
                                                                "submitted"]
    assert result.stopped_early is False


def test_an_unroutable_url_is_reported_and_never_sent(conn):
    sender = FakeSender([])
    result = _run(conn, sender, ["https://example.org/x"])
    assert sender.calls == []
    assert result.attempts[0].outcome == "no_property"
    assert result.attempts[0].spent_slot is False
    assert result.attempts[0].property is None
    # No row was opened: an open row is counted as spent quota by quota.used()
    # until reconcile() sweeps it.
    assert _count(conn, "submissions") == 0
    assert _count(conn, "quota_slots") == 0


def test_an_unroutable_url_does_not_stop_the_urls_after_it(conn):
    sender = FakeSender(["submitted"])
    result = _run(conn, sender,
                  ["https://example.org/x", "https://example.com/a"])
    assert [call[1] for call in sender.calls] == ["https://example.com/a"]
    assert [attempt.outcome for attempt in result.attempts] == ["no_property",
                                                                "submitted"]
    assert result.stopped_early is False


def test_the_configured_authuser_reaches_the_sender(conn):
    sender = FakeSender(["submitted"])
    _run(conn, sender, ["https://example.com/a"], cfg={**CFG, "authuser": "3"})
    assert sender.calls[0][2] == "3"


# --- stopping ----------------------------------------------------------------

def test_a_rate_limit_stops_the_run_and_leaves_later_urls_untouched(conn):
    sender = FakeSender(["submitted", "rate_limited", "submitted"])
    result = _run(conn, sender, ["https://example.com/a",
                                 "https://example.com/b",
                                 "https://example.com/c"])
    assert len(sender.calls) == 2
    assert result.stopped_early is True
    assert result.stop_reason == "rate_limited"
    assert [attempt.url for attempt in result.attempts] == [
        "https://example.com/a", "https://example.com/b"]
    # The third URL was never reserved: two rows, not three.
    assert _count(conn, "submissions") == 2


def test_a_rate_limit_spends_no_slot(conn):
    sender = FakeSender(["rate_limited"])
    _run(conn, sender, ["https://example.com/a"])
    assert _count(conn, "quota_slots") == 0


def test_stop_on_throttle_false_lets_a_throttled_run_continue(conn):
    sender = FakeSender(["rate_limited", "submitted"])
    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.com/b"],
                  cfg={**CFG, "stop_on_throttle": False})
    assert len(sender.calls) == 2
    assert result.stopped_early is False


def test_a_quota_exceeded_stops_the_run_even_with_stop_on_throttle_off(conn):
    # The property has no capacity left, so continuing would submit into a
    # guaranteed refusal whatever the user configured.
    sender = FakeSender(["quota_exceeded", "submitted"])
    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.com/b"],
                  cfg={**CFG, "stop_on_throttle": False})
    assert len(sender.calls) == 1
    assert result.stop_reason == "quota_exceeded"


def test_should_stop_ends_the_run_between_urls(conn):
    sender = FakeSender(["submitted", "submitted"])
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.com/b"],
                  should_stop=should_stop)
    assert len(sender.calls) == 1
    assert result.stopped_early is True
    assert result.stop_reason == "stopped_by_user"
    assert _count(conn, "submissions") == 1


def test_should_stop_checked_before_the_first_url_sends_nothing(conn):
    sender = FakeSender([])
    result = _run(conn, sender, ["https://example.com/a"],
                  should_stop=lambda: True)
    assert sender.calls == []
    assert result.attempts == []
    assert _count(conn, "submissions") == 0


def test_a_stop_raised_during_the_delay_is_honoured_before_the_next_send(conn):
    """The gap is minutes long. A stop that arrives inside it must land."""
    sender = FakeSender(["submitted", "submitted"])
    stopped = {"now": False}

    def sleep(seconds: float) -> None:
        stopped["now"] = True

    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.com/b"],
                  sleep=sleep, should_stop=lambda: stopped["now"])
    assert len(sender.calls) == 1
    assert result.stop_reason == "stopped_by_user"
    # The second URL never reserved a slot while we waited.
    assert _count(conn, "submissions") == 1


# --- quota -------------------------------------------------------------------

def test_an_exhausted_property_stops_the_run_without_sending(conn):
    _fill_slots(conn, 11)
    sender = FakeSender([])
    result = _run(conn, sender, ["https://example.com/a"])
    assert sender.calls == []
    assert result.attempts[0].outcome == "no_quota"
    assert result.attempts[0].spent_slot is False
    assert result.stopped_early is True
    assert result.stop_reason == "no_quota"


def test_a_url_that_never_got_a_slot_leaves_the_ledger_untouched(conn):
    _fill_slots(conn, 11)
    _run(conn, FakeSender([]), ["https://example.com/a"])
    assert _count(conn, "quota_slots") == 11   # not twelve
    assert _count(conn, "submissions") == 0    # and no open row to reconcile


def test_a_full_property_does_not_stop_urls_on_a_different_property(conn):
    # Quota is per property. One exhausted property must not end a run that
    # still has work on another.
    _fill_slots(conn, 11)
    sender = FakeSender(["submitted"])
    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.net/b"],
                  properties=[PROPERTY, OTHER_PROPERTY])
    assert [attempt.outcome for attempt in result.attempts] == ["no_quota",
                                                                "submitted"]
    assert [call[0] for call in sender.calls] == [OTHER_PROPERTY]
    assert result.stopped_early is False


def test_a_full_property_does_not_stop_a_run_with_unroutable_work_left(conn):
    """An unroutable URL still has its own report line to produce."""
    _fill_slots(conn, 11)
    result = _run(conn, FakeSender([]),
                  ["https://example.com/a", "https://example.org/x"])
    assert [attempt.outcome for attempt in result.attempts] == ["no_quota",
                                                                "no_property"]
    assert result.stopped_early is False


def test_the_run_stops_once_the_last_property_with_work_runs_dry(conn):
    _fill_slots(conn, 11)
    _fill_slots(conn, 11, property=OTHER_PROPERTY)
    result = _run(conn, FakeSender([]),
                  ["https://example.com/a", "https://example.net/b"],
                  properties=[PROPERTY, OTHER_PROPERTY])
    # The first no_quota continues (work remains elsewhere); the second has
    # nothing left to hope for and ends the run.
    assert [attempt.outcome for attempt in result.attempts] == ["no_quota",
                                                                "no_quota"]
    assert result.stop_reason == "no_quota"


def test_daily_reserve_holds_slots_back_from_the_loop(conn):
    _fill_slots(conn, 9)
    result = _run(conn, FakeSender([]), ["https://example.com/a"],
                  cfg={**CFG, "daily_reserve": 2})
    assert result.attempts[0].outcome == "no_quota"
    assert _count(conn, "submissions") == 0


def test_a_run_stops_when_the_property_runs_dry_mid_run(conn):
    _fill_slots(conn, 10)
    sender = FakeSender(["submitted"])
    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.com/b"])
    assert [attempt.outcome for attempt in result.attempts] == ["submitted",
                                                                "no_quota"]
    assert len(sender.calls) == 1
    assert result.stop_reason == "no_quota"


# --- pacing ------------------------------------------------------------------

def test_the_delay_falls_inside_the_configured_range_and_not_after_the_last(conn):
    slept: list[float] = []
    sender = FakeSender(["submitted", "submitted"])
    _run(conn, sender, ["https://example.com/a", "https://example.com/b"],
         sleep=slept.append)
    assert len(slept) == 1                    # between, not after
    assert 130 <= slept[0] <= 180


def test_nothing_is_waited_for_when_nothing_was_sent(conn):
    """A URL that never reached Google needs no pacing before or after it.

    Pacing exists to space real submissions. Sleeping around a routing miss
    or an exhausted property would burn minutes of a job's wall clock for
    an attempt Google never saw.
    """
    slept: list[float] = []
    _run(conn, FakeSender([]),
         ["https://example.org/x", "https://example.org/y"], sleep=slept.append)
    assert slept == []


def test_no_delay_is_requested_after_the_last_actual_send(conn):
    """A trailing URL that is never sent must not cost the job three minutes."""
    slept: list[float] = []
    sender = FakeSender(["submitted"])
    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.org/x"],
                  sleep=slept.append)
    assert [attempt.outcome for attempt in result.attempts] == ["submitted",
                                                                "no_property"]
    assert slept == []


def test_each_gap_is_drawn_afresh(conn):
    """A single draw reused for a whole run is a fixed, fingerprintable beat."""
    slept: list[float] = []
    sender = FakeSender(["submitted"] * 12)
    _run(conn, sender, [f"https://example.com/{index}" for index in range(12)],
         sleep=slept.append, cfg={**CFG, "property_slots": 20})
    assert len(slept) == 11
    assert len(set(slept)) > 1
    assert all(130 <= seconds <= 180 for seconds in slept)


def test_the_shipped_default_gap_is_the_proven_one():
    """130-180 seconds, proven against live runs. Shortening it throttles.

    Pinned against the shipped defaults, not against a test constant, so a
    well-meaning edit to config.py to speed something up fails here.
    """
    assert config.DEFAULTS["submit_delay_range"] == [130, 180]


def test_a_cfg_without_a_range_still_paces_at_the_proven_default(conn):
    slept: list[float] = []
    bare = {key: value for key, value in CFG.items()
            if key != "submit_delay_range"}
    _run(conn, FakeSender(["submitted", "submitted"]),
         ["https://example.com/a", "https://example.com/b"],
         cfg=bare, sleep=slept.append)
    assert 130 <= slept[0] <= 180


# --- failure -----------------------------------------------------------------

def test_a_sender_that_raises_settles_the_row_rather_than_leaking_it(conn):
    class Exploding:
        def submit(self, property: str, url: str, authuser: str) -> str:
            raise RuntimeError("socket died")

    result = _run(conn, Exploding(), ["https://example.com/a"])
    assert result.attempts[0].outcome == "error"
    # The row MUST be closed. A leaked open row is counted as spent quota by
    # quota.used() until reconcile() sweeps it, silently shrinking the budget.
    assert store.open_submissions(conn) == []
    assert _count(conn, "quota_slots") == 0


def test_a_sender_that_raises_does_not_end_the_run(conn):
    class FlakyOnce:
        def __init__(self) -> None:
            self.calls = 0

        def submit(self, property: str, url: str, authuser: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("socket died")
            return "submitted"

    sender = FlakyOnce()
    result = _run(conn, sender,
                  ["https://example.com/a", "https://example.com/b"])
    assert [attempt.outcome for attempt in result.attempts] == ["error",
                                                                "submitted"]
    assert result.stopped_early is False


def test_an_unknown_outcome_leaves_the_row_open_for_reconcile(conn):
    """Loud on purpose: a drifted vocabulary must not mis-charge quota.

    settle() resolves the disposition BEFORE touching the store, so the row
    survives for reconcile() to judge rather than being guessed at now.
    """
    with pytest.raises(KeyError):
        _run(conn, FakeSender(["something_new"]), ["https://example.com/a"])
    assert len(store.open_submissions(conn)) == 1


def test_the_failure_path_names_no_account_and_no_url_detail(conn):
    class Exploding:
        def submit(self, property: str, url: str, authuser: str) -> str:
            raise RuntimeError("socket died for me@example.com")

    with capturing(submit.log) as records:
        _run(conn, Exploding(), ["https://example.com/a"],
             account="operator@example.com")
    records.assert_says_nothing_identifying("operator@example.com",
                                            "socket died")
    assert "RuntimeError" in records.text


# --- progress ----------------------------------------------------------------

def test_on_progress_sees_every_attempt_in_order(conn):
    seen: list[str] = []
    sender = FakeSender(["submitted"])
    result = _run(conn, sender,
                  ["https://example.org/x", "https://example.com/a"],
                  on_progress=lambda attempt: seen.append(attempt.outcome))
    assert seen == ["no_property", "submitted"]
    assert seen == [attempt.outcome for attempt in result.attempts]


def test_a_progress_callback_that_raises_does_not_lose_the_run(conn):
    """The callback belongs to the caller. Its bug is not the ledger's."""
    def boom(attempt: submit.Attempt) -> None:
        raise RuntimeError("callback exploded")

    result = _run(conn, FakeSender(["submitted", "submitted"]),
                  ["https://example.com/a", "https://example.com/b"],
                  on_progress=boom)
    assert [attempt.outcome for attempt in result.attempts] == ["submitted",
                                                                "submitted"]


def test_an_empty_url_list_is_a_no_op(conn):
    result = _run(conn, FakeSender([]), [])
    assert result.attempts == []
    assert result.stopped_early is False
    assert result.stop_reason is None
