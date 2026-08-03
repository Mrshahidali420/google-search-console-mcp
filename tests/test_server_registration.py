"""The submission tools as a CLIENT sees them, plus the startup sweep.

Everything under this milestone was built behind server.py: a run loop, a
quota gate, a bridge, a worker thread. None of it is worth anything until
FastMCP knows the four tool names, so these tests go through the registry
rather than through the module, and they check the wiring rather than the
behaviour — the behaviour has its own tests in test_tools_submit.py and
test_tools_jobs.py.

Nothing here starts a thread, opens a browser or sleeps: every wrapper is
called with its tool body replaced by a spy, which is also what makes
"the wrapper delegates and does nothing else" an assertable claim.
"""
from __future__ import annotations

from typing import Any

import pytest

from gsc_core import store
from gsc_mcp import server

SUBMISSION_TOOLS = ("gsc_request_indexing", "gsc_start_indexing_job",
                    "gsc_job_status", "gsc_stop_job")


def registered() -> dict[str, Any]:
    """What a client can actually call, read off FastMCP's own registry.

    Private API, for the reason test_no_stdout.py gives: it is the only
    thing that knows what was registered, it fails loudly on an upgrade,
    and the mcp>=1.2,<2.0 pin holds the surface still.
    """
    return {tool.name: tool.fn for tool in server.mcp._tool_manager.list_tools()}


def test_the_four_submission_tools_are_registered() -> None:
    assert set(SUBMISSION_TOOLS) <= set(registered())


@pytest.mark.parametrize("name", SUBMISSION_TOOLS)
def test_every_submission_tool_documents_itself(name: str) -> None:
    """A tool's docstring is its entire interface to a model, and FastMCP
    ships it as the description. An undocumented tool is registered but
    unusable."""
    tool = {t.name: t for t in server.mcp._tool_manager.list_tools()}[name]
    assert tool.description and len(tool.description) > 100


# ---------------------------------------------------------------------------
# Delegation: the wrapper passes the call through and adds nothing
# ---------------------------------------------------------------------------

#: One call per tool: (tool name, tool_submit function name, args, kwargs,
#: what the body should be handed). Written out rather than derived so a
#: wrapper that quietly reorders or renames a parameter fails here.
_DELEGATIONS = [
    ("gsc_request_indexing", "request_indexing",
     (["https://example.com/a"],), {}, (["https://example.com/a"],), {}),
    ("gsc_start_indexing_job", "start_indexing_job",
     (["https://example.net/b"],), {}, (["https://example.net/b"],), {}),
    ("gsc_job_status", "job_status", ("job-1",), {}, ("job-1",), {}),
    ("gsc_stop_job", "stop_job", ("job-1",), {}, ("job-1",), {}),
]


@pytest.mark.parametrize(
    ("tool_name", "body_name", "args", "kwargs", "want_args", "want_kwargs"),
    _DELEGATIONS,
    ids=[row[0] for row in _DELEGATIONS],
)
def test_the_wrapper_delegates_and_returns_the_body_result(
    tool_name: str, body_name: str, args: tuple, kwargs: dict,
    want_args: tuple, want_kwargs: dict, monkeypatch: pytest.MonkeyPatch,
    home,
) -> None:
    """The whole contract of a wrapper: arguments in, result out, untouched.

    Identity on the sentinel, not equality: a wrapper that rebuilt the dict
    would be putting result-shaping logic in server.py, which is the one
    file this milestone is not allowed to grow.

    `home` is taken even though the spy means no real body runs. That is
    exactly the assumption a wrapper regression would break: a wrapper that
    stopped delegating would run the real tool against the contributor's
    own GSC_MCP_HOME — their store, and their eleven-a-day quota.
    """
    sentinel: dict = {"ok": True, "sentinel": object()}
    seen: list[tuple] = []

    def spy(*a: Any, **k: Any) -> dict:
        seen.append((a, k))
        return sentinel

    monkeypatch.setattr(server.tools_submit, body_name, spy)
    result = registered()[tool_name](*args, **kwargs)

    assert seen == [(want_args, want_kwargs)]
    assert result is sentinel


def test_job_status_can_be_called_with_no_job_id(
    monkeypatch: pytest.MonkeyPatch, home,
) -> None:
    """"the most recent job" is the documented default, so the default has
    to survive the wrapper — a required parameter here would make the
    no-argument call a schema error the model never sees the reason for."""
    seen: list[tuple] = []
    monkeypatch.setattr(server.tools_submit, "job_status",
                        lambda job_id=None: seen.append(job_id) or {"ok": True})

    assert registered()["gsc_job_status"]()["ok"] is True
    assert seen == [None]


def test_no_wrapper_swallows_the_bodys_refusal(
    monkeypatch: pytest.MonkeyPatch, home,
) -> None:
    """A refusal envelope is the tool bodies' job. If a wrapper ever grew a
    try/except of its own it would rewrite them, and the caller would lose
    the `fix` string that tells it what to do instead."""
    refusal = {"ok": False, "error": "no_browser", "fix": "pair one"}
    monkeypatch.setattr(server.tools_submit, "request_indexing",
                        lambda urls: refusal)
    assert registered()["gsc_request_indexing"]([]) is refusal


# ---------------------------------------------------------------------------
# The startup sweep
# ---------------------------------------------------------------------------

def test_startup_reconciles_jobs_left_running_by_a_restart(
    home, store_conn,
) -> None:
    store.create_job(store_conn, "orphan", {"urls": []})
    store.update_job(store_conn, "orphan", state="running")

    server._reconcile_at_startup()

    assert store.get_job(store_conn, "orphan")["state"] == "failed"


def test_startup_reconciles_jobs_left_pending_by_a_restart(
    home, store_conn,
) -> None:
    """A job row is created pending and only becomes running once its
    worker picks it up. A crash in that window leaves a row no worker in
    this process will ever touch, so it is orphaned for exactly the same
    reason a running row is."""
    store.create_job(store_conn, "never-started", {"urls": []})

    server._reconcile_at_startup()

    assert store.get_job(store_conn, "never-started")["state"] == "failed"


def test_startup_leaves_settled_jobs_alone(home, store_conn) -> None:
    store.create_job(store_conn, "done", {"urls": []})
    store.update_job(store_conn, "done", state="completed")

    server._reconcile_at_startup()

    assert store.get_job(store_conn, "done")["state"] == "completed"


def test_startup_reconciles_open_submission_rows_too(
    home, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The submissions ledger has the same crash hole as the jobs table:
    an open row holds a quota slot that never settles. Sweeping the jobs
    and forgetting the ledger would leave a property looking exhausted."""
    seen: list[str] = []
    monkeypatch.setattr(server.store, "reconcile",
                        lambda conn, *a, **k: seen.append("submissions"))

    server._reconcile_at_startup()

    assert seen == ["submissions"]


def test_a_broken_store_does_not_stop_the_server_starting(
    home, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Startup reconciliation is housekeeping. A server that refuses to
    start because it could not tidy up is strictly worse than one that
    starts with a stale row in it.

    The log line carries the exception TYPE only. An unauthored message
    can hold a filesystem path — which on this project's own machines
    contains the operator's account name — and log files are shipped.
    """
    def boom(*a: Any, **k: Any):
        raise RuntimeError(
            "database at /home/someone@example.com/db is locked")

    monkeypatch.setattr(server.store, "session", boom)
    with caplog.at_level("WARNING"):
        server._reconcile_at_startup()          # must not raise

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert "example.com" not in logged
    assert "locked" not in logged


def test_main_reconciles_before_it_starts_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order is the whole point. Reconciling after mcp.run() returns would
    run at shutdown, when the rows it exists to close are being written."""
    order: list[str] = []
    monkeypatch.setattr(server, "_reconcile_at_startup",
                        lambda: order.append("reconcile"))
    monkeypatch.setattr(server.mcp, "run", lambda: order.append("run"))

    server.main()

    assert order == ["reconcile", "run"]
