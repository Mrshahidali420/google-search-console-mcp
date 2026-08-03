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
from gsc_mcp import deps


@pytest.fixture(autouse=True)
def _source_checkout(monkeypatch):
    """Run every test as if no OAuth client were baked into the build.

    src/gsc_mcp/_embedded.py is gitignored and exists only in a release
    build — or on the machine of whoever generated one locally. Without
    this, the suite's result depends on whether that file happens to be
    present: every test that asserts "no client is configured" passes on
    CI and fails on that machine, having tested nothing about the code.

    Pinning the empty state here rather than in each test keeps that
    coupling in one place, and keeps the default the state almost every
    test wants. The handful that need a client set one explicitly; their
    monkeypatching runs after this and wins.
    """
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "", raising=False)


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
