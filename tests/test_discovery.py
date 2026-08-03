from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gsc_core import discovery, sitemaps, store

from _logcheck import capturing

PROPERTY = "https://example.com/"
PROPERTIES = [PROPERTY]


def _fetch_returning(urls: list[str]):
    def _fetch(sitemap_url: str, session=None, **kwargs):
        return sitemaps.SitemapResult(list(urls), [sitemap_url], [])
    return _fetch


def _row(url: str, status: str, detail: str = "", *,
         unverified: bool = False) -> dict:
    """One row shaped the way api._rows (api.py:660-676) actually shapes it.

    "unverified" is ALWAYS present and ALWAYS a bool there --
    `rows.append({**row, "unverified": url in unverified})`. It is never
    absent, never a string, never None. Every fake in this file went
    through this helper after a fake that omitted the key hid a critical
    defect from 1187 tests: discovery branched on `unverified is not None`,
    so against a real row (`False`) every freshly inspected URL was filed
    as undetermined and `unindexed` came back empty on every live run.
    """
    return {"url": url, "status": status, "detail": detail,
            "unverified": unverified}


def _check_returning(rows: list[dict], skipped: list[dict] | None = None):
    calls: list[list[str]] = []

    def _check(conn, urls, provider, properties, **kwargs):
        for row in rows:
            assert isinstance(row.get("unverified"), bool), (
                "this fake disagrees with its real producer: api._rows "
                "always sets 'unverified' to a bool")
        calls.append(list(urls))
        return {"rows": rows, "checked": len(urls),
                "skipped_quota": list(skipped or []),
                "quota": {"reserved": len(urls)}}
    return _check, calls


def test_sitemap_source_discovers_urls_and_inspects_them(store_conn):
    fetch = _fetch_returning(["https://example.com/a", "https://example.com/b"])
    check, calls = _check_returning([
        _row("https://example.com/a", "indexed"),
        _row("https://example.com/b", "discovered_not_indexed"),
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
        _row("https://example.com/a", "noindex"),
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
        _row("https://example.com/a", "indexed"),
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
        _row(url, "noindex") for url in urls[:3]
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
        _row("https://example.com/a", "indexed"),
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
        _row("https://example.com/a", "error", "HTTP 503"),
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
        _row("https://example.com/a", "unknown_to_google",
             "unknown to Google | not re-verified: inspection budget spent",
             unverified=True),
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _check=check)

    assert out["unindexed"] == []
    assert out["undetermined"][0]["unverified"] is True
    # The honest reason lives in the detail, appended by api._rows.
    assert "not re-verified" in out["undetermined"][0]["detail"]


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


def test_both_reports_sitemap_urls_before_store_only_urls(store_conn):
    # What the union order actually governs is the order of the FINDINGS:
    # the inspection order follows store.stale_urls, which sorts by url. So
    # the URLs here are chosen so alphabetical order is the REVERSE of
    # sitemap-first order -- with names that happen to sort the right way,
    # this assertion would pass under either union order and pin nothing.
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/z-sitemap", PROPERTY,
                         None, None, None)
        store.upsert_url(store_conn, "https://example.com/a-stored", PROPERTY,
                         None, None, None)
    fetch = _fetch_returning(["https://example.com/z-sitemap"])
    check, _ = _check_returning([
        _row("https://example.com/a-stored", "noindex"),
        _row("https://example.com/z-sitemap", "noindex"),
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="both", _fetch=fetch, _check=check,
        _sitemap_urls=["https://example.com/sitemap.xml"])

    assert [row["url"] for row in out["unindexed"]] == [
        "https://example.com/z-sitemap", "https://example.com/a-stored"]
    # z-sitemap is in both the sitemap and the store; the union counts it once.
    assert out["candidates_total"] == 2


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        discovery.find_unindexed(
            None, PROPERTY, provider=object(), properties=PROPERTIES,
            source="everything")


def test_a_row_nothing_doubted_is_classified_not_filed_as_undetermined(store_conn):
    """The critical one. `unverified: False` is the everyday case.

    api._rows sets "unverified" on EVERY row and sets it to `False` for
    every row the re-verification pass had no doubt about -- which, on a
    healthy run, is all of them. Branching on `unverified is not None`
    made `False is not None` true, so every freshly inspected URL was
    filed as undetermined and `unindexed` came back empty on every live
    run: the tool's entire purpose silently producing nothing, read by the
    caller as "this site has no indexing problems".
    """
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         None, None, None)
    check, _ = _check_returning([
        _row("https://example.com/a", "noindex", "noindex", unverified=False),
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _check=check)

    assert out["undetermined"] == []
    assert [row["url"] for row in out["unindexed"]] == ["https://example.com/a"]
    assert out["unindexed"][0]["reason"] == "noindex"
    assert out["unindexed"][0]["fresh"] is False


def test_only_a_truthy_unverified_flag_withholds_a_verdict(store_conn):
    """Both halves of the flag in one run, so neither can drift alone.

    Asserting only the False case would still pass if the guard were
    dropped entirely; asserting only the True case is what shipped.
    """
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/doubted", PROPERTY,
                         None, None, None)
        store.upsert_url(store_conn, "https://example.com/settled", PROPERTY,
                         None, None, None)
    check, _ = _check_returning([
        _row("https://example.com/doubted", "unknown_to_google",
             "unknown to Google | not re-verified: time budget spent",
             unverified=True),
        _row("https://example.com/settled", "unknown_to_google",
             "unknown to Google", unverified=False),
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _check=check)

    assert [row["url"] for row in out["unindexed"]] == [
        "https://example.com/settled"]
    assert out["unindexed"][0]["reason"] == "unknown-to-google"
    assert [row["url"] for row in out["undetermined"]] == [
        "https://example.com/doubted"]
    assert out["undetermined"][0]["unverified"] is True


def _quota_skipped(url: str, property: str = PROPERTY) -> dict:
    """One entry as api._skipped_rows (api.py:638-646) shapes it."""
    return {"url": url, "property": property, "binding_at_gate": "daily",
            "retry_after_seconds": 3600}


def test_a_quota_skipped_url_is_undetermined_and_says_so(store_conn):
    """`skipped_quota` is the everyday path, not an internal inconsistency.

    With roughly eleven inspection slots per property, any site larger
    than a dozen URLs hits the gate on an ordinary run. api._rows mints
    the status; reasons.py has to know it, or discovery falls through to
    the unmapped-status branch and reports a routine outcome as a bug.

    "we looked and could not tell" and "we never got to look" call for
    opposite actions -- the second is simply "run again tomorrow" -- so
    the caller has to be able to tell them apart.
    """
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         None, None, None)
    skipped = _quota_skipped("https://example.com/a")
    check, _ = _check_returning(
        [_row("https://example.com/a", "skipped_quota",
              "inspection quota exhausted; retry in 3600s")],
        skipped=[skipped])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", _check=check)

    assert out["unindexed"] == []
    assert [row["status"] for row in out["undetermined"]] == ["skipped_quota"]
    # Surfaced rather than discarded: this is what makes "run again
    # tomorrow" distinguishable from "we asked and got no answer".
    assert out["skipped_quota"] == [skipped]
    assert out["checked"] == 1


def test_a_quota_skip_is_not_logged_as_an_unmapped_status(store_conn):
    """Positive control FIRST: a negative log assertion over an empty
    buffer passes forever, and runlog.py:24 sets propagate = False so
    caplog would never see these records at all.
    """
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a", PROPERTY,
                         None, None, None)

    # Control: a status genuinely nobody anticipated DOES log.
    unmapped, _ = _check_returning(
        [_row("https://example.com/a", "a_status_from_the_future")])
    with capturing(discovery.log) as records:
        discovery.find_unindexed(
            store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
            source="store", _check=unmapped)
    assert records, "the unmapped-status fallback should still log"

    # The real assertion: a quota skip is routine and logs nothing.
    check, _ = _check_returning(
        [_row("https://example.com/a", "skipped_quota", "retry later")],
        skipped=[_quota_skipped("https://example.com/a")])
    with capturing(discovery.log) as records:
        discovery.find_unindexed(
            store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
            source="store", _check=check)
    assert list(records) == [], records.text


def test_a_store_url_outside_the_candidate_set_is_never_inspected(store_conn):
    """The `url in known` filter guards the one resource that cannot be
    reclaimed. A spent inspection slot is gone; a source="sitemap" run
    that inspected every stale store URL would spend the property's whole
    daily allowance on URLs the caller did not ask about.
    """
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/not-in-sitemap",
                         PROPERTY, None, None, None)
    fetch = _fetch_returning(["https://example.com/in-sitemap"])
    check, calls = _check_returning([
        _row("https://example.com/in-sitemap", "noindex"),
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="sitemap", _fetch=fetch, _check=check,
        _sitemap_urls=["https://example.com/sitemap.xml"])

    assert calls == [["https://example.com/in-sitemap"]]
    assert "https://example.com/not-in-sitemap" not in calls[0]
    assert out["inspected"] == 1


def test_fresh_says_this_run_did_not_inspect_it(store_conn):
    """What server.py's docstring promises, in one run carrying both.

    `"fresh": true` means THIS run did not inspect the URL -- usually
    because it was inside the TTL. Asserting only the true side leaves
    `"fresh": True` hardcodable, which the suite could not detect.
    """
    recent = store.utc_iso(datetime.now(UTC) - timedelta(hours=1))
    old = store.utc_iso(datetime.now(UTC) - timedelta(days=30))
    with store.tx(store_conn):
        store.upsert_url(store_conn, "https://example.com/a-stale", PROPERTY,
                         "noindex", "noindex", old)
        store.upsert_url(store_conn, "https://example.com/b-recent", PROPERTY,
                         "noindex", "noindex", recent)
    check, calls = _check_returning([
        _row("https://example.com/a-stale", "noindex", "noindex"),
    ])

    out = discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="store", ttl_days=7, _check=check)

    assert calls == [["https://example.com/a-stale"]]
    fresh_by_url = {row["url"]: row["fresh"] for row in out["unindexed"]}
    assert fresh_by_url == {"https://example.com/a-stale": False,
                            "https://example.com/b-recent": True}

OVERLAPPING = ["https://example.com/", "https://example.com/blog/"]


def test_discovery_attributes_a_url_the_way_api_persist_does(store_conn):
    """Two writers, one answer. api._persist attributes by
    routing.route_all and store.upsert_url overwrites `property` on
    conflict, so a discovery pass that attributed by its `property`
    argument instead made the two flip the same rows back and forth --
    and gsc_audit's per-property counts moved depending on which tool ran
    last.
    """
    from gsc_core import routing

    url = "https://example.com/blog/post"
    fetch = _fetch_returning([url])
    check, _ = _check_returning([])

    discovery.find_unindexed(
        store_conn, OVERLAPPING[1], provider=object(),
        properties=OVERLAPPING, source="sitemap", _fetch=fetch, _check=check,
        _sitemap_urls=["https://example.com/blog/sitemap.xml"])

    routed = routing.resolve_property(url, OVERLAPPING)
    assert [row["url"] for row in store.get_urls(store_conn, routed)] == [url]
    other = next(p for p in OVERLAPPING if p != routed)
    assert store.get_urls(store_conn, other) == []


def test_a_sitemap_url_no_property_covers_is_not_written(store_conn):
    """api._persist writes only routed targets, so a URL routing cannot
    place has no property to be written under. Inventing one would put a
    foreign host's URL into this account's counts.
    """
    fetch = _fetch_returning(["https://elsewhere.invalid/page"])
    check, _ = _check_returning([])

    discovery.find_unindexed(
        store_conn, PROPERTY, provider=object(), properties=PROPERTIES,
        source="sitemap", _fetch=fetch, _check=check,
        _sitemap_urls=["https://example.com/sitemap.xml"])

    assert store.get_urls(store_conn, PROPERTY) == []
