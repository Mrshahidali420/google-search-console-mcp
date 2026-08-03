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
from gsc_mcp import deps, shipped_client


@pytest.fixture(autouse=True)
def _source_checkout(monkeypatch):
    """Run every test as if no OAuth client were baked into the build.

    src/gsc_mcp/_embedded.py is gitignored and exists only in a release
    build — or on the machine of whoever generated one locally. Without
    this, the suite's result depends on whether that file happens to be
    present: every test that asserts "no client is configured" passes on
    CI and fails on that machine, having tested nothing about the code.

    The cached client downloaded by gsc_setup is blanked for the same
    reason and it is the sharper hazard of the two: it lives under
    GSC_MCP_HOME, so any test that does NOT use the `home` fixture would
    read the developer's REAL config directory. Every "no client is
    configured" assertion would then pass on CI and fail on the machine of
    anyone who has ever run setup — the failure landing in tests that never
    mentioned a client.

    Pinning the empty state here rather than in each test keeps that
    coupling in one place, and keeps the default the state almost every
    test wants. The handful that need a client set one explicitly; their
    monkeypatching runs after this and wins.
    """
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "", raising=False)
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "", raising=False)
    monkeypatch.setattr(deps, "_cached_client", lambda: ("", ""))


@pytest.fixture(autouse=True)
def _no_client_download(monkeypatch):
    """Fail loudly if a test reaches the network for the shipped client.

    gsc_setup downloads the OAuth client when it finds none, so the moment
    that was added, every existing "no client configured" test began making
    a real request to GitHub — and still PASSED, because a 404 produces the
    same next step as being offline. A green suite that silently depends on
    the network, and on a release asset existing, is worse than a red one.

    An AssertionError rather than a canned failure response: a test that
    wants the fetch to fail should say so by installing that failure
    itself. This one only catches the tests that never meant to fetch at
    all. Tests that do patch requests.get override this.
    """
    def forbidden(*args, **kwargs):
        raise AssertionError(
            "this test reached the network for the shipped OAuth client; "
            "patch shipped_client.requests.get or configure a client"
        )

    monkeypatch.setattr(shipped_client.requests, "get", forbidden)


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
