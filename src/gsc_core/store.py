"""The single SQLite store.

Replaces what used to be three things: quota_ledger.json, journal.jsonl and a
Google Sheet. One file means one locking story, and it means a slot and the
submission that spent it can be written in the same transaction — which the
old JSON-plus-filelock arrangement could not guarantee.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
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
CREATE INDEX IF NOT EXISTS idx_urls_property ON urls (property);
CREATE INDEX IF NOT EXISTS idx_urls_checked  ON urls (checked_at);

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
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction. BEGIN IMMEDIATE so concurrent writers queue rather
    than fail late with SQLITE_BUSY partway through."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
