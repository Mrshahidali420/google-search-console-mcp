from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gsc_core import discovery, sitemaps, store

PROPERTY = "https://example.com/"
PROPERTIES = [PROPERTY]


def _fetch_returning(urls: list[str]):
    def _fetch(sitemap_url: str, session=None, **kwargs):
        return sitemaps.SitemapResult(list(urls), [sitemap_url], [])
    return _fetch


def _check_returning(rows: list[dict]):
    calls: list[list[str]] = []

    def _check(conn, urls, provider, properties, **kwargs):
        calls.append(list(urls))
        return {"rows": rows, "checked": len(urls), "skipped_quota": [],
                "quota": {"reserved": len(urls)}}
    return _check, calls


def test_sitemap_source_discovers_urls_and_inspects_them(store_conn):
    fetch = _fetch_returning(["https://example.com/a", "https://example.com/b"])
    check, calls = _check_returning([
        {"url": "https://example.com/a", "status": "indexed", "detail": ""},
        {"url": "https://example.com/b", "status": "discovered_not_indexed",
         "detail": ""},
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="sitemap", _fetch=fetch, _check=check,
        _sitemap_urls=["https://example.com/sitemap.xml"])

    assert out["ok"] is True
    assert out["candidates_total"] == 2
    assert out["inspected"] == 2
    assert calls == [["https://example.com/a", "https://example.com/b"]]
    assert [row["url"] for row in out["unindexed"]] == ["https://example.com/b"]
    assert out["unindexed"][0]["reason"] == "discovered-not-indexed"
    assert out["unindexed"][0]["submitting_helps"] is True
    assert out["indexed"] == 1


def test_discovery_upserts_sitemap_urls_before_inspecting(store_conn):
    # The store.upsert_url contract (store.py:300-307): a discovery pass
    # carries no inspection result, so status/reason/checked_at are None and
    # mean "no new information".
    fetch = _fetch_returning(["https://example.com/a"])
    check, _ = _check_returning([])

    discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="sitemap", _fetch=fetch, _check=check,
        _sitemap_urls=["https://example.com/sitemap.xml"])

    rows = store.get_urls(store_conn, PROPERTY)
    assert [r["url"] for r in rows] == ["https://example.com/a"]
    assert rows[0]["first_seen"]


def test_store_source_makes_no_http_request(store_conn):
    def _fetch(*args, **kwargs):
        raise AssertionError("source='store' must not fetch a sitemap")

    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         None, None, None)
    check, calls = _check_returning([
        {"url": "https://example.com/a", "status": "noindex", "detail": ""},
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _fetch=_fetch, _check=check)

    assert out["sitemaps_read"] == []
    assert [row["reason"] for row in out["unindexed"]] == ["noindex"]


def test_a_fresh_url_is_reported_from_the_store_without_being_inspected(store_conn):
    # The load-bearing case: a second call the same day must still say
    # which pages are unindexed, but must not spend a slot re-asking.
    recent = store.utc_iso(datetime.now(UTC) - timedelta(hours=1))
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         "noindex", "noindex", recent)
    check, calls = _check_returning([])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _check=check)

    assert calls == []
    assert out["inspected"] == 0
    assert [row["url"] for row in out["unindexed"]] == ["https://example.com/a"]
    assert out["unindexed"][0]["fresh"] is True
    assert out["unindexed"][0]["reason"] == "noindex"


def test_a_stale_url_is_re_inspected(store_conn):
    old = store.utc_iso(datetime.now(UTC) - timedelta(days=30))
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         "noindex", "noindex", old)
    check, calls = _check_returning([
        {"url": "https://example.com/a", "status": "indexed", "detail": ""},
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", ttl_days=7, _check=check)

    assert calls == [["https://example.com/a"]]
    assert out["unindexed"] == []
    assert out["indexed"] == 1


def test_limit_caps_what_is_inspected_not_what_is_returned(store_conn):
    urls = [f"https://example.com/p{n}" for n in range(10)]
    with store.tx(store_conn):
        for url in urls:
            store.upsert_url(store_conn, url, PROPERTY, None, None, None)
    check, calls = _check_returning([
        {"url": url, "status": "noindex", "detail": ""} for url in urls[:3]
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", limit=3, _check=check)

    assert len(calls[0]) == 3
    assert out["inspected"] == 3
    assert out["limited"] is True
    assert out["candidates_total"] == 10
    assert out["limit"] == 3


def test_limit_is_not_reported_as_limiting_when_it_was_not_reached(store_conn):
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         None, None, None)
    check, _ = _check_returning([
        {"url": "https://example.com/a", "status": "indexed", "detail": ""},
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", limit=50, _check=check)

    assert out["limited"] is False


def test_an_error_row_is_undetermined_never_unindexed(store_conn):
    # Reporting a failed inspection as "unindexed, reason unknown" would be
    # a fabricated finding.
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         None, None, None)
    check, _ = _check_returning([
        {"url": "https://example.com/a", "status": "error",
         "detail": "HTTP 503"},
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _check=check)

    assert out["unindexed"] == []
    assert [row["url"] for row in out["undetermined"]] == ["https://example.com/a"]
    assert out["undetermined"][0]["status"] == "error"


def test_an_unverified_row_carries_its_flag_into_the_result(store_conn):
    # api.py:238-265: under a concurrent burst the Inspection API silently
    # downgrades real coverage states to "unknown to Google". A row the
    # re-verify loop could not confirm must never be reported as fact.
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         None, None, None)
    check, _ = _check_returning([
        {"url": "https://example.com/a", "status": "unknown_to_google",
         "detail": "", "unverified": "inspection budget exhausted"},
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _check=check)

    assert out["unindexed"] == []
    assert out["undetermined"][0]["unverified"] == "inspection budget exhausted"


def test_sitemap_failures_are_reported_not_swallowed(store_conn):
    def _fetch(sitemap_url, session=None, **kwargs):
        return sitemaps.SitemapResult(
            [], [], [sitemaps.SitemapFailure(sitemap_url, "http_error")])
    check, _ = _check_returning([])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="sitemap", _fetch=_fetch, _check=check,
        _sitemap_urls=["https://example.com/sitemap.xml"])

    assert out["sitemap_failures"] == [
        {"url": "https://example.com/sitemap.xml", "reason": "http_error"}]
    assert out["candidates_total"] == 0


def test_both_unions_sitemap_and_store_with_sitemap_order_first(store_conn):
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/stored", PROPERTY,
                         None, None, None)
    fetch = _fetch_returning(["https://example.com/fresh"])
    check, calls = _check_returning([])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="both", _fetch=fetch, _check=check,
        _sitemap_urls=["https://example.com/sitemap.xml"])

    assert calls[0] == ["https://example.com/fresh",
                        "https://example.com/stored"]


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        discovery.find_unindexed(
            None, PROPERTY, provider=object(), properties=PROPERTIES,
            source="everything")
