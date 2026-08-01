from datetime import datetime, timedelta, UTC

from gsc_core import store

PROP = "sc-domain:example.com"


def _conn(tmp_path):
    return store.connect(tmp_path / "state.db")


def _iso(moment):
    return moment.isoformat()


def test_upsert_site_then_read_back(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_site(conn, PROP, "example.com", "siteOwner",
                      ["https://example.com/sitemap.xml"])
    sites = store.get_sites(conn)
    assert len(sites) == 1
    assert sites[0]["property"] == PROP
    assert sites[0]["permission"] == "siteOwner"
    assert sites[0]["sitemaps"] == ["https://example.com/sitemap.xml"]


def test_upsert_site_is_idempotent(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_site(conn, PROP, "example.com", "siteOwner", [])
    store.upsert_site(conn, PROP, "example.com", "siteFullUser", [])
    sites = store.get_sites(conn)
    assert len(sites) == 1
    assert sites[0]["permission"] == "siteFullUser"


def test_upsert_url_preserves_first_seen(tmp_path):
    conn = _conn(tmp_path)
    url = "https://example.com/a"
    store.upsert_url(conn, url, PROP, "INDEXED", None, _iso(datetime.now(UTC)))
    original = store.get_urls(conn, PROP)[0]["first_seen"]

    store.upsert_url(conn, url, PROP, "NOT_INDEXED", "crawled_not_indexed",
                     _iso(datetime.now(UTC)))
    updated = store.get_urls(conn, PROP)[0]
    assert updated["first_seen"] == original
    assert updated["status"] == "NOT_INDEXED"
    assert updated["reason"] == "crawled_not_indexed"


def test_get_urls_filters_by_status(tmp_path):
    conn = _conn(tmp_path)
    now = _iso(datetime.now(UTC))
    store.upsert_url(conn, "https://example.com/a", PROP, "INDEXED", None, now)
    store.upsert_url(conn, "https://example.com/b", PROP, "NOT_INDEXED",
                     "discovered", now)
    not_indexed = store.get_urls(conn, PROP, status="NOT_INDEXED")
    assert [u["url"] for u in not_indexed] == ["https://example.com/b"]


def test_stale_urls_returns_only_expired_entries(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    store.upsert_url(conn, "https://example.com/fresh", PROP, "INDEXED", None,
                     _iso(now - timedelta(days=1)))
    store.upsert_url(conn, "https://example.com/stale", PROP, "INDEXED", None,
                     _iso(now - timedelta(days=30)))
    stale = store.stale_urls(conn, PROP, ttl_days=7, now=now)
    assert stale == ["https://example.com/stale"]


def test_never_checked_url_counts_as_stale(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_url(conn, "https://example.com/new", PROP, None, None, None)
    stale = store.stale_urls(conn, PROP, ttl_days=7)
    assert stale == ["https://example.com/new"]


def test_mark_submitted_sets_timestamp(tmp_path):
    conn = _conn(tmp_path)
    url = "https://example.com/a"
    store.upsert_url(conn, url, PROP, "NOT_INDEXED", "discovered", None)
    stamp = _iso(datetime.now(UTC))
    store.mark_submitted(conn, url, stamp)
    assert store.get_urls(conn, PROP)[0]["last_submitted"] == stamp
