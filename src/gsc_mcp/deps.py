"""Per-tool-call dependencies for the MCP server.

Every gsc_* tool builds its own TokenProvider and opens its own database
connection through this module rather than sharing a module-level instance
of either. That is not a style preference: store.tx()'s re-entrancy is
CONNECTION-scoped, not task-scoped (see its docstring), so two concurrent
tool calls sharing one connection would silently nest transactions and the
inner RELEASE would not durably commit. Giving each call its own connection
via connection() below is what keeps that hazard out of the picture.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from gsc_core import gauth, store

CLIENT_ID_ENV = "GSC_MCP_CLIENT_ID"
CLIENT_SECRET_ENV = "GSC_MCP_CLIENT_SECRET"

# Decision D1: gsc-mcp ships its own Google OAuth client, embedded in the
# distributed package so a user never has to create one in the Cloud
# Console themselves. That Cloud app does not exist yet, and a placeholder
# secret must never be a real one committed to a public repository — so
# both constants are empty until the verified app exists. Populating them
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


def provider() -> gauth.TokenProvider:
    """A fresh TokenProvider for one tool call.

    Not cached or shared across calls — TokenProvider itself is cheap to
    construct (it just remembers the client id/secret/path) and its own
    single-flight lock is per-instance, so nothing is gained by reusing one
    across unrelated tool invocations, while a shared instance would be one
    more piece of state a concurrent server would have to reason about.
    """
    client_id, client_secret = oauth_client()
    return gauth.TokenProvider(client_id, client_secret)


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """One database connection, opened and closed within a single tool call.

    A thin wrapper over store.session() — kept here, rather than every tool
    importing store directly, so "one connection per call, no module-level
    handle" has exactly one place it is implemented and enforced.
    """
    with store.session() as conn:
        yield conn
