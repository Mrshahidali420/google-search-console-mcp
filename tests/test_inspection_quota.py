import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from gsc_core import quota, store

PROP = "sc-domain:example.com"
OTHER = "sc-domain:example.net"
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "t.db")
    yield connection
    connection.close()


def _spend(conn, prop, when, count=1):
    with store.tx(conn):
        quota.record_inspections(conn, prop, count, when=when)


def test_fresh_property_has_the_full_budget(conn):
    verdict = quota.inspection_check(conn, PROP, now=NOW)
    assert verdict.allowed
    assert verdict.daily_free == quota.DAILY_INSPECTION_LIMIT
    assert verdict.minute_free == quota.MINUTE_INSPECTION_LIMIT
    assert verdict.binding is None


def test_recorded_calls_reduce_both_counters(conn):
    _spend(conn, PROP, NOW - timedelta(seconds=10), count=5)
    used = quota.inspection_used(conn, PROP, now=NOW)
    assert used == {"day": 5, "minute": 5}


def test_calls_older_than_a_minute_leave_the_minute_window(conn):
    _spend(conn, PROP, NOW - timedelta(seconds=90), count=5)
    used = quota.inspection_used(conn, PROP, now=NOW)
    assert used == {"day": 5, "minute": 0}


def test_calls_older_than_24h_leave_the_daily_window(conn):
    _spend(conn, PROP, NOW - timedelta(hours=25), count=5)
    assert quota.inspection_used(conn, PROP, now=NOW) == {"day": 0, "minute": 0}


def test_properties_are_independent(conn):
    _spend(conn, PROP, NOW, count=7)
    assert quota.inspection_used(conn, OTHER, now=NOW) == {"day": 0, "minute": 0}


def test_daily_limit_blocks_and_reports_daily_as_binding(conn):
    _spend(conn, PROP, NOW - timedelta(hours=5), count=quota.DAILY_INSPECTION_LIMIT)
    verdict = quota.inspection_check(conn, PROP, wanted=1, now=NOW)
    assert not verdict.allowed
    assert verdict.binding == "daily"
    assert verdict.daily_free == 0


def test_minute_limit_blocks_and_reports_minute_as_binding(conn):
    _spend(conn, PROP, NOW - timedelta(seconds=5), count=quota.MINUTE_INSPECTION_LIMIT)
    verdict = quota.inspection_check(conn, PROP, wanted=1, now=NOW)
    assert not verdict.allowed
    assert verdict.binding == "minute"
    assert verdict.minute_free == 0
    assert verdict.daily_free > 0


def test_retry_after_is_under_a_minute_when_only_the_minute_window_is_full(conn):
    _spend(conn, PROP, NOW - timedelta(seconds=20), count=quota.MINUTE_INSPECTION_LIMIT)
    verdict = quota.inspection_check(conn, PROP, wanted=1, now=NOW)
    assert verdict.retry_after_seconds is not None
    assert 0 < verdict.retry_after_seconds <= 60


def test_wanted_larger_than_the_remaining_budget_is_refused(conn):
    # Spent outside the minute window (5 minutes ago, not "at NOW") so this
    # exercises only the daily dimension. Spending DAILY_INSPECTION_LIMIT - 3
    # calls in one instant would also blow the 600/minute ceiling, making
    # minute_free the binding constraint and wanted=3 fail for the wrong
    # reason -- the brief's original version had this collision.
    _spend(conn, PROP, NOW - timedelta(minutes=5),
           count=quota.DAILY_INSPECTION_LIMIT - 3)
    assert quota.inspection_check(conn, PROP, wanted=3, now=NOW).allowed
    assert not quota.inspection_check(conn, PROP, wanted=4, now=NOW).allowed


def test_prune_removes_old_rows_and_keeps_recent_ones(conn):
    _spend(conn, PROP, NOW - timedelta(days=5), count=4)
    _spend(conn, PROP, NOW - timedelta(hours=1), count=2)
    with store.tx(conn):
        removed = quota.prune_inspections(conn, keep_days=2, now=NOW)
    assert removed == 4
    assert quota.inspection_used(conn, PROP, now=NOW)["day"] == 2


def test_schema_is_version_two(conn):
    assert store.schema_version(conn) == 2


def test_an_existing_v1_database_gains_the_table_and_advances(tmp_path):
    """A database written before this change must gain inspection_calls and
    report the new version, with no hand-written migration."""
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta (key, value) VALUES ('schema_version', '1');"
    )
    legacy.commit()
    legacy.close()

    conn = store.connect(path)
    try:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='inspection_calls'").fetchall()
        assert store.schema_version(conn) == 2
    finally:
        conn.close()


def test_a_database_from_a_newer_version_is_left_alone(tmp_path):
    """Never silently downgrade a stamp written by a newer gsc-mcp."""
    path = tmp_path / "future.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta (key, value) VALUES ('schema_version', '99');"
    )
    legacy.commit()
    legacy.close()

    conn = store.connect(path)
    try:
        assert store.schema_version(conn) == 99
    finally:
        conn.close()


def _legacy_db_with_stamp(path, value):
    legacy = sqlite3.connect(path)
    legacy.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
    )
    legacy.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (value,)
    )
    legacy.commit()
    legacy.close()


def test_connect_does_not_crash_on_an_empty_version_stamp(tmp_path):
    """A corrupt schema_version must not stop the database from opening --
    the previous behaviour never parsed the value at all, so this must not
    regress just because advancing a stale stamp now needs to parse it."""
    path = tmp_path / "corrupt_empty.db"
    _legacy_db_with_stamp(path, "")

    conn = store.connect(path)
    try:
        stamp = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert stamp == ""
    finally:
        conn.close()


def test_connect_does_not_crash_on_a_non_numeric_version_stamp(tmp_path):
    """Same as above for a non-numeric value, not just an empty one."""
    path = tmp_path / "corrupt_text.db"
    _legacy_db_with_stamp(path, "abc")

    conn = store.connect(path)
    try:
        stamp = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert stamp == "abc"
    finally:
        conn.close()


def test_inspection_check_accepts_a_naive_now_when_blocked(conn):
    """now: datetime | None states no tz requirement, and every other path
    in this module tolerates naive input by normalising through
    store.utc_iso -- the blocked path (which computes retry_after_seconds)
    must too, not just the allowed path."""
    naive_now = NOW.replace(tzinfo=None)
    _spend(conn, PROP, NOW - timedelta(seconds=5),
           count=quota.MINUTE_INSPECTION_LIMIT)
    verdict = quota.inspection_check(conn, PROP, wanted=1, now=naive_now)
    assert not verdict.allowed
    assert verdict.binding == "minute"
    assert verdict.retry_after_seconds is not None
    assert 0 < verdict.retry_after_seconds <= 60
