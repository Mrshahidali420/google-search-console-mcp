from __future__ import annotations

import gzip

import pytest

from gsc_core import sitemaps

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
</sitemapindex>"""

LEAF = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/a</loc></url>
  <url><loc>https://example.com/b</loc></url>
</urlset>"""


class _Resp:
    def __init__(self, body: bytes, status: int = 200,
                 headers: dict[str, str] | None = None) -> None:
        self.content = body
        self.status_code = status
        self.headers = headers or {}
        self.url = ""

    def iter_content(self, chunk_size: int = 8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self) -> None:
        pass


class _FakeSession:
    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.asked: list[str] = []

    def get(self, url: str, **kwargs: object) -> _Resp:
        self.asked.append(url)
        try:
            resp = self.routes[url]
        except KeyError:
            raise AssertionError(f"unexpected fetch: {url}") from None
        resp.url = url
        return resp


def test_a_sitemap_index_is_followed_into_its_children():
    session = _FakeSession({
        "https://example.com/sitemap.xml": _Resp(INDEX.encode()),
        "https://example.com/sitemap-1.xml": _Resp(LEAF.encode()),
    })

    result = sitemaps.fetch_urls("https://example.com/sitemap.xml", session)

    assert result.urls == ["https://example.com/a", "https://example.com/b"]
    assert result.sitemaps_read == ["https://example.com/sitemap.xml",
                                    "https://example.com/sitemap-1.xml"]
    assert result.failures == []


def test_a_sitemap_that_points_at_itself_is_read_once():
    body = """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/loop.xml</loc></sitemap>
</sitemapindex>"""
    session = _FakeSession({"https://example.com/loop.xml": _Resp(body.encode())})

    result = sitemaps.fetch_urls("https://example.com/loop.xml", session)

    assert session.asked == ["https://example.com/loop.xml"]
    assert result.urls == []


def test_a_gzipped_sitemap_is_decompressed():
    session = _FakeSession({
        "https://example.com/sitemap.xml.gz": _Resp(gzip.compress(LEAF.encode())),
    })

    result = sitemaps.fetch_urls("https://example.com/sitemap.xml.gz", session)

    assert result.urls == ["https://example.com/a", "https://example.com/b"]


def test_a_plain_text_sitemap_is_parsed_line_by_line():
    body = b"https://example.com/a\nhttps://example.com/b\n\n# note\n"
    session = _FakeSession({"https://example.com/urls.txt": _Resp(body)})

    result = sitemaps.fetch_urls("https://example.com/urls.txt", session)

    assert result.urls == ["https://example.com/a", "https://example.com/b"]


def test_an_oversized_body_is_a_reported_failure_not_an_empty_sitemap():
    session = _FakeSession({
        "https://example.com/big.xml": _Resp(b"<urlset>" + b"x" * 5000),
    })

    result = sitemaps.fetch_urls("https://example.com/big.xml", session,
                                 max_bytes=1000)

    assert result.urls == []
    assert [f.reason for f in result.failures] == ["too_large"]
    assert result.failures[0].url == "https://example.com/big.xml"


def test_unparseable_xml_is_a_reported_failure():
    session = _FakeSession({"https://example.com/bad.xml": _Resp(b"<urlset><ur")})

    result = sitemaps.fetch_urls("https://example.com/bad.xml", session)

    assert [f.reason for f in result.failures] == ["unparseable"]


def test_a_transport_error_is_a_reported_failure():
    class _Boom:
        def get(self, url: str, **kwargs: object):
            raise OSError("connection reset by C:\\Users\\someone\\x")

    result = sitemaps.fetch_urls("https://example.com/sitemap.xml", _Boom())

    assert [f.reason for f in result.failures] == ["fetch_failed"]


def test_a_non_http_scheme_is_refused_without_fetching():
    class _NeverCalled:
        def get(self, url: str, **kwargs: object):
            raise AssertionError("must not fetch a non-http(s) URL")

    result = sitemaps.fetch_urls("file:///etc/passwd", _NeverCalled())

    assert [f.reason for f in result.failures] == ["bad_scheme"]


def test_a_loopback_host_is_refused_without_fetching():
    class _NeverCalled:
        def get(self, url: str, **kwargs: object):
            raise AssertionError("must not fetch a loopback URL")

    result = sitemaps.fetch_urls("http://127.0.0.1/sitemap.xml", _NeverCalled())

    assert [f.reason for f in result.failures] == ["blocked_host"]


def test_a_redirect_to_a_loopback_host_is_refused():
    session = _FakeSession({
        "https://example.com/sitemap.xml": _Resp(
            b"", status=302, headers={"Location": "http://127.0.0.1/x.xml"}),
    })

    result = sitemaps.fetch_urls("https://example.com/sitemap.xml", session)

    assert [f.reason for f in result.failures] == ["blocked_host"]
    assert session.asked == ["https://example.com/sitemap.xml"]


def test_recursion_stops_at_the_depth_cap():
    routes = {}
    for depth in range(8):
        routes[f"https://example.com/s{depth}.xml"] = _Resp(f"""<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/s{depth + 1}.xml</loc></sitemap>
</sitemapindex>""".encode())
    session = _FakeSession(routes)

    result = sitemaps.fetch_urls("https://example.com/s0.xml", session,
                                 max_depth=3)

    assert len(session.asked) == 3
    assert [f.reason for f in result.failures] == ["too_deep"]


def test_a_loc_that_is_not_http_is_dropped_from_the_url_set():
    body = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>javascript:alert(1)</loc></url>
  <url><loc>https://example.com/ok</loc></url>
</urlset>"""
    session = _FakeSession({"https://example.com/s.xml": _Resp(body.encode())})

    result = sitemaps.fetch_urls("https://example.com/s.xml", session)

    assert result.urls == ["https://example.com/ok"]


def test_every_failure_reason_is_in_the_closed_vocabulary():
    # A reason built by interpolation could carry a path or a host into a
    # tool result. The set is closed so it cannot.
    assert sitemaps.FAILURE_REASONS == frozenset({
        "bad_scheme", "blocked_host", "fetch_failed", "http_error",
        "too_large", "too_deep", "unparseable",
    })
