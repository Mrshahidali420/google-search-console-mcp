"""Tests for gsc_performance and gsc_submit_sitemaps.

The brief's own fixture hands `deps.provider` back as a bare `object()` --
deliberately, since every test that uses it also monkeypatches the perf.*/
api.* function the tool would otherwise call, so nothing ever calls
`.access_token()` on that stand-in. The auth-path tests below use a real
stand-in with an `access_token()` that raises, and leave perf.*/api.*
UNPATCHED, so the real propagation path (provider.access_token() raising
inside post_query/_auth_headers, before any network call) is what is
actually being exercised -- not a mock standing in for it.
"""
from __future__ import annotations

import pytest

from gsc_core import gauth, perf, store
from gsc_mcp import server


class _SignedOutProvider:
    """Stands in for what deps.provider() returns when a client is
    configured but nobody has run the consent flow. AuthRequired only ever
    surfaces from access_token(), never from provider() itself -- matching
    gauth.TokenProvider's real behaviour (see server.py's gsc_check_status
    docstring)."""

    def access_token(self) -> str:
        raise gauth.AuthRequired("no stored credentials; run gsc_setup()")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.com", "example.com", "siteOwner", [])
    return tmp_path


# ------------------------------------------------------------- gsc_performance


def test_start_date_without_end_date_is_refused(home):
    out = server.gsc_performance(site="example.com", start_date="2026-07-01")
    assert out["ok"] is False
    assert "end_date" in out["note"]


def test_no_site_returns_a_portfolio(home, monkeypatch):
    monkeypatch.setattr(server.perf, "portfolio", lambda *a, **k: [
        {"site": "sc-domain:example.com", "clicks": 5, "impressions": 50,
         "ctr": 0.1, "position": 3.0}])
    out = server.gsc_performance()
    assert out["scope"] == "portfolio"
    assert out["totals"]["clicks"] == 5
    assert out["sites"][0]["site"] == "sc-domain:example.com"


def test_site_without_dimension_returns_totals(home, monkeypatch):
    monkeypatch.setattr(server.perf, "totals", lambda *a, **k: {"clicks": 7})
    assert server.gsc_performance(site="example.com")["scope"] == "site"


def test_site_with_dimension_returns_sorted_rows(home, monkeypatch):
    monkeypatch.setattr(server.perf, "query", lambda *a, **k: [
        {"query": "a", "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 4.0},
        {"query": "b", "clicks": 9, "impressions": 90, "ctr": 0.1, "position": 2.0}])
    out = server.gsc_performance(site="example.com", dim="query")
    assert [r["query"] for r in out["rows"]] == ["b", "a"]
    assert out["scope"] == "query"
    # totals is a real aggregate_rows() over the (already-sorted) rows, not
    # a copy of one row or a mocked value -- 1 + 9 clicks.
    assert out["totals"]["clicks"] == 10


def test_data_state_final_carries_a_warning(home, monkeypatch):
    monkeypatch.setattr(server.perf, "totals", lambda *a, **k: {"clicks": 0})
    out = server.gsc_performance(site="example.com", data_state="final")
    assert "warning" in out
    assert str(perf.FINAL_LAG_DAYS) in out["warning"]


def test_data_state_all_carries_no_warning(home, monkeypatch):
    monkeypatch.setattr(server.perf, "totals", lambda *a, **k: {"clicks": 0})
    assert "warning" not in server.gsc_performance(site="example.com")


def test_perf_error_becomes_a_structured_answer(home, monkeypatch):
    def boom(*a, **k):
        raise perf.PerfError("no access")

    monkeypatch.setattr(server.perf, "totals", boom)
    out = server.gsc_performance(site="example.com")
    assert out["ok"] is False
    assert "no access" in out["note"]
    # PerfError's structured answer still carries the window it failed
    # against -- the brief's `{"ok": False, **window, "note": ...}` contract.
    assert "start" in out and "end" in out


def test_bad_days_becomes_a_structured_answer_not_a_raise(home):
    out = server.gsc_performance(site="example.com", days=0)
    assert out["ok"] is False
    assert "days" in out["note"]


def test_performance_signed_out_returns_auth_required_not_zeros(home, monkeypatch):
    """The core regression this task exists to prevent: a signed-out caller
    must get an auth answer, never a fabricated report. perf.totals is left
    UNPATCHED here -- the real function runs, reaches post_query, and
    provider.access_token() raises before any HTTP call is attempted."""
    monkeypatch.setattr(server.deps, "provider", lambda: _SignedOutProvider())
    out = server.gsc_performance(site="example.com")
    assert out == {"ok": False, "error": "auth_required", "fix": server._FIX_TOKEN}


def test_performance_signed_out_portfolio_path_also_refuses(home, monkeypatch):
    """Same guarantee on the portfolio dispatch branch, whose per-property
    except clause only catches PerfError/ValueError -- confirms
    AuthRequired is not swallowed there either."""
    monkeypatch.setattr(server.deps, "provider", lambda: _SignedOutProvider())
    out = server.gsc_performance()
    assert out == {"ok": False, "error": "auth_required", "fix": server._FIX_TOKEN}


def test_performance_signed_out_query_dimension_path_also_refuses(home, monkeypatch):
    """Same guarantee on the third dispatch branch (site + dim -> perf.query),
    left untested by the first two signed-out tests."""
    monkeypatch.setattr(server.deps, "provider", lambda: _SignedOutProvider())
    out = server.gsc_performance(site="example.com", dim="query")
    assert out == {"ok": False, "error": "auth_required", "fix": server._FIX_TOKEN}


def test_performance_signed_out_multi_property_portfolio_forces_the_thread_pool(
    home, monkeypatch,
):
    """With a single property, perf.portfolio's workers=1 path runs `one()`
    inline in a list comprehension and never touches ThreadPoolExecutor --
    too easy a case to prove AuthRequired survives the threaded path. A
    second property pushes workers to 2, forcing the real pool.map() code
    path that a future change could accidentally wrap in a broader except
    clause."""
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.net", "example.net",
                          "siteOwner", [])
    monkeypatch.setattr(server.deps, "provider", lambda: _SignedOutProvider())
    out = server.gsc_performance()
    assert out == {"ok": False, "error": "auth_required", "fix": server._FIX_TOKEN}


def test_performance_not_configured_returns_a_setup_answer(home, monkeypatch):
    def boom():
        raise server.deps.NotConfigured("no client")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_performance(site="example.com")
    assert out == {"ok": False, "error": "not_configured",
                   "fix": server._FIX_OAUTH_CLIENT}


# ---------------------------------------------------------- gsc_submit_sitemaps


def test_submit_sitemaps_routes_each_url_to_its_property(home, monkeypatch):
    seen = []
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: seen.append((prop, sm)) or
                        {"site": prop, "sitemap": sm, "ok": True,
                         "http_status": 200, "note": ""})
    server.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])
    assert seen == [("sc-domain:example.com", "https://example.com/sitemap.xml")]


def test_submit_sitemaps_with_nothing_known_refuses_to_guess(home):
    out = server.gsc_submit_sitemaps()
    assert out["ok"] is False
    assert "sitemap" in out["note"].lower()


def test_a_submitted_sitemap_is_remembered_for_next_time(home, monkeypatch):
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: {"site": prop, "sitemap": sm,
                                                         "ok": True, "http_status": 200,
                                                         "note": ""})
    server.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])
    with store.session() as conn:
        assert store.get_sites(conn)[0]["sitemaps"] == ["https://example.com/sitemap.xml"]


def test_submit_sitemaps_omitted_resubmits_what_the_store_already_knows(home, monkeypatch):
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.com", "example.com", "siteOwner",
                          ["https://example.com/sitemap.xml"])
    seen = []
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: seen.append((prop, sm)) or
                        {"site": prop, "sitemap": sm, "ok": True,
                         "http_status": 200, "note": ""})
    out = server.gsc_submit_sitemaps()
    assert seen == [("sc-domain:example.com", "https://example.com/sitemap.xml")]
    assert out[0]["ok"] is True


def test_submit_sitemaps_does_not_duplicate_an_already_recorded_sitemap(home, monkeypatch):
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.com", "example.com", "siteOwner",
                          ["https://example.com/sitemap.xml"])
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: {"site": prop, "sitemap": sm,
                                                         "ok": True, "http_status": 200,
                                                         "note": ""})
    server.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])
    with store.session() as conn:
        assert store.get_sites(conn)[0]["sitemaps"] == \
            ["https://example.com/sitemap.xml"]


def test_submit_sitemaps_adds_a_second_sitemap_without_losing_the_first(
    home, monkeypatch,
):
    """The dedup test above resubmits the SAME url, which a `merged = []`
    regression would pass too (one url in, one url out). This submits a
    DIFFERENT sitemap for the same property and checks the FIRST one is
    still there afterwards -- the actual "existing sitemaps must not be
    lost" guarantee gsc_list_sites' carry-forward fix depends on."""
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.com", "example.com", "siteOwner",
                          ["https://example.com/old.xml"])
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: {"site": prop, "sitemap": sm,
                                                         "ok": True, "http_status": 200,
                                                         "note": ""})
    server.gsc_submit_sitemaps(["https://example.com/new.xml"])
    with store.session() as conn:
        assert store.get_sites(conn)[0]["sitemaps"] == [
            "https://example.com/old.xml", "https://example.com/new.xml"]


def test_submit_sitemaps_keeps_a_second_property_sitemap_unmerged(home, monkeypatch):
    """Recording a success for property A must not touch property B's row --
    each property's sitemaps merge independently."""
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.net", "example.net", "siteOwner",
                          ["https://example.net/old.xml"])
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: {"site": prop, "sitemap": sm,
                                                         "ok": True, "http_status": 200,
                                                         "note": ""})
    server.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])
    with store.session() as conn:
        sites = {row["property"]: row["sitemaps"] for row in store.get_sites(conn)}
    assert sites["sc-domain:example.net"] == ["https://example.net/old.xml"]
    assert sites["sc-domain:example.com"] == ["https://example.com/sitemap.xml"]


def test_submit_sitemaps_unroutable_url_is_reported_not_submitted(home, monkeypatch):
    calls = []
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: calls.append((prop, sm)) or
                        {"site": prop, "sitemap": sm, "ok": True,
                         "http_status": 200, "note": ""})
    out = server.gsc_submit_sitemaps(["https://nomatch.example.org/sitemap.xml"])
    assert calls == []
    assert out == [{"site": None, "sitemap": "https://nomatch.example.org/sitemap.xml",
                    "http_status": None, "ok": False,
                    "note": "no Search Console property matches this URL"}]


def test_submit_sitemaps_failed_submission_is_not_recorded(home, monkeypatch):
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: {"site": prop, "sitemap": sm,
                                                         "ok": False, "http_status": 500,
                                                         "note": "boom"})
    server.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])
    with store.session() as conn:
        assert store.get_sites(conn)[0]["sitemaps"] == []


def test_submit_sitemaps_signed_out_returns_auth_required_and_submits_nothing(
    home, monkeypatch,
):
    """api.submit_sitemap is left UNPATCHED -- the real function runs,
    calls provider.access_token() inside _auth_headers before its PUT, and
    that is what raises here."""
    monkeypatch.setattr(server.deps, "provider", lambda: _SignedOutProvider())
    out = server.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])
    assert out == {"ok": False, "error": "auth_required", "fix": server._FIX_TOKEN}
    with store.session() as conn:
        assert store.get_sites(conn)[0]["sitemaps"] == []


def test_submit_sitemaps_not_configured_returns_a_setup_answer(home, monkeypatch):
    def boom():
        raise server.deps.NotConfigured("no client")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_submit_sitemaps(["https://example.com/sitemap.xml"])
    assert out == {"ok": False, "error": "not_configured",
                   "fix": server._FIX_OAUTH_CLIENT}


# ------------------------------- gsc_submit_sitemaps: forgetting a submission
#
# Three independent ways this tool could forget a sitemap Google has already
# accepted, all of them silent -- the row simply never comes back, and
# because a bare gsc_submit_sitemaps() resubmits only what the store knows
# about, it is never resubmitted either. One test each.

def _ok(prop, sm):
    return {"site": prop, "sitemap": sm, "ok": True, "http_status": 200,
            "note": ""}


def test_a_concurrent_submission_is_not_overwritten(home, monkeypatch):
    """(1) The lost update.

    The tool reads the site rows, then makes N slow network calls, then
    merges against that PRE-NETWORK snapshot -- and upsert_site replaces the
    sitemaps column outright. A second submit that commits in between had
    its sitemap silently erased. The fake below commits exactly that way,
    from its own connection, while the "network call" is in flight.
    """
    def submit(prop, sitemap_url, provider, **kwargs):
        with store.session() as other, store.tx(other):
            store.upsert_site(other, prop, "example.com", "siteOwner",
                              ["https://example.com/concurrent.xml"])
        return _ok(prop, sitemap_url)

    monkeypatch.setattr(server.api, "submit_sitemap", submit)
    server.gsc_submit_sitemaps(["https://example.com/mine.xml"])

    with store.session() as conn:
        assert store.get_sites(conn)[0]["sitemaps"] == [
            "https://example.com/concurrent.xml", "https://example.com/mine.xml"]


def test_a_property_deleted_mid_flight_still_records_its_submission(home, monkeypatch):
    """(2) The re-read finding nothing.

    Re-reading inside the transaction is what fixes the lost update, but it
    introduces a row that can be absent. Re-creating it is the recoverable
    direction; dropping a sitemap Google accepted is not.
    """
    def submit(prop, sitemap_url, provider, **kwargs):
        with store.session() as other, store.tx(other):
            other.execute("DELETE FROM sites WHERE property = ?", (prop,))
        return _ok(prop, sitemap_url)

    monkeypatch.setattr(server.api, "submit_sitemap", submit)
    server.gsc_submit_sitemaps(["https://example.com/mine.xml"])

    with store.session() as conn:
        sites = store.get_sites(conn)
    assert sites[0]["property"] == "sc-domain:example.com"
    assert sites[0]["sitemaps"] == ["https://example.com/mine.xml"]


def test_a_token_dying_mid_loop_keeps_what_already_succeeded(home, monkeypatch):
    """(3) The mid-loop AuthRequired.

    The first sitemap really did reach Google. Bailing out without
    persisting it would leave a live sitemap the store has no record of.
    """
    def submit(prop, sitemap_url, provider, **kwargs):
        if sitemap_url.endswith("second.xml"):
            raise gauth.AuthRequired("token died mid-loop")
        return _ok(prop, sitemap_url)

    monkeypatch.setattr(server.api, "submit_sitemap", submit)
    out = server.gsc_submit_sitemaps(["https://example.com/first.xml",
                                      "https://example.com/second.xml"])

    assert out == {"ok": False, "error": "auth_required", "fix": server._FIX_TOKEN}
    with store.session() as conn:
        assert store.get_sites(conn)[0]["sitemaps"] == [
            "https://example.com/first.xml"]


def test_a_token_dying_mid_loop_stops_rather_than_carrying_on(home, monkeypatch):
    """The break is load-bearing: once the token is dead every remaining
    PUT would fail too, and each is an outward-facing call."""
    attempted = []

    def submit(prop, sitemap_url, provider, **kwargs):
        attempted.append(sitemap_url)
        if sitemap_url.endswith("second.xml"):
            raise gauth.AuthRequired("token died mid-loop")
        return _ok(prop, sitemap_url)

    monkeypatch.setattr(server.api, "submit_sitemap", submit)
    server.gsc_submit_sitemaps(["https://example.com/first.xml",
                                "https://example.com/second.xml",
                                "https://example.com/third.xml"])
    assert attempted == ["https://example.com/first.xml",
                         "https://example.com/second.xml"]


def test_submit_sitemaps_reports_an_unexpected_failure_as_data(home, monkeypatch):
    """B3: a failure this tool does not model still keeps the
    {ok, error, fix} contract, and never leaks the exception message."""
    def submit(prop, sitemap_url, provider, **kwargs):
        raise RuntimeError("token=ya29.LEAK")

    monkeypatch.setattr(server.api, "submit_sitemap", submit)
    out = server.gsc_submit_sitemaps(["https://example.com/mine.xml"])
    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "RuntimeError"
    assert out["fix"]
    assert "ya29.LEAK" not in repr(out)


def test_the_reread_happens_inside_the_write_transaction(home, monkeypatch):
    """Re-reading is only half the fix; WHERE it happens is the other half.

    store.tx() issues BEGIN IMMEDIATE, so taking the write lock first is
    what stops another writer committing between the re-read and the
    upserts -- re-reading just before opening the transaction reintroduces
    the same lost update through a narrower window. Concurrency alone
    cannot pin this (the correct version holds the lock, so a competing
    writer blocks rather than racing), so this asserts the ordering
    directly: the snapshot read is in autocommit, the re-read is not.
    """
    seen_in_transaction: list[bool] = []
    real_get_sites = store.get_sites

    def watched(conn):
        seen_in_transaction.append(bool(conn.in_transaction))
        return real_get_sites(conn)

    monkeypatch.setattr(server.store, "get_sites", watched)
    monkeypatch.setattr(server.api, "submit_sitemap",
                        lambda prop, sm, provider, **k: _ok(prop, sm))
    server.gsc_submit_sitemaps(["https://example.com/mine.xml"])

    assert seen_in_transaction == [False, True], (
        "expected the pre-network snapshot in autocommit and the re-read "
        f"inside the write transaction, got {seen_in_transaction}")
