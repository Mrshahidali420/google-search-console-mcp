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


def test_connect_creates_missing_parent_directory(tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "state.db"
    conn = store.connect(nested)
    assert nested.exists()
    conn.close()


def test_connect_without_path_uses_configured_location(monkeypatch, tmp_path):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    conn = store.connect()
    assert (tmp_path / "state.db").exists()
    conn.close()


def test_schema_version_is_not_overwritten_on_reopen(tmp_path):
    db = tmp_path / "state.db"
    first = store.connect(db)
    first.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    first.close()

    second = store.connect(db)
    assert store.schema_version(second) == 99
    second.close()


def test_busy_timeout_matches_the_connect_timeout(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    conn.close()


def test_session_closes_the_connection(tmp_path):
    with store.session(tmp_path / "state.db") as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_base_exception_inside_tx_does_not_leave_it_open(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    with pytest.raises(KeyboardInterrupt):
        with store.tx(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('x', '1')")
            raise KeyboardInterrupt

    assert conn.in_transaction is False
    assert conn.execute("SELECT value FROM meta WHERE key='x'").fetchone() is None
    # The connection must still be usable — a leaked transaction would make
    # this raise "cannot start a transaction within a transaction".
    with store.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('y', '2')")
    assert conn.execute("SELECT value FROM meta WHERE key='y'").fetchone()["value"] == "2"


def test_nested_tx_commits_as_one_unit(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    with store.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('outer', '1')")
        with store.tx(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('inner', '2')")
    rows = conn.execute(
        "SELECT key FROM meta WHERE key IN ('outer','inner')"
    ).fetchall()
    assert {row["key"] for row in rows} == {"outer", "inner"}


def test_inner_tx_failure_rolls_back_only_the_savepoint(tmp_path):
    conn = store.connect(tmp_path / "state.db")
    with store.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('kept', '1')")
        with pytest.raises(ValueError):
            with store.tx(conn):
                conn.execute("INSERT INTO meta (key, value) VALUES ('dropped', '2')")
                raise ValueError("inner fails")
    assert conn.execute("SELECT value FROM meta WHERE key='kept'").fetchone()["value"] == "1"
    assert conn.execute("SELECT value FROM meta WHERE key='dropped'").fetchone() is None
