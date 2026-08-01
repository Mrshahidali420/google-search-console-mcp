"""The single SQLite store.

Replaces what used to be three things: quota_ledger.json, journal.jsonl and a
Google Sheet. One file means one locking story, and it means a slot and the
submission that spent it can be written in the same transaction — which the
old JSON-plus-filelock arrangement could not guarantee.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Iterator

from . import paths, runlog

log = runlog.get(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    property   TEXT PRIMARY KEY,
    host       TEXT NOT NULL,
    permission TEXT,
    sitemaps   TEXT,
    synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS urls (
    url            TEXT PRIMARY KEY,
    property       TEXT NOT NULL,
    status         TEXT,
    reason         TEXT,
    first_seen     TEXT NOT NULL,
    checked_at     TEXT,
    last_submitted TEXT
);
CREATE INDEX IF NOT EXISTS idx_urls_property_checked ON urls (property, checked_at);

CREATE TABLE IF NOT EXISTS submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL,
    property     TEXT NOT NULL,
    account      TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    verdict      TEXT,
    job_id       TEXT,
    committed    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_submissions_open
    ON submissions (committed) WHERE committed = 0;

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    params      TEXT NOT NULL,
    state       TEXT NOT NULL,
    progress    TEXT,
    started_at  TEXT,
    finished_at TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS quota_slots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    account  TEXT NOT NULL,
    property TEXT NOT NULL,
    used_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_property ON quota_slots (property, used_at);
CREATE INDEX IF NOT EXISTS idx_slots_account  ON quota_slots (account, used_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the store, creating schema if absent. Safe to call concurrently."""
    target = path or paths.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # Match the connect timeout — a PRAGMA here silently overrides it, and two
    # different waits in one function is a trap for whoever debugs a lock.
    conn.execute("PRAGMA busy_timeout=30000")
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        log.warning(
            "WAL unavailable (journal_mode=%s); concurrent server and CLI "
            "access will serialise", mode)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)

    # Stamp the version only when the file is new. Overwriting on every open
    # would let a v1 database silently claim to be v2, so no future migration
    # could ever detect what it is actually looking at.
    existing = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the store and close it again afterwards.

    Long-lived callers such as the MCP server may use connect() directly and
    own the lifecycle themselves; everything short-lived should use this.
    """
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


def _cleanup(conn: sqlite3.Connection, statement: str) -> None:
    """Best-effort transaction cleanup.

    A ROLLBACK can itself fail — SQLite may already have auto-rolled-back on
    a disk-full or I/O error, leaving no active transaction. Letting that
    raise would replace the original exception with a misleading one.
    """
    try:
        conn.execute(statement)
    except sqlite3.Error:
        log.debug("cleanup statement failed: %s", statement, exc_info=True)


_savepoints = itertools.count()


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction, re-entrant via SAVEPOINT.

    BEGIN IMMEDIATE so concurrent writers queue at the start rather than
    failing late with SQLITE_BUSY partway through.

    Re-entrancy matters because every accessor in this module opens its own
    transaction; without it a caller could not compose several of them into
    one atomic batch, and a bulk path would be forced into one transaction
    per row.

    Catches BaseException, not Exception: an MCP client disconnecting mid-call
    raises asyncio.CancelledError and a CLI interrupt raises KeyboardInterrupt.
    Neither is an Exception, and letting either skip both COMMIT and ROLLBACK
    would leave the transaction open — poisoning the connection for every
    later tx and holding the write lock against the other process.
    """
    if conn.in_transaction:
        name = f"sp_{next(_savepoints)}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            _cleanup(conn, f"ROLLBACK TO {name}")
            _cleanup(conn, f"RELEASE {name}")
            raise
        conn.execute(f"RELEASE {name}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        _cleanup(conn, "ROLLBACK")
        raise
    conn.execute("COMMIT")


_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%f+00:00"


def utc_iso(value: str | datetime | None) -> str | None:
    """Normalise a timestamp to fixed-width UTC ISO-8601.

    Timestamps here are compared as strings, so lexicographic order has to
    equal chronological order. That only holds when every value carries the
    same offset and the same width. Callers supply values from several
    sources — Google's URL Inspection API returns a 'Z' suffix, a naive
    datetime carries no offset, and isoformat() omits microseconds when they
    are zero — so normalise on write rather than trusting the caller.
    """
    if value is None:
        return None
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime(_TS_FORMAT)


def upsert_site(conn: sqlite3.Connection, property: str, host: str,
                permission: str | None, sitemaps: list[str]) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO sites (property, host, permission, sitemaps, synced_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(property) DO UPDATE SET "
            "  host=excluded.host, permission=excluded.permission, "
            "  sitemaps=excluded.sitemaps, synced_at=excluded.synced_at",
            (property, host, permission, json.dumps(sitemaps),
             utc_iso(datetime.now(UTC))),
        )


def get_sites(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sites ORDER BY property").fetchall()
    return [
        {
            "property": row["property"],
            "host": row["host"],
            "permission": row["permission"],
            "sitemaps": json.loads(row["sitemaps"] or "[]"),
            "synced_at": row["synced_at"],
        }
        for row in rows
    ]


def upsert_url(conn: sqlite3.Connection, url: str, property: str,
               status: str | None, reason: str | None,
               checked_at: str | None) -> None:
    """Insert or update a URL. first_seen is written once and never changed.

    A re-discovery pass (from sitemaps.py) carries no inspection result, so
    status/reason/checked_at of None mean "no new information" and must not
    overwrite what a prior inspection recorded. An inspection result (from
    inspect.py) always carries a status, so in that case status and reason
    move together — including clearing a reason when status improves to a
    value that no longer needs one.
    """
    with tx(conn):
        conn.execute(
            "INSERT INTO urls (url, property, status, reason, first_seen, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  property = excluded.property, "
            "  status = COALESCE(excluded.status, urls.status), "
            "  reason = CASE WHEN excluded.status IS NULL "
            "               THEN urls.reason ELSE excluded.reason END, "
            "  checked_at = COALESCE(excluded.checked_at, urls.checked_at)",
            (url, property, status, reason,
             utc_iso(datetime.now(UTC)), utc_iso(checked_at)),
        )


def get_urls(conn: sqlite3.Connection, property: str,
             status: str | None = None) -> list[dict]:
    if status is None:
        rows = conn.execute(
            "SELECT * FROM urls WHERE property=? ORDER BY url", (property,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM urls WHERE property=? AND status=? ORDER BY url",
            (property, status),
        ).fetchall()
    return [dict(row) for row in rows]


def stale_urls(conn: sqlite3.Connection, property: str, ttl_days: int,
               now: datetime | None = None) -> list[str]:
    """URLs never checked, or checked longer ago than ttl_days.

    This is what makes discovery incremental: a second run re-inspects only
    what has gone stale instead of the whole sitemap.
    """
    moment = now or datetime.now(UTC)
    cutoff = utc_iso(moment - timedelta(days=ttl_days))
    rows = conn.execute(
        "SELECT url FROM urls WHERE property=? "
        "AND (checked_at IS NULL OR checked_at < ?) ORDER BY url",
        (property, cutoff),
    ).fetchall()
    return [row["url"] for row in rows]


def mark_submitted(conn: sqlite3.Connection, url: str, when: str) -> None:
    """Record a submission timestamp. Raises KeyError if the url is unknown.

    A silent no-op here would read back as "not submitted", causing the
    caller to resubmit the same url and burn another quota slot.
    """
    with tx(conn):
        cursor = conn.execute(
            "UPDATE urls SET last_submitted=? WHERE url=?",
            (utc_iso(when), url),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"no url row for {url}")


def open_submission(conn: sqlite3.Connection, url: str, property: str,
                    account: str, job_id: str | None) -> int:
    """Record intent to submit, before the click. Spends no slot yet."""
    with tx(conn):
        cursor = conn.execute(
            "INSERT INTO submissions "
            "(url, property, account, requested_at, job_id, committed) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (url, property, account, utc_iso(datetime.now(UTC)), job_id),
        )
        return int(cursor.lastrowid)


def close_submission(conn: sqlite3.Connection, submission_id: int,
                     verdict: str, *, used_at: str | None = None) -> None:
    """Record the outcome and spend the slot, atomically.

    used_at defaults to now, but callers reconciling an old straggler should
    pass the original requested_at — see reconcile() for why.
    """
    with tx(conn):
        row = conn.execute(
            "SELECT account, property, verdict, committed "
            "FROM submissions WHERE id=?",
            (submission_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"no submission with id {submission_id}")
        if row["committed"]:
            log.warning(
                "submission %s already committed as %r; ignoring re-close "
                "with %r", submission_id, row["verdict"], verdict)
            return

        conn.execute(
            "UPDATE submissions SET verdict=?, committed=1 WHERE id=?",
            (verdict, submission_id),
        )
        conn.execute(
            "INSERT INTO quota_slots (account, property, used_at) VALUES (?, ?, ?)",
            (row["account"], row["property"],
             utc_iso(used_at) if used_at else utc_iso(datetime.now(UTC))),
        )


def abandon_submission(conn: sqlite3.Connection, submission_id: int,
                       reason: str) -> None:
    """Close a row WITHOUT spending a slot.

    For failures that happened before the click reached Google — the page
    never loaded, the session had expired. Charging those would drain a
    property's budget on submissions that never occurred.
    """
    with tx(conn):
        row = conn.execute(
            "SELECT committed FROM submissions WHERE id=?", (submission_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no submission with id {submission_id}")
        if row["committed"]:
            log.warning("submission %s already committed; not abandoning",
                        submission_id)
            return
        conn.execute(
            "UPDATE submissions SET verdict=?, committed=1 WHERE id=?",
            (f"abandoned:{reason}", submission_id),
        )


def open_submissions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM submissions WHERE committed=0 ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def reconcile(conn: sqlite3.Connection, verdict: str = "unknown_crash", *,
             grace_minutes: int = 15, now: datetime | None = None) -> int:
    """Close rows a crash left open, charging a slot for each.

    Conservative on purpose: we cannot know whether Google accepted the click,
    so we assume it did. Over-counting costs a slot; under-counting fires into
    an exhausted property and earns a real 'Quota Exceeded'.

    Only rows older than grace_minutes are touched. A row opened moments ago
    may belong to a submission another process still has in flight; closing
    it here would steal it — the real close_submission() call would then see
    committed=1 and silently no-op, losing the true verdict.

    The slot is stamped at the original requested_at, not at reconcile time.
    Stamping "now" would make a three-day-old straggler look like it just
    spent its slot, keeping the property looking exhausted for another
    rolling-window cycle after the crash is already resolved.
    """
    moment = now or datetime.now(UTC)
    cutoff = utc_iso(moment - timedelta(minutes=grace_minutes))
    stragglers = [
        row for row in open_submissions(conn) if row["requested_at"] < cutoff
    ]
    for row in stragglers:
        close_submission(conn, row["id"], verdict, used_at=row["requested_at"])
    if stragglers:
        log.warning("reconciled %d submission(s) left open by a crash",
                    len(stragglers))
    return len(stragglers)


JOB_STATES = frozenset({
    "pending", "running", "completed",
    "stopped_throttled", "stopped_user", "failed",
})

_TERMINAL_JOB_STATES = frozenset({
    "completed", "stopped_throttled", "stopped_user", "failed",
})


def create_job(conn: sqlite3.Connection, job_id: str, params: dict) -> None:
    with tx(conn):
        conn.execute(
            "INSERT INTO jobs (id, params, state, started_at) "
            "VALUES (?, ?, 'pending', ?)",
            (job_id, json.dumps(params), utc_iso(datetime.now(UTC))),
        )


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_row(row) if row else None


def update_job(conn: sqlite3.Connection, job_id: str, *,
               state: str | None = None, progress: dict | None = None,
               error: str | None = None) -> None:
    if state is not None and state not in JOB_STATES:
        raise ValueError(f"unknown job state: {state}")

    assignments: list[str] = []
    values: list = []
    if state is not None:
        assignments.append("state=?")
        values.append(state)
        if state in _TERMINAL_JOB_STATES:
            assignments.append("finished_at=?")
            values.append(utc_iso(datetime.now(UTC)))
    if progress is not None:
        assignments.append("progress=?")
        values.append(json.dumps(progress))
    if error is not None:
        assignments.append("error=?")
        values.append(error)
    if not assignments:
        return

    values.append(job_id)
    with tx(conn):
        conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", values
        )


def list_jobs(conn: sqlite3.Connection, state: str | None = None) -> list[dict]:
    if state is None:
        rows = conn.execute("SELECT * FROM jobs ORDER BY started_at").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY started_at", (state,)
        ).fetchall()
    return [_job_row(row) for row in rows]


def _job_row(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["params"] = json.loads(job["params"] or "{}")
    job["progress"] = json.loads(job["progress"]) if job["progress"] else None
    return job
