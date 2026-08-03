from __future__ import annotations

import gzip
import io

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


class _CountingBytesIO(io.BytesIO):
    """A BytesIO that records how much of itself was actually read.

    GzipFile pulls from its fileobj lazily, so a bounded read touches only
    as much compressed input as it needs. gzip.decompress consumes the
    whole buffer. Counting the reads is what tells those two apart -- the
    reported failure reason cannot, since both end in too_large.
    """

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        return chunk


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


def test_a_urlset_without_a_namespace_still_yields_its_urls():
    body = b"<urlset><url><loc>https://example.com/a</loc></url></urlset>"
    session = _FakeSession({"https://example.com/s.xml": _Resp(body)})

    result = sitemaps.fetch_urls("https://example.com/s.xml", session)

    assert result.urls == ["https://example.com/a"]
    assert result.failures == []


def test_a_sitemapindex_in_a_foreign_namespace_is_still_followed():
    body = (
        b'<sitemapindex xmlns="urn:example:not-the-sitemaps-namespace">'
        b"<sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>"
        b"</sitemapindex>"
    )
    session = _FakeSession({
        "https://example.com/index.xml": _Resp(body),
        "https://example.com/sitemap-1.xml": _Resp(LEAF.encode()),
    })

    result = sitemaps.fetch_urls("https://example.com/index.xml", session)

    assert result.urls == ["https://example.com/a", "https://example.com/b"]
    assert result.failures == []


def test_an_unrecognized_root_element_is_a_reported_failure_not_an_empty_sitemap():
    body = b"<rss><channel><title>not a sitemap</title></channel></rss>"
    session = _FakeSession({"https://example.com/feed.xml": _Resp(body)})

    result = sitemaps.fetch_urls("https://example.com/feed.xml", session)

    assert result.urls == []
    assert [f.reason for f in result.failures] == ["unparseable"]


def test_a_gzip_bomb_is_capped_before_full_expansion():
    # ~5MB of a single repeated byte compresses to well under 6000 bytes,
    # so the outer (compressed) byte cap does not catch it -- only a bound
    # on the decompressed read can.
    payload = gzip.compress(b"a" * 5_000_000)
    assert len(payload) < 6000
    session = _FakeSession({
        "https://example.com/bomb.xml.gz": _Resp(payload),
    })

    result = sitemaps.fetch_urls("https://example.com/bomb.xml.gz", session,
                                 max_bytes=6000)

    assert result.urls == []
    assert [f.reason for f in result.failures] == ["too_large"]


def test_the_gzip_cap_stops_reading_the_compressed_source_early():
    # A reported "too_large" is identical whether the decompression was
    # bounded or whether the process fully expanded the payload first and
    # noticed afterwards. Only the amount of *compressed* input actually
    # consumed tells those two apart -- which requires a compressed source
    # bigger than gzip's own internal read buffer (128KB); a smaller one
    # gets read whole in a single underlying read() regardless of how the
    # cap is enforced, and the two implementations would be indistinguishable.
    payload = gzip.compress(b"a" * 100_000_000, compresslevel=1)
    assert len(payload) > 2 * 131_072
    counting = _CountingBytesIO(payload)

    data, over = sitemaps._gunzip_capped_fileobj(counting, 6000)

    assert data is None
    assert over is True
    assert counting.bytes_read < len(payload) // 2


def test_a_metadata_endpoint_host_is_refused_without_fetching():
    class _NeverCalled:
        def get(self, url: str, **kwargs: object):
            raise AssertionError("must not fetch the metadata endpoint")

    result = sitemaps.fetch_urls("http://169.254.169.254/sitemap.xml", _NeverCalled())

    assert [f.reason for f in result.failures] == ["blocked_host"]


def test_a_private_10_address_is_refused_without_fetching():
    class _NeverCalled:
        def get(self, url: str, **kwargs: object):
            raise AssertionError("must not fetch a private address")

    result = sitemaps.fetch_urls("http://10.0.0.5/sitemap.xml", _NeverCalled())

    assert [f.reason for f in result.failures] == ["blocked_host"]


def test_a_private_192_168_address_is_refused_without_fetching():
    class _NeverCalled:
        def get(self, url: str, **kwargs: object):
            raise AssertionError("must not fetch a private address")

    result = sitemaps.fetch_urls("http://192.168.1.1/sitemap.xml", _NeverCalled())

    assert [f.reason for f in result.failures] == ["blocked_host"]


def test_a_404_status_is_a_reported_http_error_failure():
    session = _FakeSession({
        "https://example.com/missing.xml": _Resp(b"", status=404),
    })

    result = sitemaps.fetch_urls("https://example.com/missing.xml", session)

    assert [f.reason for f in result.failures] == ["http_error"]


def test_redirects_are_capped_and_do_not_loop_forever():
    routes = {}
    for i in range(50):
        routes[f"https://example.com/r{i}.xml"] = _Resp(
            b"", status=302,
            headers={"Location": f"https://example.com/r{i + 1}.xml"})
    session = _FakeSession(routes)

    result = sitemaps.fetch_urls("https://example.com/r0.xml", session)

    assert [f.reason for f in result.failures] == ["http_error"]
    assert len(session.asked) == sitemaps.MAX_REDIRECTS + 1


def test_every_failure_reason_is_in_the_closed_vocabulary():
    # A reason built by interpolation could carry a path or a host into a
    # tool result. The set is closed so it cannot.
    assert sitemaps.FAILURE_REASONS == frozenset({
        "bad_scheme", "blocked_host", "fetch_failed", "http_error",
        "too_large", "too_deep", "unparseable",
    })
