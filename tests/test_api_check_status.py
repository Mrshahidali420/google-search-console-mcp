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
    """One bad row must not abort a 1,400-URL pass."""
    real = store.upsert_url

    def flaky(conn_, url, *args, **kwargs):
        if url.endswith("/a"):
            raise ValueError("rejected")
        return real(conn_, url, *args, **kwargs)

    monkeypatch.setattr(store, "upsert_url", flaky)
    urls = ["https://example.com/a", "https://example.com/b"]
    out = run(conn, urls, ScriptedInspect({}))
    assert len(out["rows"]) == 2
    assert [r["url"] for r in store.get_urls(conn, PROP)] == ["https://example.com/b"]


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


def test_worker_threads_never_touch_the_connection(conn):
    """sqlite3 refuses cross-thread use, so a db call from a worker raises.

    This asserts the structural rule rather than trusting it: if a future
    change moved persistence or quota inside the pool, the concurrent pass
    would start failing here.
    """
    threads = set()

    def inspect(url, property, provider, **kwargs):
        threads.add(threading.get_ident())
        return ("indexed", "Submitted and indexed")

    urls = [f"https://example.com/p{n}" for n in range(6)]
    run(conn, urls, inspect, concurrency=4)
    # the pass really did run off the calling thread (asserting a *count* of
    # threads would be flaky -- the pool spawns them lazily) ...
    assert threading.get_ident() not in threads
    # ... and the connection is still usable from the calling thread, which it
    # would not be had a worker touched it.
    assert len(store.get_urls(conn, PROP)) == 6
