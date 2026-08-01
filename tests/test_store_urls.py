from datetime import datetime, timedelta, timezone, UTC

import pytest

from gsc_core import store

PROP = "sc-domain:example.com"
PROP_B = "sc-domain:other.example"


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
    stored = store.get_urls(conn, PROP)[0]["last_submitted"]
    # Compare instants, not text: the store normalises to fixed-width UTC, so
    # a raw isoformat() with zero microseconds will not match textually.
    assert datetime.fromisoformat(stored) == datetime.fromisoformat(stamp)


def test_get_urls_is_scoped_to_one_property(tmp_path):
    conn = _conn(tmp_path)
    now = _iso(datetime.now(UTC))
    store.upsert_url(conn, "https://example.com/a", PROP, "INDEXED", None, now)
    store.upsert_url(conn, "https://other.example/b", PROP_B, "INDEXED", None, now)
    assert [u["url"] for u in store.get_urls(conn, PROP)] == ["https://example.com/a"]


def test_stale_urls_is_scoped_to_one_property(tmp_path):
    conn = _conn(tmp_path)
    store.upsert_url(conn, "https://example.com/a", PROP, None, None, None)
    store.upsert_url(conn, "https://other.example/b", PROP_B, None, None, None)
    assert store.stale_urls(conn, PROP, ttl_days=7) == ["https://example.com/a"]


def test_rediscovery_does_not_clear_inspection_state(tmp_path):
    conn = _conn(tmp_path)
    url = "https://example.com/a"
    checked = _iso(datetime.now(UTC))
    store.upsert_url(conn, url, PROP, "NOT_INDEXED", "crawled_not_indexed", checked)
    # A second sitemap pass carries no inspection result.
    store.upsert_url(conn, url, PROP, None, None, None)
    row = store.get_urls(conn, PROP)[0]
    assert row["status"] == "NOT_INDEXED"
    assert row["reason"] == "crawled_not_indexed"
    assert row["checked_at"] is not None
    assert store.stale_urls(conn, PROP, ttl_days=7) == []


def test_inspection_result_clears_a_stale_reason(tmp_path):
    conn = _conn(tmp_path)
    url = "https://example.com/a"
    store.upsert_url(conn, url, PROP, "NOT_INDEXED", "crawled_not_indexed",
                     _iso(datetime.now(UTC)))
    store.upsert_url(conn, url, PROP, "INDEXED", None, _iso(datetime.now(UTC)))
    row = store.get_urls(conn, PROP)[0]
    assert row["status"] == "INDEXED"
    assert row["reason"] is None


def test_offset_timestamp_is_compared_chronologically_not_lexicographically(tmp_path):
    """A timestamp three hours NEWER than the cutoff, written with a negative
    UTC offset, sorts BEFORE the cutoff as a raw string: its wall-clock reads
    five hours earlier. Unnormalised it therefore looks stale, and the URL is
    re-inspected for nothing — a wasted call against the 2000/property/day
    budget, on every run, for every such URL.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    fresh_instant = now - timedelta(days=7) + timedelta(hours=3)
    west = fresh_instant.astimezone(timezone(timedelta(hours=-8)))

    store.upsert_url(conn, "https://example.com/west", PROP, "INDEXED", None,
                     west.isoformat())
    assert store.stale_urls(conn, PROP, ttl_days=7, now=now) == []


def test_z_and_naive_timestamp_spellings_are_accepted(tmp_path):
    """Google's URL Inspection API returns a 'Z' suffix and some callers pass
    naive datetimes. Both must be storable and readable back as instants.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    store.upsert_url(conn, "https://example.com/z", PROP, "INDEXED", None,
                     now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    store.upsert_url(conn, "https://example.com/naive", PROP, "INDEXED", None,
                     now.replace(tzinfo=None).isoformat())

    for row in store.get_urls(conn, PROP):
        stored = datetime.fromisoformat(row["checked_at"])
        assert stored.tzinfo is not None
        assert abs((stored - now).total_seconds()) < 2


def test_mark_submitted_raises_when_url_is_unknown(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(KeyError):
        store.mark_submitted(conn, "https://example.com/missing",
                             _iso(datetime.now(UTC)))
