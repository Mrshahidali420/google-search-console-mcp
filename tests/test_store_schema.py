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


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_a_database_predating_stop_reason_gains_the_column(tmp_path):
    """CREATE TABLE IF NOT EXISTS gives an old DB new tables, never new
    columns. Without the ALTER pass, every write touching stop_reason on a
    pre-existing database fails with "no such column"."""
    db = tmp_path / "state.db"
    old = sqlite3.connect(db)
    old.executescript(
        "CREATE TABLE jobs (id TEXT PRIMARY KEY, params TEXT, state TEXT,"
        " progress TEXT, started_at TEXT, finished_at TEXT, error TEXT)"
    )
    old.execute("INSERT INTO jobs (id, state) VALUES ('old-job', 'running')")
    old.commit()
    old.close()

    conn = store.connect(db)
    assert "stop_reason" in _columns(conn, "jobs")
    # The migration adds, it does not rebuild: the existing row survives.
    store.update_job(conn, "old-job", state="stopped_throttled",
                     stop_reason="no_quota")
    assert store.get_job(conn, "old-job")["stop_reason"] == "no_quota"


def test_the_column_migration_runs_only_once(tmp_path):
    db = tmp_path / "state.db"
    store.connect(db).close()
    conn = store.connect(db)
    assert "stop_reason" in _columns(conn, "jobs")


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


class _CommitFails:
    """A connection proxy whose COMMIT raises, forwarding everything else.

    sqlite3.Connection.execute is a read-only C slot, so monkeypatching the
    method on a real connection is not possible; a forwarding proxy is the
    only way to inject the disk error tx() has to survive. tx() touches only
    .in_transaction and .execute, both of which reach the real connection.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        if sql == "COMMIT":
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)


def test_failed_commit_does_not_poison_the_connection(tmp_path):
    """A failed COMMIT must roll back, not leave the transaction open — the
    next tx() would otherwise become a SAVEPOINT that commits nothing.
    """
    conn = store.connect(tmp_path / "state.db")
    with pytest.raises(sqlite3.OperationalError):
        with store.tx(_CommitFails(conn)):
            conn.execute("INSERT INTO meta (key, value) VALUES ('a', '1')")

    assert conn.in_transaction is False
    assert conn.execute("SELECT value FROM meta WHERE key='a'").fetchone() is None

    with store.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('b', '2')")
    assert conn.execute("SELECT value FROM meta WHERE key='b'").fetchone()["value"] == "2"


def _traced(conn) -> list[str]:
    """Record every statement the connection executes."""
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    return statements


def test_a_successful_nested_tx_releases_its_savepoint(tmp_path):
    """The successful RELEASE was covered by nothing: deleting it leaves the
    savepoint on the stack, but the OUTER COMMIT still commits everything, so
    every data-level assertion stays green while savepoints accumulate. Only
    the statements themselves show it, so this asserts on those.

    api._reserve nests inside batch writes, so a leak here is not theoretical
    -- one savepoint per property per call, held for the life of the
    transaction.
    """
    conn = store.connect(tmp_path / "state.db")
    statements = _traced(conn)
    with store.tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES ('outer', '1')")
        with store.tx(conn):
            conn.execute("INSERT INTO meta (key, value) VALUES ('inner', '2')")
    conn.set_trace_callback(None)

    savepoints = [s for s in statements if s.startswith("SAVEPOINT ")]
    releases = [s for s in statements if s.startswith("RELEASE ")]
    assert len(savepoints) == 1
    assert len(releases) == 1
    # Same savepoint, and released -- not merely some RELEASE somewhere.
    assert releases[0].split()[1] == savepoints[0].split()[1]
    # The success path must not touch the rollback statements at all.
    assert not [s for s in statements if s.startswith("ROLLBACK")]
    # And the release happened INSIDE the outer transaction, before COMMIT.
    assert statements.index(releases[0]) < statements.index("COMMIT")


def test_repeated_nested_txs_do_not_stack_savepoints(tmp_path):
    """Every inner block opens and closes its own savepoint, so N nested
    blocks produce N savepoints and N releases -- never N and none."""
    conn = store.connect(tmp_path / "state.db")
    statements = _traced(conn)
    with store.tx(conn):
        for n in range(3):
            with store.tx(conn):
                conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)",
                             (f"k{n}", str(n)))
    conn.set_trace_callback(None)

    assert len([s for s in statements if s.startswith("SAVEPOINT ")]) == 3
    assert len([s for s in statements if s.startswith("RELEASE ")]) == 3
    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM meta WHERE key LIKE 'k%'").fetchone()["n"] == 3
