import sqlite3

import pytest

from gsc_core import store


def _tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_connect_creates_all_tables(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    assert _tables(conn) >= {
        "meta", "sites", "urls", "submissions", "jobs", "quota_slots",
    }


def test_connect_records_schema_version(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    assert store.schema_version(conn) == store.SCHEMA_VERSION


def test_connect_is_idempotent(tmp_path):
    db = tmp_path / "state.db"
    first = store.connect(db)
    first.execute(
        "INSERT INTO meta (key, value) VALUES ('probe', 'kept')"
    )
    first.commit()
    first.close()

    second = store.connect(db)
    row = second.execute(
        "SELECT value FROM meta WHERE key='probe'"
    ).fetchone()
    assert row["value"] == "kept"


def test_rows_are_mappings(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    conn.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
    row = conn.execute("SELECT * FROM meta WHERE key='k'").fetchone()
    assert row["value"] == "v"


def test_tx_commits_on_success(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    with store.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")
    row = conn.execute("SELECT value FROM meta WHERE key='a'").fetchone()
    assert row["value"] == "1"


def test_tx_rolls_back_on_exception(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    with pytest.raises(ValueError):
        with store.tx(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('b', '2')")
            raise ValueError("boom")
    row = conn.execute("SELECT value FROM meta WHERE key='b'").fetchone()
    assert row is None


def test_wal_mode_enabled(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_enabled(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert enabled == 1
