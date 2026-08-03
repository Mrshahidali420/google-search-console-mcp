"""Fetch and parse sitemaps.

This is the first code in the package that fetches a URL the user's own
Search Console account points at and parses XML that arrives from it. Two
consequences shape everything below.

SAFETY. Remote XML is hostile input: entity expansion, external entities,
and unbounded bodies are all reachable from a sitemap URL. Parsing goes
through defusedxml with no stdlib fallback, bodies are read against a byte
ceiling, redirects are followed manually so every hop is re-validated, and
loopback and private addresses are refused outright.

HONESTY. The obvious failure mode of a sitemap reader is to return an
empty list and log a warning, which reads downstream as "this site has no
URLs" — a whole property's discovery vanishing into a log file. Every
failure here is returned as data, with a reason drawn from a closed
vocabulary so a reason can never be built by interpolation and can never
carry a host or a path into a tool result.
"""
from __future__ import annotations

import gzip
import ipaddress
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import requests
from defusedxml import ElementTree as DefusedET

from . import runlog

log = runlog.get(__name__)

MAX_DEPTH = 4
MAX_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT_S = 45.0
USER_AGENT = "gsc-mcp"

_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_GZIP_MAGIC = b"\x1f\x8b"

FAILURE_REASONS = frozenset({
    "bad_scheme",    # not http(s)
    "blocked_host",  # loopback, private, or link-local
    "fetch_failed",  # transport raised
    "http_error",    # non-2xx
    "too_large",     # body exceeded the byte ceiling
    "too_deep",      # index recursion hit the depth cap
    "unparseable",   # neither XML nor a plain-text URL list
})

_session = requests.Session()


class SitemapFailure(NamedTuple):
    """One sitemap that could not be read, and why.

    `reason` is always a member of FAILURE_REASONS. `url` is the sitemap
    the caller asked about — the user's own site, which they already know,
    and which gsc_list_sites already returns.
    """

    url: str
    reason: str


class SitemapResult(NamedTuple):
    urls: list[str]
    sitemaps_read: list[str]
    failures: list[SitemapFailure]


def fetch_urls(sitemap_url: str, session: requests.Session | None = None, *,
               max_depth: int = MAX_DEPTH, max_bytes: int = MAX_BYTES,
               timeout_s: float = FETCH_TIMEOUT_S) -> SitemapResult:
    """Every URL a sitemap lists, following index files.

    Depth-capped and cycle-guarded: a sitemap that points at itself, or at
    a sibling that points back, is read once. Order is preserved — the
    caller's `limit` is applied to this list, so a stable order makes a
    truncated answer reproducible.
    """
    client = session or _session
    urls: list[str] = []
    read: list[str] = []
    failures: list[SitemapFailure] = []
    seen: set[str] = set()
    seen_urls: set[str] = set()
    queue: list[tuple[str, int]] = [(sitemap_url, 0)]

    while queue:
        current, depth = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if depth >= max_depth:
            failures.append(SitemapFailure(current, "too_deep"))
            continue

        body, failure = _fetch(current, client, max_bytes, timeout_s)
        if failure is not None:
            failures.append(failure)
            continue
        read.append(current)

        children, found, parsed = _parse(body, current)
        if not parsed:
            failures.append(SitemapFailure(current, "unparseable"))
            continue
        queue.extend((child, depth + 1) for child in children)
        for url in found:
            if url not in seen_urls:
                seen_urls.add(url)
                urls.append(url)

    return SitemapResult(urls, read, failures)


def _fetch(url: str, client: requests.Session, max_bytes: int,
           timeout_s: float) -> tuple[bytes, SitemapFailure | None]:
    """Fetch one sitemap, re-validating the target at every redirect hop.

    Redirects are followed manually rather than by requests, because
    requests validates only what it is handed: a sitemap URL that
    redirects to 127.0.0.1 or a cloud metadata endpoint would otherwise be
    fetched with the scheme check already behind us.
    """
    target = url
    for _ in range(MAX_REDIRECTS + 1):
        blocked = _refuse(target)
        if blocked is not None:
            return b"", SitemapFailure(url, blocked)
        try:
            resp = client.get(target, headers={"User-Agent": USER_AGENT},
                              timeout=timeout_s, allow_redirects=False,
                              stream=True)
        except Exception as exc:  # noqa: BLE001 — type name only, see below
            # str(exc) on a transport error carries the resolved address and,
            # on Windows, a path out of the certificate store.
            log.info("sitemap fetch failed: %s", type(exc).__name__)
            return b"", SitemapFailure(url, "fetch_failed")

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            resp.close()
            if not location:
                return b"", SitemapFailure(url, "http_error")
            target = urljoin(target, location)
            continue

        if resp.status_code >= 400 or resp.status_code < 200:
            resp.close()
            return b"", SitemapFailure(url, "http_error")

        body, over = _read_capped(resp, max_bytes)
        if over:
            return b"", SitemapFailure(url, "too_large")
        return body, None

    return b"", SitemapFailure(url, "http_error")


def _read_capped(resp: object, max_bytes: int) -> tuple[bytes, bool]:
    """Read a response body, stopping one byte past the ceiling.

    Reading `resp.content` would pull an unbounded body fully into memory
    before any check could reject it.
    """
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return b"", True
            chunks.append(chunk)
    finally:
        close = getattr(resp, "close", None)
        if close is not None:
            close()
    return b"".join(chunks), False


def _refuse(url: str) -> str | None:
    """The reason this URL must not be fetched, or None.

    Literal loopback, private, and link-local addresses are refused. A
    hostname that RESOLVES to one is not caught here: doing so needs a
    resolve-then-connect-to-that-address client, which requests does not
    offer. Stated rather than implied so nobody reads this as full SSRF
    cover.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "bad_scheme"
    host = (parsed.hostname or "").strip("[]")
    if not host:
        return "bad_scheme"
    if host.lower() in ("localhost", "localhost.localdomain"):
        return "blocked_host"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if (address.is_loopback or address.is_private or address.is_link_local
            or address.is_reserved or address.is_multicast):
        return "blocked_host"
    return None


def _parse(body: bytes, url: str) -> tuple[list[str], list[str], bool]:
    """(child sitemaps, page URLs, parsed_ok) for one sitemap body.

    Handles the three shapes a sitemap actually arrives in: XML, gzipped
    XML (very common as .xml.gz, and often served without a gzip
    Content-Encoding header so requests does not decompress it), and the
    plain-text one-URL-per-line form sitemaps.org also permits.
    """
    payload = body
    if payload[:2] == _GZIP_MAGIC:
        try:
            payload = gzip.decompress(payload)
        except Exception as exc:  # noqa: BLE001 — type name only
            log.info("sitemap gunzip failed: %s", type(exc).__name__)
            return [], [], False

    try:
        root = DefusedET.fromstring(payload)
    except Exception as exc:  # noqa: BLE001 — type name only
        log.info("sitemap parse failed: %s", type(exc).__name__)
        plain = _plain_text(payload)
        if plain is None:
            return [], [], False
        return [], plain, True

    if root.tag == f"{_NS}sitemapindex":
        children = [_text(node) for node in root.findall(f"{_NS}sitemap/{_NS}loc")]
        return [c for c in children if _http(c)], [], True

    found = [_text(node) for node in root.findall(f"{_NS}url/{_NS}loc")]
    return [], [f for f in found if _http(f)], True


def _plain_text(payload: bytes) -> list[str] | None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = [line.strip() for line in text.splitlines()]
    urls = [line for line in lines if _http(line)]
    return urls or None


def _text(node: object) -> str:
    return (getattr(node, "text", "") or "").strip()


def _http(value: str) -> bool:
    return urlparse(value).scheme in ("http", "https")
