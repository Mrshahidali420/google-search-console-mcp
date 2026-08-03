"""The three job tools: start, status, stop.

These are the wrappers, so what is pinned here is the ENVELOPE — what a
caller can act on and what must never reach a transcript. `jobs` itself is
replaced in most of these: the orchestration has its own file, and a tool
test that spawns real threads tests the wrong layer twice.

Two standing rules the tests enforce rather than trust:

* a tool NEVER raises — every path returns a dict, because an exception
  crossing the MCP boundary is a stack trace in someone's client;
* a failure is reported by exception TYPE NAME, never by its message,
  which can carry a filesystem path holding the operator's account name.
"""
from __future__ import annotations

import pytest

from _logcheck import capturing, logged_text
from gsc_core import store
from gsc_mcp import tools_submit

PROPERTY = "sc-domain:example.com"
SECRET_PATH = "C:/Users/secret-operator/thing"
SECRET_EMAIL = "owner@example.net"


@pytest.fixture()
def seeded(store_conn):
    """A store that knows one property, so routing has somewhere to go."""
    store.upsert_site(store_conn, PROPERTY,
                      PROPERTY.removeprefix("sc-domain:"), "siteOwner", [])
    return store_conn


def _seed_job(conn, job_id: str, *, state: str = "completed",
              progress: dict | None = None, error: str | None = None) -> None:
    store.create_job(conn, job_id, {"urls": ["https://example.com/a"],
                                    "total": 1})
    store.update_job(conn, job_id, state=state, progress=progress, error=error)


def _never(*args, **kwargs):
    raise AssertionError("this should never have been reached")


# --- start -------------------------------------------------------------------

def test_start_indexing_job_refuses_an_empty_list(monkeypatch, seeded):
    monkeypatch.setattr(tools_submit.jobs, "start", _never)
    result = tools_submit.start_indexing_job([])
    assert result["ok"] is False
    assert result["error"] == "no_urls"


def test_start_indexing_job_returns_the_id_and_does_not_block(monkeypatch,
                                                              seeded):
    """The whole point of the job tools: the call returns at once and the
    caller polls. A tool that waited for the run would block a client for
    hours."""
    monkeypatch.setattr(tools_submit.jobs, "start", lambda urls, cfg: "job-1")
    result = tools_submit.start_indexing_job(["https://example.com/a"])
    assert result["ok"] is True
    assert result["job_id"] == "job-1"
    assert result["total"] == 1


def test_start_indexing_job_takes_more_urls_than_the_synchronous_cap(
        monkeypatch, seeded):
    """The reason this tool exists. A cap here would leave large runs with
    nowhere to go."""
    urls = [f"https://example.com/{index}"
            for index in range(tools_submit.HARD_SYNC_CAP * 4)]
    monkeypatch.setattr(tools_submit.jobs, "start", lambda u, cfg: "job-1")
    result = tools_submit.start_indexing_job(urls)
    assert result["ok"] is True and result["total"] == len(urls)


def test_starting_a_second_job_is_a_clean_refusal(monkeypatch, seeded):
    def busy(urls, cfg):
        raise tools_submit.jobs.AlreadyRunning("job-1 is still running")

    monkeypatch.setattr(tools_submit.jobs, "start", busy)
    result = tools_submit.start_indexing_job(["https://example.com/a"])
    assert result["ok"] is False
    assert result["error"] == "job_already_running"
    # The refusal names the job the caller has to deal with; without the id
    # the fix ("stop it") is not actionable.
    assert "job-1" in result["detail"]


def test_starting_a_job_with_no_known_properties_is_refused(monkeypatch, home):
    """Every URL would report no_property. Refusing costs nothing; running
    burns a browser session to produce a list of misses."""
    monkeypatch.setattr(tools_submit.jobs, "start", _never)
    result = tools_submit.start_indexing_job(["https://example.com/a"])
    assert result["ok"] is False
    assert result["error"] == "no_properties"


def test_start_indexing_job_reports_an_unexpected_failure_by_type(monkeypatch,
                                                                  seeded):
    def explode(urls, cfg):
        raise ValueError(f"{SECRET_PATH} for {SECRET_EMAIL}")

    monkeypatch.setattr(tools_submit.jobs, "start", explode)
    with capturing(tools_submit.log) as records:
        result = tools_submit.start_indexing_job(["https://example.com/a"])

    assert result["ok"] is False and result["error"] == "unexpected"
    assert result["detail"] == "ValueError"
    text = logged_text(records) + repr(result)
    assert "secret-operator" not in text and SECRET_EMAIL not in text
    assert "ValueError" in text                    # live-capture canary


# --- status ------------------------------------------------------------------

def test_job_status_with_no_id_reports_the_most_recent(store_conn):
    _seed_job(store_conn, "job-old", progress={"done": 1, "total": 1,
                                               "results": []})
    _seed_job(store_conn, "job-new", state="running",
              progress={"done": 0, "total": 3, "results": []})

    result = tools_submit.job_status()
    assert result["ok"] is True
    assert result["job_id"] == "job-new"
    assert result["state"] == "running"
    assert result["done"] == 0 and result["total"] == 3


def test_job_status_for_an_unknown_id_is_a_clean_refusal(store_conn):
    result = tools_submit.job_status("nope")
    assert result["ok"] is False
    assert result["error"] == "unknown_job"


def test_job_status_with_no_jobs_at_all_is_a_clean_refusal(store_conn):
    result = tools_submit.job_status()
    assert result["ok"] is False
    assert result["error"] == "no_jobs"


def test_job_status_reports_a_job_that_has_not_started_yet(store_conn):
    """The row is created pending and the worker flips it to running. A
    status call landing in that window must still answer."""
    store.create_job(store_conn, "job-1", {"urls": [], "total": 0})
    result = tools_submit.job_status("job-1")
    assert result["ok"] is True
    assert result["state"] == "pending"
    assert result["done"] == 0 and result["results"] == []


def test_job_status_carries_the_per_url_outcomes(store_conn):
    _seed_job(store_conn, "job-1", progress={
        "done": 2, "total": 2,
        "results": [
            {"url": "https://example.com/a", "property": PROPERTY,
             "outcome": "submitted", "spent_slot": True},
            {"url": "https://example.net/b", "property": None,
             "outcome": "no_property", "spent_slot": False},
        ]})
    result = tools_submit.job_status("job-1")
    assert [row["outcome"] for row in result["results"]] == [
        "submitted", "no_property"]


def test_job_status_keeps_no_property_and_no_quota_apart(store_conn):
    """Two different problems with two different fixes. Collapsing them
    into one "skipped" count would leave the operator guessing."""
    _seed_job(store_conn, "job-1", progress={
        "done": 2, "total": 2,
        "results": [
            {"url": "https://example.net/b", "property": None,
             "outcome": "no_property", "spent_slot": False},
            {"url": "https://example.com/c", "property": PROPERTY,
             "outcome": "no_quota", "spent_slot": False},
        ]})
    notes = tools_submit.job_status("job-1")["notes"]
    assert len(notes) == 2
    assert any("property" in note for note in notes)
    assert any("quota" in note for note in notes)


def test_job_status_reports_a_failed_job_with_its_stored_error(store_conn):
    _seed_job(store_conn, "job-1", state="failed", error="NoBrowser")
    result = tools_submit.job_status("job-1")
    assert result["ok"] is True                # the CALL worked
    assert result["state"] == "failed"
    assert result["error"] == "NoBrowser"


def test_job_status_reports_a_worker_that_died_before_it_could_start(
        monkeypatch, store_conn):
    """The operator-visible half of the worker-startup defect.

    A worker that dies acquiring its connection must leave a row the
    operator can read a verdict off. Left pending with no error and no live
    worker, gsc_job_status says "queued, be patient" forever — and
    reconcile_jobs() sweeps only "running", so nothing ever corrects it.
    """
    _seed_job(store_conn, "job-1", state="failed", error="_ConnectFailure")
    monkeypatch.setattr(tools_submit.jobs, "is_running", lambda job_id: False)

    result = tools_submit.job_status("job-1")
    assert result["ok"] is True
    assert result["state"] == "failed"
    assert result["error"] == "_ConnectFailure"
    assert result["live"] is False
    assert result["state"] not in tools_submit.jobs.ACTIVE_STATES


def test_job_status_says_whether_a_worker_is_actually_live(monkeypatch,
                                                           store_conn):
    """A row saying "running" after a crash is a lie until the next startup
    reconcile. `live` is the only field that knows the difference."""
    _seed_job(store_conn, "job-1", state="running")
    monkeypatch.setattr(tools_submit.jobs, "is_running", lambda job_id: False)
    assert tools_submit.job_status("job-1")["live"] is False
    monkeypatch.setattr(tools_submit.jobs, "is_running", lambda job_id: True)
    assert tools_submit.job_status("job-1")["live"] is True


def test_job_status_reports_an_unexpected_failure_by_type(monkeypatch,
                                                          store_conn):
    def explode(*args, **kwargs):
        raise ValueError(f"{SECRET_PATH} for {SECRET_EMAIL}")

    monkeypatch.setattr(tools_submit.store, "list_jobs", explode)
    with capturing(tools_submit.log) as records:
        result = tools_submit.job_status()

    assert result["ok"] is False and result["error"] == "unexpected"
    assert result["detail"] == "ValueError"
    text = logged_text(records) + repr(result)
    assert "secret-operator" not in text and SECRET_EMAIL not in text
    assert "ValueError" in text                    # live-capture canary


# --- stop --------------------------------------------------------------------

def test_stop_job_signals_a_live_job(monkeypatch, store_conn):
    monkeypatch.setattr(tools_submit.jobs, "stop", lambda job_id: True)
    result = tools_submit.stop_job("job-1")
    assert result["ok"] is True and result["job_id"] == "job-1"


def test_stop_job_on_a_finished_job_says_so_without_failing(monkeypatch,
                                                            store_conn):
    """Asking a finished job to stop is not an error — the caller got what
    they wanted. Refusing would read as "it is still running"."""
    _seed_job(store_conn, "job-1")
    monkeypatch.setattr(tools_submit.jobs, "stop", lambda job_id: False)
    result = tools_submit.stop_job("job-1")
    assert result["ok"] is True
    assert result["state"] == "completed"


def test_stop_job_for_an_unknown_id_is_a_clean_refusal(monkeypatch, store_conn):
    monkeypatch.setattr(tools_submit.jobs, "stop", lambda job_id: False)
    result = tools_submit.stop_job("nope")
    assert result["ok"] is False
    assert result["error"] == "unknown_job"


def test_stop_job_reports_an_unexpected_failure_by_type(monkeypatch, store_conn):
    def explode(job_id):
        raise ValueError(f"{SECRET_PATH} for {SECRET_EMAIL}")

    monkeypatch.setattr(tools_submit.jobs, "stop", explode)
    with capturing(tools_submit.log) as records:
        result = tools_submit.stop_job("job-1")

    assert result["ok"] is False and result["error"] == "unexpected"
    assert result["detail"] == "ValueError"
    text = logged_text(records) + repr(result)
    assert "secret-operator" not in text and SECRET_EMAIL not in text
    assert "ValueError" in text                    # live-capture canary


# --- stdout ------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda: tools_submit.start_indexing_job([]),
    lambda: tools_submit.job_status("nope"),
    lambda: tools_submit.stop_job("nope"),
])
def test_the_job_tools_write_nothing_to_stdout(store_conn, capsys, call):
    call()
    assert capsys.readouterr().out == ""
