import pytest

from gsc_core import store


def _conn(tmp_path):
    return store.connect(tmp_path / "state.db")


def test_create_and_get_job(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {"sites": ["example.com"], "limit": 5})
    job = store.get_job(conn, "job-1")
    assert job["state"] == "pending"
    assert job["params"] == {"sites": ["example.com"], "limit": 5}
    assert job["started_at"] is not None


def test_get_job_returns_none_when_absent(tmp_path):
    conn = _conn(tmp_path)
    assert store.get_job(conn, "nope") is None


def test_update_job_state_and_progress(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    store.update_job(conn, "job-1", state="running",
                     progress={"done": 2, "total": 5})
    job = store.get_job(conn, "job-1")
    assert job["state"] == "running"
    assert job["progress"] == {"done": 2, "total": 5}


def test_terminal_state_sets_finished_at(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    store.update_job(conn, "job-1", state="completed")
    assert store.get_job(conn, "job-1")["finished_at"] is not None


def test_non_terminal_state_leaves_finished_at_empty(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    store.update_job(conn, "job-1", state="running")
    assert store.get_job(conn, "job-1")["finished_at"] is None


def test_update_job_rejects_unknown_state(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    with pytest.raises(ValueError):
        store.update_job(conn, "job-1", state="wandering")


def test_list_jobs_filters_by_state(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    store.create_job(conn, "job-2", {})
    store.update_job(conn, "job-2", state="running")
    running = store.list_jobs(conn, state="running")
    assert [job["id"] for job in running] == ["job-2"]


def test_resuming_a_finished_job_clears_finished_at_and_error(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    store.update_job(conn, "job-1", state="failed", error="boom")
    assert store.get_job(conn, "job-1")["finished_at"] is not None

    store.update_job(conn, "job-1", state="running")
    job = store.get_job(conn, "job-1")
    assert job["finished_at"] is None
    assert job["error"] is None


def test_update_job_raises_for_unknown_id(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        store.update_job(conn, "no-such-job", state="running")


def test_reconcile_jobs_fails_orphaned_running_jobs(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    store.create_job(conn, "job-2", {})
    store.update_job(conn, "job-1", state="running")
    store.update_job(conn, "job-2", state="completed")

    assert store.reconcile_jobs(conn) == 1
    assert store.get_job(conn, "job-1")["state"] == "failed"
    assert "restart" in store.get_job(conn, "job-1")["error"]
    assert store.get_job(conn, "job-2")["state"] == "completed"


def test_reconcile_jobs_is_a_noop_when_nothing_is_running(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {})
    assert store.reconcile_jobs(conn) == 0


def test_corrupt_progress_does_not_break_the_listing(tmp_path):
    conn = _conn(tmp_path)
    store.create_job(conn, "job-1", {"ok": True})
    store.create_job(conn, "job-2", {"ok": True})
    conn.execute("UPDATE jobs SET progress='{ not json' WHERE id='job-1'")

    jobs = store.list_jobs(conn)
    assert len(jobs) == 2
    assert jobs[0]["progress"] is None
    assert jobs[1]["params"] == {"ok": True}
