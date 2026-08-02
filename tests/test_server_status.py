from datetime import UTC, datetime, timedelta

import pytest

from gsc_core import config, gauth, quota, store
from gsc_mcp import server


class _AuthedProvider:
    """A minimal stand-in for gauth.TokenProvider: enough surface for code
    that calls access_token() to treat it as signed in. A bare object()
    used to serve this role in these tests, but it has no access_token()
    method at all -- fine before gsc_check_status probed for one, but a
    silent AttributeError trap once it started doing so for real (Task 8
    review, Finding 1/2)."""

    def access_token(self) -> str:
        return "token"


class _SignedOutProvider:
    """A stand-in for gauth.TokenProvider with no stored credentials --
    what deps.provider() actually hands back when a client is configured
    but nobody has ever run the consent flow. AuthRequired only ever
    surfaces from access_token(), never from provider() itself."""

    def access_token(self) -> str:
        raise gauth.AuthRequired("no stored credentials; run gsc_setup()")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    with store.session() as conn, store.tx(conn):
        store.upsert_site(conn, "sc-domain:example.com", "example.com", "siteOwner", [])
    return tmp_path


@pytest.fixture()
def empty_home(tmp_path, monkeypatch):
    """Same as `home`, but the store is left empty -- the fresh-install,
    nothing-synced-yet case Finding 1 was about."""
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    return tmp_path


def _record_quota_slot(conn, property, when, account="user@example.com"):
    conn.execute(
        "INSERT INTO quota_slots (account, property, used_at) VALUES (?, ?, ?)",
        (account, property, store.utc_iso(when)),
    )


def test_check_status_delegates_and_returns_rows(home, monkeypatch):
    monkeypatch.setattr(server.deps, "provider", lambda: _AuthedProvider())
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

    monkeypatch.setattr(server.deps, "provider", lambda: _AuthedProvider())
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


def test_check_status_reports_not_configured_as_data_not_an_exception(
    home, monkeypatch
):
    def boom(*a, **k):
        raise server.deps.NotConfigured("no client")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_check_status(["https://example.com/a"])
    assert out["ok"] is False
    assert out["error"] == "not_configured"
    assert out["fix"]


def test_check_status_reports_auth_required_before_syncing_an_empty_store(
    empty_home, monkeypatch
):
    """Finding 1: deps.provider() only constructs a TokenProvider and never
    raises AuthRequired itself -- that happened lazily, inside
    api.list_properties() (the empty-store sync), OUTSIDE the original
    try/except. A fresh install with a configured client but no token used
    to raise AuthRequired straight across the MCP boundary here instead of
    answering with the structured dict this tool's docstring promises.
    """
    monkeypatch.setattr(server.deps, "provider", lambda: _SignedOutProvider())

    def must_not_run(*a, **k):
        raise AssertionError("list_properties must not run before the auth check")

    monkeypatch.setattr(server.api, "list_properties", must_not_run)

    out = server.gsc_check_status(["https://example.com/a"])
    assert out["ok"] is False
    assert out["error"] == "auth_required"
    assert out["fix"]


def test_check_status_reports_auth_required_with_a_populated_store(
    home, monkeypatch
):
    """Finding 2: with a populated store the empty-store sync never runs,
    so api.check_status() is reached directly -- and it swallows a
    per-URL AuthRequired into an "error" status row rather than raising
    (one bad row must not abort the batch, by design). A signed-out user
    used to get back a normal-shaped result full of fabricated per-URL
    "error" rows instead of the structured auth_required answer.
    """
    monkeypatch.setattr(server.deps, "provider", lambda: _SignedOutProvider())

    def must_not_run(*a, **k):
        raise AssertionError(
            "check_status must not run any inspection while signed out")

    monkeypatch.setattr(server.api, "check_status", must_not_run)

    out = server.gsc_check_status(["https://example.com/a"])
    assert out["ok"] is False
    assert out["error"] == "auth_required"
    assert out["fix"]


def test_check_status_syncs_properties_when_the_store_is_empty(
    empty_home, monkeypatch
):
    """The empty-store fallback itself, positively: it must still run (and
    persist) for a signed-in caller -- Finding 3's "empty-store sync
    deleted entirely" mutation."""
    monkeypatch.setattr(server.deps, "provider", lambda: _AuthedProvider())
    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [
        {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}])

    seen = {}

    def spy(conn, urls, provider, properties, **kwargs):
        seen["properties"] = properties
        return {"rows": [], "checked": 0, "skipped_quota": [], "quota": {}}

    monkeypatch.setattr(server.api, "check_status", spy)

    server.gsc_check_status(["https://example.com/a"])

    assert seen["properties"] == ["sc-domain:example.com"]
    with store.session() as conn:
        assert store.get_sites(conn)[0]["property"] == "sc-domain:example.com"


def test_check_status_concurrency_defaults_from_config(home, monkeypatch):
    """Finding 3's "concurrency default replaced by 1" mutation: pin the
    real configured default (8), not just "some value"."""
    monkeypatch.setattr(server.deps, "provider", lambda: _AuthedProvider())
    seen = {}

    def spy(conn, urls, provider, properties, **kwargs):
        seen["concurrency"] = kwargs.get("concurrency")
        return {"rows": [], "checked": 0, "skipped_quota": [], "quota": {}}

    monkeypatch.setattr(server.api, "check_status", spy)

    server.gsc_check_status(["https://example.com/a"])

    assert seen["concurrency"] == config.DEFAULTS["inspect_concurrency"]


def test_check_status_concurrency_override_is_passed_through(home, monkeypatch):
    monkeypatch.setattr(server.deps, "provider", lambda: _AuthedProvider())
    seen = {}

    def spy(conn, urls, provider, properties, **kwargs):
        seen["concurrency"] = kwargs.get("concurrency")
        return {"rows": [], "checked": 0, "skipped_quota": [], "quota": {}}

    monkeypatch.setattr(server.api, "check_status", spy)

    server.gsc_check_status(["https://example.com/a"], concurrency=3)

    assert seen["concurrency"] == 3


def test_check_status_passes_unverified_flag_through_untouched(home, monkeypatch):
    """Task 4 spent two fix rounds getting `unverified` right; nothing at
    this layer should ever touch it. Finding 3's "unverified popped from
    every row" mutation."""
    monkeypatch.setattr(server.deps, "provider", lambda: _AuthedProvider())
    monkeypatch.setattr(server.api, "check_status", lambda *a, **k: {
        "rows": [{"url": "https://example.com/a", "status": "unknown_to_google",
                  "detail": "not re-verified: re-check quota exhausted",
                  "unverified": True}],
        "checked": 1, "skipped_quota": [], "quota": {}})

    out = server.gsc_check_status(["https://example.com/a"])
    assert out["rows"][0]["unverified"] is True


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
    """Not just key presence (reserve=0 can't tell "surfaced" from
    "hardcoded to 0" apart) -- configure a real reserve and check the
    value round-trips."""
    config.save({**config.DEFAULTS, "daily_reserve": 3})
    submission = server.gsc_quota()[0]["submission"]
    assert submission["daily_reserve"] == 3


def test_quota_spendable_free_and_used_reflect_actual_spend(home):
    """Finding 3's "spendable_free set to raw free" and "used forced to 0"
    mutations: a nonzero reserve plus real spend is the only way either
    number can be told apart from the other/from zero."""
    config.save({**config.DEFAULTS, "daily_reserve": 2})
    with store.session() as conn, store.tx(conn):
        for _ in range(3):
            _record_quota_slot(conn, "sc-domain:example.com", datetime.now(UTC))

    submission = server.gsc_quota()[0]["submission"]
    assert submission["used"] == 3
    assert submission["free"] == 8          # 11 raw slots - 3 used
    assert submission["spendable_free"] == 6  # (11 - 2 reserve) - 3 used


def test_quota_reports_submission_binding_and_next_free_at_when_exhausted(home):
    """Finding 3's "binding == 'submission' replaced by None" and
    "next_free_at forced to None" mutations."""
    with store.session() as conn, store.tx(conn):
        for _ in range(quota.DEFAULT_PROPERTY_SLOTS):
            _record_quota_slot(conn, "sc-domain:example.com",
                               datetime.now(UTC) - timedelta(minutes=5))

    entry = server.gsc_quota()[0]
    assert entry["binding"] == "submission"
    assert entry["submission"]["next_free_at"] is not None


def test_quota_reports_the_real_inspection_free_counts(home):
    """Finding 3's "daily_free forced to 0" mutation."""
    inspection = server.gsc_quota()[0]["inspection"]
    assert inspection["daily_free"] == quota.DAILY_INSPECTION_LIMIT
    assert inspection["minute_free"] == quota.MINUTE_INSPECTION_LIMIT
