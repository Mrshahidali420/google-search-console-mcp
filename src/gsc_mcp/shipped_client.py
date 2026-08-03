"""The OAuth client gsc-mcp ships, fetched once and cached on disk.

WHY THIS EXISTS. Decision D1 says a user should never have to create a
Google Cloud project to use this server — they install it and click Allow.
A release wheel carries the client baked in (`deps._embedded_client`), but
someone who clones this public repository gets no client at all, because
the value cannot live in tracked source: a secret committed to git is
permanent even once deleted, and GitHub's scanner reports Google client
secrets to Google, which can revoke them. A revoked client would break
every user of every release at once, not just the person who pushed it.

So a source checkout downloads the client on first setup instead. Nothing
about that is a security improvement and it must not be sold as one: the
URL below is in public source and hands the client to anyone who asks for
it, exactly as unzipping a release wheel does. What it buys is
OPERATIONAL. The value can be rotated by replacing one release asset,
without a rebuild and without a new version; it can be withdrawn if it is
ever abused; and it never appears in this repository, so the scanner has
nothing to report. The security of an installed-app client rests on PKCE
(`gauth.consent_url` uses S256), which is unchanged either way.

WHY A DEDICATED RELEASE TAG, not `releases/latest/download/`. The asset
hangs off a permanent `client` tag that version releases never touch. With
`latest`, every future release would have to remember to re-upload the
client or first-run setup breaks for everyone — a trap that springs on the
one release somebody forgets. The two lifecycles are better kept apart:
cutting 0.2.0 does not involve this file, and rotating this file does not
involve a release.

WHEN THE NETWORK IS TOUCHED. Only from `gsc_setup`, and only when no
client was found in the environment, the build, or the cache. Never from
`deps.oauth_client`, which `deps.provider` calls on every single tool call
and `gsc_doctor` calls three times: a network round trip there would let a
DNS timeout hang any tool, and a flaky connection would present as a
broken install. Fetch once, cache, and every run afterwards is offline.

NEVER LOGGED. The client secret does not reach a log line at any level and
does not appear in an exception message. Failures are reported by a fixed
string chosen here, never by interpolating a response body — a captive
portal's HTML would otherwise land in the user's log.
"""
from __future__ import annotations

import json

import requests

from gsc_core import gauth, paths, runlog

log = runlog.get(__name__)

CLIENT_URL = (
    "https://github.com/Mrshahidali420/google-search-console-mcp"
    "/releases/download/client/client.json"
)

# A client.json is ~200 bytes. The cap is what stops a wrong URL — a login
# page, an error page, a redirect to something enormous — from being read
# into memory before it is rejected, so it is enforced WHILE reading rather
# than after.
_MAX_BYTES = 8192
_TIMEOUT_SECONDS = 15

# Google issues client ids ending in this. Checking it turns "the body was
# not what we expected" into a failure HERE, where the message can name the
# fix, instead of an opaque invalid_client rejection from Google in the
# middle of the consent flow, long after the bad value was cached.
_ID_SUFFIX = ".apps.googleusercontent.com"


class FetchFailed(RuntimeError):
    """The shipped client could not be downloaded or was not usable.

    Its message is written for a user to read — it is surfaced through
    `gsc_setup`'s next action — and therefore never carries a response
    body, a URL query, or any part of the credential.
    """


def cached() -> tuple[str, str]:
    """The client saved by a previous fetch, or ("", "") if there is none.

    A local read on the hot path, so it must never raise: a truncated or
    hand-edited cache degrades to "no client", which routes the user to
    setup and a re-fetch, rather than taking the server down at import.
    """
    try:
        raw = paths.client_path().read_text(encoding="utf-8")
    except OSError:
        return "", ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("the cached OAuth client is not valid JSON; ignoring it")
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    client_id = payload.get("client_id")
    client_secret = payload.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        return "", ""
    return client_id, client_secret


def fetch_and_cache() -> tuple[str, str]:
    """Download the shipped client, cache it, and return it.

    Raises FetchFailed for every failure mode — network, HTTP status,
    oversized body, malformed JSON, missing or implausible fields — so the
    caller has one exception to handle and one message to show.
    """
    payload = _download()
    client_id, client_secret = _validate(payload)
    _cache(client_id, client_secret)
    return client_id, client_secret


def _download() -> object:
    try:
        response = requests.get(CLIENT_URL, timeout=_TIMEOUT_SECONDS,
                                stream=True)
    except requests.RequestException as exc:
        # The exception's own message can embed the URL and, on some
        # adapters, proxy credentials from the environment. Type name only.
        log.warning("could not reach the client URL: %s", type(exc).__name__)
        raise FetchFailed(
            "could not download the bundled OAuth client; check the network "
            "connection and try again"
        ) from None

    with response:
        # GitHub redirects release-asset downloads to object storage, so
        # redirects must be followed — but every hop has to stay on HTTPS.
        # requests will happily follow https -> http, which would put the
        # client secret on the wire in the clear.
        for hop in (*response.history, response):
            if not hop.url.lower().startswith("https://"):
                raise FetchFailed(
                    "the bundled OAuth client was served over an insecure "
                    "connection and was discarded"
                )

        if response.status_code != 200:
            raise FetchFailed(
                f"the bundled OAuth client is not available "
                f"(HTTP {response.status_code}); set GSC_MCP_CLIENT_ID and "
                f"GSC_MCP_CLIENT_SECRET to use your own instead"
            )

        body = b""
        for chunk in response.iter_content(chunk_size=1024):
            body += chunk
            if len(body) > _MAX_BYTES:
                raise FetchFailed(
                    "the bundled OAuth client response was far larger than "
                    "expected and was discarded"
                )

    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FetchFailed(
            "the bundled OAuth client response was not valid JSON; if you "
            "are behind a proxy or captive portal, try again once online"
        ) from None


def _validate(payload: object) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise FetchFailed("the bundled OAuth client response had the wrong shape")

    client_id = payload.get("client_id")
    client_secret = payload.get("client_secret")
    if not isinstance(client_id, str) or not isinstance(client_secret, str):
        raise FetchFailed(
            "the bundled OAuth client response was missing client_id or "
            "client_secret"
        )
    if not client_id or not client_secret:
        raise FetchFailed(
            "the bundled OAuth client response contained an empty client_id "
            "or client_secret"
        )
    if not client_id.endswith(_ID_SUFFIX):
        # Deliberately does not echo the value: it is short enough that a
        # wrong one is usually a page fragment, and quoting it would put
        # arbitrary fetched text into the user's client.
        raise FetchFailed(
            "the bundled OAuth client response did not contain a Google "
            "client id"
        )
    return client_id, client_secret


def _cache(client_id: str, client_secret: str) -> None:
    """Save the client for later runs. A cache write failure is not fatal.

    The caller already holds a usable client, and refusing to proceed
    because a read-only home directory could not be written would turn a
    slow setup into a broken one. The cost of not caching is one more
    download next time.
    """
    try:
        gauth.write_private_json(
            {"client_id": client_id, "client_secret": client_secret},
            paths.client_path(),
        )
    except OSError as exc:
        # OSError's message carries the absolute path, which holds the
        # operator's account name. Type name only, as everywhere else.
        log.warning("could not cache the OAuth client: %s", type(exc).__name__)
