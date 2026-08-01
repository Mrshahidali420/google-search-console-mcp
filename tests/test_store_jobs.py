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
