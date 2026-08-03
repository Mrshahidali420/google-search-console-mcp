"""Localhost WebSocket bridge to the browser extension that drives the user's
real, logged-in Chromium profile.

Protocol (JSON text frames):

    client -> server   {"type":"pair_request","extension_id":...}   (first frame)
    server -> client   {"type":"pair_ok","token":...} | {"type":"pair_denied","reason":...}
    client -> server   {"type":"hello","token":...,"version":...}
    server -> client   {"type":"hello_ok"} | {"type":"hello_denied"}
    server -> client   {"type":"submit","id":...,"property":...,"url":...,"authuser":...}
    client -> server   {"type":"result","id":...,"outcome":...,"detail":...?}
    client -> server   {"type":"progress","id":...,"stage":...}   (informational)
    client -> server   {"type":"cancel"}                          (abort the run)
    either             {"type":"ping"} / {"type":"pong"}

Security: bound to 127.0.0.1 only. The first frame must be a hello carrying
the shared token, or a pair_request that is verified against the browser
profile before the token is handed over — see pairing.verify_pair_request.

This module is transport only. It knows nothing about quota, the database,
or what an outcome means; that is submit.py's job.

This is the pure-function head only: the token store, the outcome
vocabulary, and the frame helpers. No server, no threads, no sockets — those
arrive in a later task alongside the ``websockets`` dependency.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from . import paths, runlog

log = runlog.get(__name__)

# The EXACT result vocabulary submit.py consumes. Anything else off the wire
# is coerced to "error" — the bridge never invents a status. "skipped" is in
# here because content.js emits it (probe-only runs, an aborted job); the
# private toolkit's copy of this set omits it, which turned a legitimate skip
# into an error and charged a quota slot for a URL that was never submitted.
KNOWN_OUTCOMES = frozenset({
    "submitted", "already_indexed", "quota_exceeded", "timeout",
    "captcha", "captcha_after_click", "auth_required",
    "account_mismatch", "rate_limited", "skipped", "error",
})


def token_path() -> Path:
    return paths.ensure_config_dir() / "bridge_token.txt"


def load_or_create_token() -> str:
    """The shared secret the extension proves itself with. Created on first use.

    Never logged, never returned from a tool, never put in an exception
    message: it is the one credential that lets a local process drive the
    user's signed-in browser session.
    """
    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    log.info("created a new bridge token; the extension will fetch it on "
             "its next pair request")
    return token


def make_submit(cmd_id: str, gsc_property: str, url: str, authuser: str) -> str:
    return json.dumps({"type": "submit", "id": cmd_id, "property": gsc_property,
                       "url": url, "authuser": authuser})


def parse_message(raw: object) -> dict | None:
    """A frame we can act on, or None. Never raises on malformed input."""
    try:
        message = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict) or "type" not in message:
        return None
    return message


def map_outcome(outcome: object) -> str:
    return outcome if outcome in KNOWN_OUTCOMES else "error"
