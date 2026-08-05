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

SCHEMA_VERSION = 3

#: Columns added to existing tables after their first release, as
#: (table, column, declaration). CREATE TABLE IF NOT EXISTS gives an old
#: database new TABLES for free; it does nothing whatsoever for a new COLUMN
#: on a table that already exists, which is a quiet way to ship a schema the
#: code assumes and the store does not have. Each entry is applied only when
#: absent, so this stays idempotent and order-independent. NULL-able and
#: without a default, always: SQLite rewrites nothing for that, and a column
#: added here is by definition unknown for every row that predates it.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("jobs", "stop_reason", "TEXT"),
)

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
    error       TEXT,
    stop_reason TEXT
);

CREATE TABLE IF NOT EXISTS quota_slots (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    account  TEXT NOT NULL,
    property TEXT NOT NULL,
    used_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_property ON quota_slots (property, used_at);
CREATE INDEX IF NOT EXISTS idx_slots_account  ON quota_slots (account, used_at);

CREATE TABLE IF NOT EXISTS inspection_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    property  TEXT NOT NULL,
    called_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inspection_property ON inspection_calls (property, called_at);
"""


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Apply _ADDED_COLUMNS to tables that do not have them yet.

    Reads the live table shape from PRAGMA table_info rather than trying the
    ALTER and swallowing the error: "duplicate column name" is not reliably
    distinguishable from a real failure by message, and catching it broadly
    would hide a genuinely broken schema behind a successful open.
    """
    for table, column, declaration in _ADDED_COLUMNS:
        present = {row["name"] for row in
                   conn.execute(f"PRAGMA table_info({table})")}
        if not present or column in present:
            # An absent table is not this function's business: _SCHEMA has
            # already run, so a missing table means something is wrong that
            # an ALTER would only obscure.
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        log.info("added column %s.%s to an existing database", table, column)


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
    _add_missing_columns(conn)

    # Advance the stamp only when it is behind SCHEMA_VERSION; never touch it
    # otherwise. _SCHEMA above is all CREATE ... IF NOT EXISTS, and
    # _add_missing_columns() covers what that cannot (a new column on an
    # existing table), so between them an old database is brought up to date
    # before we get here. This block only updates the version label that
    # describes what just happened:
    #   - no stamp yet -> this is a brand-new database, stamp SCHEMA_VERSION.
    #   - stamp is not a parseable integer -> leave it alone. Refusing to
    #     open the database over a corrupt meta row would be a worse outcome
    #     than an inaccurate label; this matches the original behaviour,
    #     which never parsed the value at all.
    #   - stamp < SCHEMA_VERSION -> an older gsc-mcp wrote this database, and
    #     the additive schema now brings it up to date, so the stamp follows.
    #   - stamp >= SCHEMA_VERSION -> leave it alone. Unconditionally
    #     overwriting on every open would let a v1 database silently claim to
    #     be v2 with no migration having run (the defect Plan 1 fixed).
    #     Pulling a stamp DOWN would be the same mistake in the other
    #     direction: a database written by a newer gsc-mcp must stay
    #     detectable as newer, so a later health check can refuse to operate
    #     on it instead of being told it is safely current.
    existing = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    else:
        try:
            stamped = int(existing["value"])
        except ValueError:
            stamped = None
        if stamped is not None and stamped < SCHEMA_VERSION:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
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

    Re-entrancy is connection-aware, not task-aware — a hazard once anything
    async is built on this. Two concurrent asyncio tasks sharing a connection
    would see each other's open transaction and nest silently: the inner
    RELEASE only folds its savepoint into the outer transaction, so work the
    caller believes is committed is not durable, and is lost outright if the
    other task rolls back. Without re-entrancy that interleaving failed
    loudly. A connection must therefore have a single owner: never share one
    across concurrent tasks; give each its own via connect() or session().
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
        # A failing RELEASE would leave the savepoint on the stack and the
        # transaction open, so later tx calls nest against a dead savepoint.
        try:
            conn.execute(f"RELEASE {name}")
        except sqlite3.Error:
            _cleanup(conn, f"ROLLBACK TO {name}")
            _cleanup(conn, f"RELEASE {name}")
            raise
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        _cleanup(conn, "ROLLBACK")
        raise
    # A failing COMMIT leaves in_transaction True, so the next tx() would
    # silently become a SAVEPOINT whose RELEASE commits nothing.
    try:
        conn.execute("COMMIT")
    except sqlite3.Error:
        _cleanup(conn, "ROLLBACK")
        raise


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
            "sitemaps": _decode(row["sitemaps"], [], row["property"],
                                "sitemaps"),
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
               error: str | None = None,
               stop_reason: str | None = None) -> None:
    """Patch a job row. Only the fields passed are touched.

    stop_reason is WHY a run ended early -- "quota_exceeded", "no_quota",
    "stopped_by_user" -- as opposed to `error`, which means the worker itself
    died. A run that stops at the gate with "no_quota" records no attempt at
    all, so without this column that cause is unrecoverable from the row and
    a caller can only guess from a results list that is empty for two
    completely different reasons.
    """
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
        else:
            # Moving back to a live state: a previous run's completion time,
            # error and stop reason are stale by definition, and a consumer
            # treating finished_at as "done" would misread the resumed job.
            # stop_reason is cleared with them for the same reason -- a
            # resumed run that then finishes cleanly must not still be
            # advertising why the last one gave up.
            #
            # Each clear is skipped when the caller also passed that field, so
            # one column never gets two assignments in a single SET. SQLite's
            # behaviour for that is not worth relying on, and "live state plus
            # an explicit reason" is a contradiction whichever way it resolved.
            assignments.append("finished_at=NULL")
            if error is None:
                assignments.append("error=NULL")
            if stop_reason is None:
                assignments.append("stop_reason=NULL")
    if progress is not None:
        assignments.append("progress=?")
        values.append(json.dumps(progress))
    if error is not None:
        assignments.append("error=?")
        values.append(error)
    if stop_reason is not None:
        assignments.append("stop_reason=?")
        values.append(stop_reason)
    if not assignments:
        return

    values.append(job_id)
    with tx(conn):
        cursor = conn.execute(
            f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", values
        )
        if cursor.rowcount == 0:
            raise KeyError(f"no job with id {job_id}")


def list_jobs(conn: sqlite3.Connection, state: str | None = None) -> list[dict]:
    if state is None:
        rows = conn.execute("SELECT * FROM jobs ORDER BY started_at").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE state=? ORDER BY started_at", (state,)
        ).fetchall()
    return [_job_row(row) for row in rows]


def _decode(raw: str | None, fallback, record_id: str, field: str):
    """Decode a JSON column, falling back rather than failing the whole read.

    One corrupt row must not take out the listing it appears in. record_id is
    whatever identifies the row to an operator — a job id, a site property —
    so the message stays accurate wherever this is reused.
    """
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("%s has unreadable %s; ignoring it", record_id, field)
        return fallback


def _job_row(row: sqlite3.Row) -> dict:
    job = dict(row)
    job["params"] = _decode(job["params"], {}, job["id"], "params")
    job["progress"] = _decode(job["progress"], None, job["id"], "progress")
    return job


#: The two states that only a live worker inside THIS process can move on.
#: A row is created pending and its worker flips it to running moments
#: later; a crash in either state leaves a row nothing will ever advance.
#: Every other state is settled and must not be touched.
UNSETTLED_JOB_STATES = ("pending", "running")


def reconcile_jobs(conn: sqlite3.Connection, *,
                   error: str = "interrupted by restart") -> int:
    """Fail jobs that a crash or restart left unsettled.

    A pending or running job implies a live worker inside this process.
    After a restart there is none, so such a row is orphaned and will never
    progress. Call once at startup, before accepting new work.

    Returns how many were reconciled.
    """
    orphans = [job for state in UNSETTLED_JOB_STATES
               for job in list_jobs(conn, state=state)]
    for job in orphans:
        update_job(conn, job["id"], state="failed", error=error)
    if orphans:
        log.warning("reconciled %d job(s) left unsettled by a restart",
                    len(orphans))
    return len(orphans)
