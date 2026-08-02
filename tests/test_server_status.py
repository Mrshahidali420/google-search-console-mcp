import pytest

from gsc_core import quota, store
from gsc_mcp import server


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.com", "example.com", "siteOwner", [])
    return tmp_path


def test_check_status_delegates_and_returns_rows(home, monkeypatch):
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    monkeypatch.setattr(server.api, "check_status", lambda *a, **k: {
        "rows": [{"url": "https://example.com/a", "status": "indexed", "detail": ""}],
        "checked": 1, "skipped_quota": [], "quota": {}})
    out = server.gsc_check_status(["https://example.com/a"])
    assert out["rows"][0]["status"] == "indexed"


def test_check_status_passes_the_stores_properties_through(home, monkeypatch):
    seen = {}

    def spy(conn, urls, provider, properties, **kwargs):
        seen["properties"] = properties
        return {"rows": [], "checked": 0, "skipped_quota": [], "quota": {}}

    monkeypatch.setattr(server.deps, "provider", lambda: object())
    monkeypatch.setattr(server.api, "check_status", spy)
    server.gsc_check_status(["https://example.com/a"])
    assert seen["properties"] == ["sc-domain:example.com"]


def test_check_status_docstring_names_every_status():
    doc = server.gsc_check_status.__doc__ or ""
    for status in ("indexed", "crawled_not_indexed", "discovered_not_indexed",
                   "unknown_to_google", "redirect", "noindex", "duplicate",
                   "alternate_canonical", "not_found", "soft_404",
                   "blocked_robots", "no_property", "error"):
        assert status in doc, f"{status} missing from the model-facing docstring"


def test_check_status_docstring_says_it_does_not_submit():
    doc = (server.gsc_check_status.__doc__ or "").lower()
    assert "submit" in doc


def test_quota_reports_both_budgets_per_property(home):
    out = server.gsc_quota()
    assert out[0]["property"] == "sc-domain:example.com"
    assert "submission" in out[0]
    assert "inspection" in out[0]


def test_quota_binding_is_none_when_everything_has_headroom(home):
    assert server.gsc_quota()[0]["binding"] is None


def test_quota_reports_inspection_daily_as_binding_when_exhausted(home):
    with store.session() as conn, store.tx(conn):
        quota.record_inspections(conn, "sc-domain:example.com",
                                 quota.DAILY_INSPECTION_LIMIT)
    assert server.gsc_quota()[0]["binding"] == "inspection_daily"


def test_quota_surfaces_the_configured_daily_reserve(home):
    assert "daily_reserve" in server.gsc_quota()[0]["submission"]
