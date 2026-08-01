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
             datetime.now(UTC).isoformat()),
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
    """Insert or update a URL. first_seen is written once and never changed."""
    with tx(conn):
        conn.execute(
            "INSERT INTO urls (url, property, status, reason, first_seen, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  property=excluded.property, status=excluded.status, "
            "  reason=excluded.reason, checked_at=excluded.checked_at",
            (url, property, status, reason,
             datetime.now(UTC).isoformat(), checked_at),
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
    cutoff = (moment - timedelta(days=ttl_days)).isoformat()
    rows = conn.execute(
        "SELECT url FROM urls WHERE property=? "
        "AND (checked_at IS NULL OR checked_at < ?) ORDER BY url",
        (property, cutoff),
    ).fetchall()
    return [row["url"] for row in rows]


def mark_submitted(conn: sqlite3.Connection, url: str, when: str) -> None:
    with tx(conn):
        conn.execute("UPDATE urls SET last_submitted=? WHERE url=?", (when, url))
