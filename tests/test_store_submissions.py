import sqlite3
from datetime import datetime, timedelta, UTC

import pytest

from gsc_core import store

PROP = "sc-domain:example.com"
ACCOUNT = "user@example.com"
URL = "https://example.com/page"


def _conn(tmp_path):
    return store.connect(tmp_path / "state.db")


def _backdate(conn, submission_id, minutes):
    """Push a row's requested_at into the past, e.g. past reconcile's grace
    period, without going through the public API (there is none for this)."""
    old = store.utc_iso(datetime.now(UTC) - timedelta(minutes=minutes))
    conn.execute(
        "UPDATE submissions SET requested_at=? WHERE id=?", (old, submission_id)
    )
    return old


def test_open_submission_returns_id_and_is_uncommitted(tmp_path):
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    assert isinstance(sid, int)
    assert [row["id"] for row in store.open_submissions(conn)] == [sid]


def test_open_submission_spends_no_slot(tmp_path):
    conn = _conn(tmp_path)
    store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    slots = conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()
    assert slots["n"] == 0


def test_close_submission_commits_and_spends_one_slot(tmp_path):
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    store.close_submission(conn, sid, "submitted")

    assert store.open_submissions(conn) == []
    row = conn.execute(
        "SELECT verdict, committed FROM submissions WHERE id=?", (sid,)
    ).fetchone()
    assert row["verdict"] == "submitted"
    assert row["committed"] == 1

    slots = conn.execute(
        "SELECT account, property FROM quota_slots"
    ).fetchall()
    assert len(slots) == 1
    assert slots[0]["account"] == ACCOUNT
    assert slots[0]["property"] == PROP


def test_close_submission_is_atomic_across_verdict_and_slot(tmp_path):
    """If the slot insert fails, the verdict must not survive either.

    Without a shared transaction the UPDATE would commit and the slot would be
    lost — exactly the leak this module exists to prevent.
    """
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    conn.execute(
        "CREATE TRIGGER block_slots BEFORE INSERT ON quota_slots "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.close_submission(conn, sid, "submitted")
    finally:
        conn.execute("DROP TRIGGER block_slots")

    row = conn.execute(
        "SELECT verdict, committed FROM submissions WHERE id=?", (sid,)
    ).fetchone()
    assert row["verdict"] is None
    assert row["committed"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"] == 0


def test_double_close_is_ignored_and_logged(tmp_path, caplog):
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    store.close_submission(conn, sid, "submitted")
    with caplog.at_level("WARNING"):
        store.close_submission(conn, sid, "submitted")
    assert conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"] == 1
    assert "already committed" in caplog.text


def test_abandon_submission_spends_no_slot(tmp_path):
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    store.abandon_submission(conn, sid, "session_expired")
    assert store.open_submissions(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"] == 0
    verdict = conn.execute(
        "SELECT verdict FROM submissions WHERE id=?", (sid,)
    ).fetchone()["verdict"]
    assert verdict == "abandoned:session_expired"


def test_reconcile_closes_stragglers_conservatively(tmp_path):
    conn = _conn(tmp_path)
    sid1 = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    sid2 = store.open_submission(conn, URL + "2", PROP, ACCOUNT, job_id=None)
    # Past the default 15-minute grace period, or reconcile would (correctly)
    # leave these alone as possibly still in flight.
    _backdate(conn, sid1, minutes=30)
    _backdate(conn, sid2, minutes=30)

    closed = store.reconcile(conn)
    assert closed == 2
    assert store.open_submissions(conn) == []

    slots = conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()
    assert slots["n"] == 2

    verdicts = {
        row["verdict"]
        for row in conn.execute("SELECT verdict FROM submissions").fetchall()
    }
    assert verdicts == {"unknown_crash"}


def test_reconcile_stamps_the_slot_at_request_time_not_now(tmp_path):
    """A straggler from three days ago must not read as a slot spent today, or
    the property looks exhausted for another 24h."""
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    old = store.utc_iso(datetime.now(UTC) - timedelta(days=3))
    conn.execute("UPDATE submissions SET requested_at=? WHERE id=?", (old, sid))

    store.reconcile(conn)
    used_at = conn.execute("SELECT used_at FROM quota_slots").fetchone()["used_at"]
    assert used_at == old


def test_reconcile_leaves_recent_rows_alone(tmp_path):
    """A row opened moments ago may belong to a live run in another process."""
    conn = _conn(tmp_path)
    store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    assert store.reconcile(conn) == 0
    assert len(store.open_submissions(conn)) == 1


def test_reconcile_is_a_noop_when_nothing_is_open(tmp_path):
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    store.close_submission(conn, sid, "submitted")
    assert store.reconcile(conn) == 0


def test_close_submission_rejects_unknown_id(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        store.close_submission(conn, 9999, "submitted")


def test_timestamps_are_stored_in_normalised_form(tmp_path):
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    store.close_submission(conn, sid, "submitted")
    requested = conn.execute(
        "SELECT requested_at FROM submissions WHERE id=?", (sid,)
    ).fetchone()["requested_at"]
    used = conn.execute("SELECT used_at FROM quota_slots").fetchone()["used_at"]
    for value in (requested, used):
        assert value.endswith("+00:00")
        assert len(value) == len("2026-01-01T00:00:00.000000+00:00")
