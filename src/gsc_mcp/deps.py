"""Dependencies for the MCP server's tools. The two are shared DIFFERENTLY,
and each way round is load-bearing.

A DATABASE CONNECTION IS PER CALL. store.tx()'s re-entrancy is
CONNECTION-scoped, not task-scoped (see its docstring), so two concurrent
tool calls sharing one connection would silently nest transactions and the
inner RELEASE would not durably commit. connection() below gives each call
its own, which keeps that hazard out of the picture.

A TOKEN PROVIDER IS SHARED. TokenProvider's refresh lock is per-instance,
and the race it exists to stop — concurrent refreshes each spending a
refresh token Google rotates on use — happens BETWEEN tool calls. A
per-call provider would leave that lock guarding nothing at exactly the
point the threat lives. provider() below therefore caches, keyed by the
credentials and token path it was built from.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from gsc_core import gauth, paths, store

CLIENT_ID_ENV = "GSC_MCP_CLIENT_ID"
CLIENT_SECRET_ENV = "GSC_MCP_CLIENT_SECRET"

# Decision D1: gsc-mcp ships its own Google OAuth client, embedded in the
# distributed package so a user never has to create one in the Cloud
# Console themselves. That Cloud app does not exist yet, and a placeholder
# secret must never be a real one committed to a public repository — so
# both constants are empty until the published app exists. Populating them
# is a one-line change at that point; the environment variables below let
# development and internal use proceed in the meantime without it.
EMBEDDED_CLIENT_ID = ""
EMBEDDED_CLIENT_SECRET = ""


class NotConfigured(RuntimeError):
    """No OAuth client is available — neither environment nor embedded.

    Distinct from gauth.AuthRequired: that means "no token yet, go sign
    in"; this means "there is no client to sign in WITH" — a setup problem
    one step earlier, with a different fix.
    """


def oauth_client() -> tuple[str, str]:
    """The (client_id, client_secret) to authenticate with, env-first.

    The environment variables always win when both are set, so a developer
    or a self-hosted deployment can override the embedded client without
    patching source. Raises NotConfigured when the result would be empty —
    better than handing gauth an empty client id that fails confusingly
    much later, mid-OAuth-flow.
    """
    client_id = os.environ.get(CLIENT_ID_ENV) or EMBEDDED_CLIENT_ID
    client_secret = os.environ.get(CLIENT_SECRET_ENV) or EMBEDDED_CLIENT_SECRET
    if not client_id or not client_secret:
        raise NotConfigured(
            "no OAuth client configured; set GSC_MCP_CLIENT_ID and "
            "GSC_MCP_CLIENT_SECRET, or install a release build with an "
            "embedded client"
        )
    return client_id, client_secret


_provider_lock = threading.Lock()
_providers: dict[tuple[str, str, str], gauth.TokenProvider] = {}


def provider() -> gauth.TokenProvider:
    """The TokenProvider for this client and token file, SHARED across calls.

    Unlike connection(), which must be per-call, this must NOT be: it is
    the opposite hazard. TokenProvider's single-flight lock is
    per-instance, and the race it exists to stop lives between concurrent
    TOOL CALLS — Google rotates the refresh token on use, so N providers
    refreshing at once each spend the same refresh token and the losers
    persist one Google has already retired, leaving the stored credential
    dead and the user re-authorising. Constructing one per call left that
    lock guarding nothing at exactly the point the threat exists. Proven:
    one shared provider under five concurrent access_token() calls issues
    one refresh; five fresh ones issue five.

    Cached by (client_id, client_secret, token path) rather than in a
    plain module-level singleton, because oauth_client() and
    paths.token_path() both read environment variables the test suite
    monkeypatches — a singleton would hand a test the provider a previous
    test built against a different client or a different token file. Keying
    on what the provider was actually built from means a changed
    environment gets a new provider and an unchanged one gets the same
    instance, which is the whole point.

    The cache is bounded by the number of distinct credential/path
    combinations a process sees: exactly one in production.
    """
    client_id, client_secret = oauth_client()
    key = (client_id, client_secret, str(paths.token_path()))
    with _provider_lock:
        existing = _providers.get(key)
        if existing is None:
            existing = gauth.TokenProvider(client_id, client_secret)
            _providers[key] = existing
        return existing


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """One database connection, opened and closed within a single tool call.

    A thin wrapper over store.session() — kept here, rather than every tool
    importing store directly, so "one connection per call, no module-level
    handle" has exactly one place it is implemented and enforced.
    """
    with store.session() as conn:
        yield conn
