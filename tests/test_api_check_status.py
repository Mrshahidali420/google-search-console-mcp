"""check_status: routing, quota reservation, burst re-verification, persistence.

Two things about the fixtures below are load-bearing and were corrected from
the task brief; both are explained where they occur:

- `test_urls_beyond_the_daily_budget_are_skipped_not_attempted` back-dates its
  pre-existing calls, otherwise the *minute* window is what blocks and the
  test proves the opposite of its name.
- every re-verification test inspects TWO urls, because the worker count is
  `min(concurrency, 15, len(urls))` and re-verification only runs when that
  came out above one. With a single url no burst is possible, so no amount of
  `concurrency=4` makes the pass concurrent.
"""
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from _fakes import FakeProvider
from gsc_core import api, quota, store

PROP = "sc-domain:example.com"
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.db")
    yield connection
    connection.close()


class ScriptedInspect:
    """Stands in for api.inspect_url; returns queued verdicts per URL."""

    def __init__(self, script: dict[str, list[tuple[str, str]]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def __call__(self, url, property, provider, **kwargs):
        self.calls.append(url)
        queue = self.script.get(url) or [("indexed", "Submitted and indexed")]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def run(conn, urls, inspect, **kwargs):
    kwargs.setdefault("concurrency", 1)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("sleep", lambda _: None)
    return api.check_status(conn, urls, FakeProvider(), [PROP],
                            _inspect=inspect, **kwargs)


def test_returns_a_row_per_url_in_input_order(conn):
    urls = ["https://example.com/a", "https://example.com/b"]
    out = run(conn, urls, ScriptedInspect({}))
    assert [r["url"] for r in out["rows"]] == urls


def test_unroutable_url_is_flagged_and_never_inspected(conn):
    inspect = ScriptedInspect({})
    out = run(conn, ["https://elsewhere.test/a"], inspect)
    assert out["rows"][0]["status"] == "no_property"
    assert inspect.calls == []


def test_results_are_written_to_the_store(conn):
    run(conn, ["https://example.com/a"], ScriptedInspect({}))
    rows = store.get_urls(conn, PROP)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://example.com/a"
    assert rows[0]["status"] == "indexed"


def test_inspections_are_recorded_against_the_property_budget(conn):
    run(conn, ["https://example.com/a", "https://example.com/b"], ScriptedInspect({}))
    assert quota.inspection_used(conn, PROP, now=NOW)["day"] == 2


def test_urls_beyond_the_daily_budget_are_skipped_not_attempted(conn):
    # Back-dated five minutes on purpose. Stamped at NOW these 1,999 calls sit
    # inside the 60-second window too, so MINUTE_INSPECTION_LIMIT (600) is what
    # would actually bind and the free count would be 0, not 1 -- the test
    # would still go green-ish for the wrong reason on the wrong limit. Aged
    # out of the minute window, only the daily ceiling is left to do the work.
    with store.tx(conn):
        quota.record_inspections(conn, PROP, quota.DAILY_INSPECTION_LIMIT - 1,
                                 when=NOW - timedelta(minutes=5))
    inspect = ScriptedInspect({})
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect)
    assert len(inspect.calls) == 1
    assert len(out["skipped_quota"]) == 1
    assert out["skipped_quota"][0]["binding"] == "daily"
    assert out["skipped_quota"][0]["retry_after_seconds"] > 0


def test_the_smaller_of_the_two_windows_is_what_limits_a_partial_batch(conn):
    """The verdict names only the FIRST binding window. Honouring that one
    alone fires the other straight into a rejection."""
    with store.tx(conn):
        # 1,950 calls today, 570 of them inside the last minute:
        # daily_free == 50, minute_free == 30.
        quota.record_inspections(conn, PROP, 1380, when=NOW - timedelta(minutes=5))
        quota.record_inspections(conn, PROP, 570, when=NOW - timedelta(seconds=10))
    urls = [f"https://example.com/p{n}" for n in range(60)]
    out = run(conn, urls, ScriptedInspect({}))
    assert out["quota"][PROP]["binding"] == "daily"     # ...but 30 is the cap
    assert out["checked"] == 30
    assert len(out["skipped_quota"]) == 30


def test_a_batch_larger_than_the_minute_ceiling_does_not_crash(conn):
    """A 1,400-url site is the motivating case; 600/min is the first ceiling
    it walks into, on a completely empty ledger."""
    urls = [f"https://example.com/p{n}" for n in range(700)]
    out = run(conn, urls, ScriptedInspect({}))
    assert out["checked"] == quota.MINUTE_INSPECTION_LIMIT
    assert len(out["skipped_quota"]) == 100


def test_quota_is_reserved_before_the_first_http_call(conn):
    """Reserve-then-spend: a crash mid-batch must over-count, never under-count."""
    seen = []

    def inspect(url, property, provider, **kwargs):
        seen.append(quota.inspection_used(conn, PROP, now=NOW)["day"])
        return ("indexed", "Submitted and indexed")

    run(conn, ["https://example.com/a", "https://example.com/b"], inspect)
    assert seen[0] == 2, "both slots must be recorded before any call is made"


def test_unknown_result_is_reverified_sequentially(conn):
    """The concurrent pass degrades real states to unknown; the re-verify pass
    is what recovers them."""
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "URL is unknown to Google"),
                                  ("indexed", "Submitted and indexed")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=4)
    assert out["rows"][0]["status"] == "indexed"
    assert inspect.calls.count("https://example.com/a") == 2


def test_reverification_is_skipped_when_only_one_worker_ran(conn):
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u"), ("indexed", "i")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=1)
    assert out["rows"][0]["status"] == "unknown_to_google"
    assert inspect.calls.count("https://example.com/a") == 1


def test_a_genuinely_unknown_url_stops_being_rechecked(conn):
    """When a round flips nothing, the loop must stop rather than run all 4."""
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u")],
    })
    run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
        concurrency=4)
    assert inspect.calls.count("https://example.com/a") == 2


def test_reverification_calls_also_consume_quota(conn):
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u"), ("indexed", "i")],
    })
    run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
        concurrency=4)
    # two in the concurrent pass, one more for the single re-verified suspect.
    assert quota.inspection_used(conn, PROP, now=NOW)["day"] == 3


# A round that turns one suspect into a *different* suspect has flipped
# nothing -- only leaving the suspect set counts. So keeping the loop alive
# for all four rounds needs one url that never settles plus others that
# settle on successive rounds.
_FOUR_ROUND_SCRIPT = {
    "https://example.com/a": [("unknown_to_google", "u")],
    "https://example.com/b": [("unknown_to_google", "u"), ("indexed", "i")],
    "https://example.com/c": [("unknown_to_google", "u"), ("unknown", "u"),
                              ("indexed", "i")],
    "https://example.com/d": [("unknown_to_google", "u"), ("unknown", "u"),
                              ("unknown", "u"), ("indexed", "i")],
}


def test_reverification_cooldown_doubles_each_round(conn):
    slept = []
    run(conn, list(_FOUR_ROUND_SCRIPT), ScriptedInspect(_FOUR_ROUND_SCRIPT),
        concurrency=4, sleep=slept.append)
    cooldowns = [s for s in slept if s != api.REVERIFY_GAP_S]
    assert cooldowns == [5.0, 10.0, 20.0, 40.0]


def test_reverification_gives_up_after_four_rounds(conn):
    inspect = ScriptedInspect(_FOUR_ROUND_SCRIPT)
    run(conn, list(_FOUR_ROUND_SCRIPT), inspect, concurrency=4)
    assert inspect.calls.count("https://example.com/a") == 1 + api.MAX_REVERIFY_ROUNDS


def test_suspects_beyond_the_round_cap_are_left_unverified_and_logged(conn, caplog):
    urls = [f"https://example.com/p{n}" for n in range(4)]
    inspect = ScriptedInspect({u: [("unknown_to_google", "u")] for u in urls})
    with caplog.at_level("WARNING", logger="gsc.gsc_core.api"):
        run(conn, urls, inspect, concurrency=4, max_suspects_per_round=2)
    assert len(inspect.calls) == 4 + 2
    assert any("2 of 4" in record.getMessage() for record in caplog.records)


def test_time_budget_stops_reverification(conn):
    ticks = iter([0.0] + [10_000.0] * 50)
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u"), ("indexed", "i")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=4, time_budget_s=1.0, monotonic=lambda: next(ticks))
    assert out["rows"][0]["status"] == "unknown_to_google"
    assert inspect.calls.count("https://example.com/a") == 1


def test_a_row_the_store_rejects_does_not_lose_the_rest_of_the_batch(conn, monkeypatch):
    """One bad row must not abort a 1,400-URL pass.

    The rejection is a REAL one -- a NOT NULL violation raised from inside
    upsert_url's own transaction, mid-write. An earlier version of this test
    raised before upsert_url was ever entered, which proved only that a
    try/except catches a ValueError and would have passed against a store
    that corrupted the batch.
    """
    real = store.upsert_url

    def flaky(conn_, url, property, *args, **kwargs):
        # property is NOT NULL, so this fails inside the INSERT itself.
        return real(conn_, url, None if url.endswith("/a") else property,
                    *args, **kwargs)

    monkeypatch.setattr(store, "upsert_url", flaky)
    urls = ["https://example.com/a", "https://example.com/b"]
    out = run(conn, urls, ScriptedInspect({}))
    assert len(out["rows"]) == 2
    assert [r["url"] for r in store.get_urls(conn, PROP)] == ["https://example.com/b"]
    # the batch committed and the connection is still usable afterwards
    assert not conn.in_transaction


def test_every_row_in_a_batch_shares_one_checked_at(conn):
    urls = ["https://example.com/a", "https://example.com/b"]
    run(conn, urls, ScriptedInspect({}))
    stamps = {row["checked_at"] for row in store.get_urls(conn, PROP)}
    assert stamps == {store.utc_iso(NOW)}


def test_a_raising_inspect_becomes_an_error_row_not_a_lost_batch(conn):
    def boom(url, property, provider, **kwargs):
        if url.endswith("/a"):
            raise RuntimeError("worker exploded")
        return ("indexed", "Submitted and indexed")

    out = run(conn, ["https://example.com/a", "https://example.com/b"], boom)
    assert out["rows"][0]["status"] == "error"
    assert out["rows"][1]["status"] == "indexed"


def test_workers_run_off_thread_and_are_never_handed_the_connection(conn):
    """The structural rule, asserted rather than trusted.

    A previous version of this test could not fail for its stated reason:
    _safe_inspect turns ANY exception into an "error" row, so a worker
    touching the connection would raise sqlite3.ProgrammingError, get
    swallowed, and look exactly like an ordinary inspection failure while
    every assertion still passed. So the worker catches the touch itself and
    the outcome is asserted directly.
    """
    threads: set[int] = set()
    outcomes: list[str] = []
    kwargs_seen: list[set[str]] = []

    def inspect(url, property, provider, **kwargs):
        threads.add(threading.get_ident())
        kwargs_seen.append(set(kwargs))
        try:
            conn.execute("SELECT 1").fetchone()
            outcomes.append("touched")
        except sqlite3.ProgrammingError:
            outcomes.append("refused")
        return ("indexed", "Submitted and indexed")

    urls = [f"https://example.com/p{n}" for n in range(6)]
    out = run(conn, urls, inspect, concurrency=4)

    # the pass really did run off the calling thread (asserting a *count* of
    # threads would be flaky -- the pool spawns them lazily) ...
    assert threading.get_ident() not in threads
    # ... sqlite3 refuses the connection out there, so the rule has teeth ...
    assert outcomes == ["refused"] * 6
    # ... nothing a worker receives is a database handle ...
    assert all(seen <= {"session", "sleep"} for seen in kwargs_seen)
    # ... and no row was quietly downgraded to an error on the way, which is
    # how a swallowed ProgrammingError would have surfaced.
    assert {row["status"] for row in out["rows"]} == {"indexed"}
    assert len(store.get_urls(conn, PROP)) == 6


# --- re-verification honesty (a suspect nobody re-checked is not confirmed) ---

def _burn_minute_budget(conn, leaving: int):
    """Fill the 60-second window so only `leaving` calls fit."""
    with store.tx(conn):
        quota.record_inspections(conn, PROP,
                                 quota.MINUTE_INSPECTION_LIMIT - leaving,
                                 when=NOW - timedelta(seconds=10))


def test_a_suspect_the_recheck_quota_never_reached_is_flagged_unverified(conn):
    """The critical case: re-verify quota runs out, _recheck flips nothing,
    and the loop would otherwise read that as "these unknowns are real".

    Without the flag the caller sees status unknown_to_google, an empty
    skipped_quota and binding None -- nothing anywhere saying the URL was
    never actually re-checked. "Google still says unknown" and "we never got
    to ask" must not arrive looking alike.
    """
    _burn_minute_budget(conn, leaving=2)
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "URL is unknown to Google"),
                                  ("indexed", "would have flipped")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=4)

    # the concurrent pass happened; the re-verification round did not.
    assert inspect.calls.count("https://example.com/a") == 1
    row = out["rows"][0]
    assert row["status"] == "unknown_to_google"      # status is preserved ...
    assert row["unverified"] is True                 # ... but not trusted
    assert "not re-verified: re-check quota exhausted" in row["detail"]
    assert out["quota"][PROP]["unverified"] == 1


def test_an_exhausted_recheck_budget_stops_instead_of_idling(conn):
    """A round the budget refused outright is not "flipped nothing" -- it is
    "could not look". Nothing was inspected, so nothing changed, so the next
    three rounds would cool down and hit the same wall: 20 seconds of real
    production wall clock spent to learn nothing, instead of 5.
    """
    _burn_minute_budget(conn, leaving=2)
    slept: list[float] = []
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u")],
    })
    run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
        concurrency=4, sleep=slept.append)
    assert slept == [api.REVERIFY_COOLDOWN_S]


def test_a_suspect_already_rechecked_is_not_re_flagged_by_a_later_round(conn):
    """Round N confirms it; round N+1 is refused quota. It stays confirmed.

    A later round's refusal cannot un-ask a question an earlier round already
    answered. Re-flagging it is the mirror image of the bug this whole
    mechanism exists to prevent -- a false alarm about our own work rather
    than false confidence, but still an untrue account of what ran.
    """
    _burn_minute_budget(conn, leaving=4)   # 2 for the pass, 2 for round 1
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u")],
        # b flipping in round 1 is what keeps the loop alive for round 2.
        "https://example.com/b": [("unknown_to_google", "u"), ("indexed", "i")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=4)

    # a was re-checked once (round 1) and round 2 was refused quota ...
    assert inspect.calls.count("https://example.com/a") == 2
    assert out["rows"][0]["status"] == "unknown_to_google"
    # ... so it is a confirmed unknown, not an unreached one.
    assert out["rows"][0]["unverified"] is False
    assert "not re-verified" not in out["rows"][0]["detail"]
    assert out["quota"][PROP]["unverified"] == 0


def test_a_sequentially_confirmed_unknown_is_not_flagged_unverified(conn):
    """The other half of the distinction: this one really was re-checked."""
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "URL is unknown to Google")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=4)
    assert inspect.calls.count("https://example.com/a") == 2
    assert out["rows"][0]["status"] == "unknown_to_google"
    assert out["rows"][0]["unverified"] is False
    assert "not re-verified" not in out["rows"][0]["detail"]
    assert out["quota"][PROP]["unverified"] == 0


def test_suspects_dropped_by_the_round_cap_are_flagged_unverified(conn):
    urls = [f"https://example.com/p{n}" for n in range(4)]
    inspect = ScriptedInspect({u: [("unknown_to_google", "u")] for u in urls})
    out = run(conn, urls, inspect, concurrency=4, max_suspects_per_round=2)
    flagged = [row["url"] for row in out["rows"] if row["unverified"]]
    assert flagged == urls[2:]
    assert "round cap reached" in out["rows"][2]["detail"]
    assert out["quota"][PROP]["unverified"] == 2


def test_a_suspect_the_cap_dropped_then_a_later_round_reached_is_not_flagged(conn):
    """The flag must clear when a later round gets to it.

    A suspect the cap skipped in round 1 and re-checked in round 2 HAS been
    confirmed sequentially. Leaving round 1's reason on it reports "we never
    got to ask" about a URL we did ask about -- a false alarm in the opposite
    direction, and just as much a lie about what the tool actually did.
    """
    urls = [f"https://example.com/p{n}" for n in range(4)]
    inspect = ScriptedInspect({
        # p0 flips in round 1, which is what keeps the loop alive for round 2.
        urls[0]: [("unknown_to_google", "u"), ("indexed", "i")],
        urls[1]: [("unknown_to_google", "u")],
        urls[2]: [("unknown_to_google", "u")],
        urls[3]: [("unknown_to_google", "u")],
    })
    out = run(conn, urls, inspect, concurrency=4, max_suspects_per_round=2)
    flags = {row["url"]: row["unverified"] for row in out["rows"]}

    # round 1 re-checked p0 and p1; round 2 re-checked p1 and p2 ...
    assert inspect.calls.count(urls[2]) == 2
    assert flags[urls[1]] is False
    assert flags[urls[2]] is False
    # ... and only p3 was never reached at all.
    assert flags[urls[3]] is True
    assert inspect.calls.count(urls[3]) == 1
    assert out["quota"][PROP]["unverified"] == 1


def test_suspects_left_by_the_time_budget_are_flagged_unverified(conn):
    ticks = iter([0.0] + [10_000.0] * 50)
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u"), ("indexed", "i")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=4, time_budget_s=1.0, monotonic=lambda: next(ticks))
    assert out["rows"][0]["unverified"] is True
    assert "time budget spent" in out["rows"][0]["detail"]


def test_a_single_worker_pass_leaves_nothing_flagged_unverified(conn):
    """A sequential pass cannot have been degraded by a burst, so its
    unknowns need no confirmation and must not be reported as doubtful."""
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u")],
    })
    out = run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
              concurrency=1)
    assert out["rows"][0]["status"] == "unknown_to_google"
    assert out["rows"][0]["unverified"] is False
    assert out["quota"][PROP]["unverified"] == 0


# --- timestamps, duplicates ---------------------------------------------------

def test_reverification_records_are_stamped_at_their_call_time(conn):
    """Stamping a T+120 re-check as if it happened at T makes it leave the
    60-second window early, so the NEXT batch believes it has budget it does
    not -- the over-permit direction the whole design is biased against."""
    ticks = iter([0.0] + [120.0] * 10)
    inspect = ScriptedInspect({
        "https://example.com/a": [("unknown_to_google", "u"), ("indexed", "i")],
    })
    run(conn, ["https://example.com/a", "https://example.com/b"], inspect,
        concurrency=4, monotonic=lambda: next(ticks))

    stamps = [row["called_at"] for row in conn.execute(
        "SELECT called_at FROM inspection_calls ORDER BY id")]
    assert stamps[:2] == [store.utc_iso(NOW)] * 2
    assert stamps[2] == store.utc_iso(NOW + timedelta(seconds=120))


def test_a_repeated_url_is_inspected_once_and_reserves_one_slot(conn):
    """Letting both copies through sends two requests against one reserved
    slot -- under-counting, the direction reserve-then-spend exists to
    prevent -- and reports both rows as quota-skipped."""
    inspect = ScriptedInspect({})
    url = "https://example.com/a"
    out = run(conn, [url, url], inspect)

    assert inspect.calls == [url]
    assert quota.inspection_used(conn, PROP, now=NOW)["day"] == 1
    assert out["checked"] == 1
    assert out["skipped_quota"] == []
    # every occurrence still gets a row, and it carries the real result
    assert [row["status"] for row in out["rows"]] == ["indexed", "indexed"]
