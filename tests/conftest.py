"""Fixtures shared by every test that needs a throwaway GSC_MCP_HOME.

These began as local copies in one test file, then a second needed them.
The third copy is where two "isolated" homes quietly stop being the same
thing — one file gains a fixture that also seeds a site, say — so they live
here now.

`home` is the isolation itself: config, database and log file all hang off
GSC_MCP_HOME, so pointing it at tmp_path is what keeps a test off the
developer's real store. `store_conn` is a connection into that database,
opened and closed around the test.
"""
from __future__ import annotations

import pytest

from gsc_core import store


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point config, database and logs at a throwaway directory."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def store_conn(home):
    """A connection to the throwaway database, closed with the test."""
    with store.session() as conn:
        yield conn
