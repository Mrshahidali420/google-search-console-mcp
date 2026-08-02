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
