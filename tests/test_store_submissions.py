from gsc_core import store

PROP = "sc-domain:example.com"
ACCOUNT = "user@example.com"
URL = "https://example.com/page"


def _conn(tmp_path):
    return store.connect(tmp_path / "state.db")


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


def test_reconcile_closes_stragglers_conservatively(tmp_path):
    conn = _conn(tmp_path)
    store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    store.open_submission(conn, URL + "2", PROP, ACCOUNT, job_id=None)

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


def test_reconcile_is_a_noop_when_nothing_is_open(tmp_path):
    conn = _conn(tmp_path)
    sid = store.open_submission(conn, URL, PROP, ACCOUNT, job_id=None)
    store.close_submission(conn, sid, "submitted")
    assert store.reconcile(conn) == 0


def test_close_submission_rejects_unknown_id(tmp_path):
    import pytest

    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        store.close_submission(conn, 9999, "submitted")
