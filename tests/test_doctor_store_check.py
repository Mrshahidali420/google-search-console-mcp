"""`gsc_doctor`'s store check, and the advice it gives about a mismatch.

The two directions are not the same problem and must not get the same
answer. A database OLDER than this build failed to migrate on open and
rebuilding it is a reasonable last resort. A database NEWER than this
build was written by a newer gsc-mcp, which store.connect() deliberately
leaves alone rather than downgrading — and telling the operator to delete
it would throw away the quota ledger, which is the one thing in there that
cannot be re-derived from Search Console.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from gsc_mcp import server


@pytest.fixture
def stamped(monkeypatch):
    """Report the store as carrying a given schema version."""
    def _stamp(version: int) -> None:
        @contextmanager
        def _connection():
            yield object()

        monkeypatch.setattr(server.deps, "connection", _connection)
        monkeypatch.setattr(server.store, "schema_version",
                            lambda _conn: version)
    return _stamp


def test_the_current_version_passes(stamped):
    stamped(server.store.SCHEMA_VERSION)
    assert server._check_store()["ok"] is True


def test_a_newer_database_is_not_answered_with_delete_it(stamped):
    """The regression this exists for: one migration ahead used to read as
    "delete the database to rebuild", over a version stamp alone."""
    stamped(server.store.SCHEMA_VERSION + 1)

    check = server._check_store()

    assert check["ok"] is False
    assert "do not delete" in check["fix"].lower()


def test_a_newer_database_is_told_to_update_instead(stamped):
    stamped(server.store.SCHEMA_VERSION + 1)
    fix = server._check_store()["fix"].lower()
    assert "update" in fix or "restart" in fix


def test_an_older_database_may_still_be_rebuilt(stamped):
    """Older means the migration on open did not run, and there is nothing
    newer to update to."""
    stamped(server.store.SCHEMA_VERSION - 1)

    check = server._check_store()

    assert check["ok"] is False
    assert "delete" in check["fix"].lower()
