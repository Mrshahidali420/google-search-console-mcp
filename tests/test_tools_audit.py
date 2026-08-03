"""The MCP-facing layer for gsc_find_unindexed and gsc_audit.

Two properties are load-bearing here and are tested for directly rather
than incidentally: neither tool ever raises out to the protocol, and no
exception MESSAGE ever reaches the returned envelope (a message can carry
a filesystem path containing the operator's account name).
"""
from __future__ import annotations

from gsc_core import api, gauth
from gsc_mcp import deps, tools_audit

PROPERTY = "https://example.com/"

# A message shaped like the ones that must never escape: it carries an
# absolute path with an account name in it. Every "unexpected" test raises
# an exception carrying this and asserts the envelope is clean of it.
LEAKY = r"C:\Users\someone\AppData\gsc-mcp\store.db is locked"


def _ok_provider():
    class _P:
        def access_token(self) -> str:
            return "token"
    return _P()


def _configured(monkeypatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties", lambda provider: [PROPERTY])


def _assert_clean(out: dict) -> None:
    """No fragment of a leaked message survives anywhere in the envelope."""
    blob = repr(out)
    for secret in ("Users", "someone", "AppData", "store.db", "locked",
                   LEAKY):
        assert secret not in blob


# --------------------------------------------------------------- find_unindexed


def test_find_unindexed_refuses_when_no_oauth_client_is_configured(
        home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client",
                        lambda: (_ for _ in ()).throw(deps.NotConfigured()))

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "not_configured"
    assert out["fix"]


def test_find_unindexed_probes_the_token_before_fetching_a_sitemap(
        home, monkeypatch):
    # api.check_status never raises AuthRequired -- a 401 becomes an
    # "error" row. Without an eager probe a signed-out caller gets a
    # normal-shaped result full of fabricated rows.
    fetched: list[str] = []

    class _Provider:
        def access_token(self) -> str:
            raise gauth.AuthRequired()

    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _Provider())
    monkeypatch.setattr(
        tools_audit.discovery, "find_unindexed",
        lambda *a, **k: fetched.append("fetched") or {})

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "auth_required"
    assert fetched == []


def test_find_unindexed_refuses_an_unknown_source(home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, source="everything")

    assert out["ok"] is False
    assert out["error"] == "bad_source"
    assert "sitemap" in out["fix"]


def test_find_unindexed_refuses_a_non_positive_limit(home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, limit=0)

    assert out["ok"] is False
    assert out["error"] == "bad_limit"


def test_find_unindexed_refuses_a_negative_limit(home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, limit=-3)

    assert out["ok"] is False
    assert out["error"] == "bad_limit"


def test_find_unindexed_refuses_a_boolean_limit(home, monkeypatch):
    # bool is a subclass of int, and True would sail through a naive
    # `limit < 1` check and silently cap the run at one URL.
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, limit=True)

    assert out["ok"] is False
    assert out["error"] == "bad_limit"


def test_find_unindexed_validates_the_source_before_touching_credentials(
        home, monkeypatch):
    # A typo must not cost a token load or an API round trip.
    def _boom():
        raise AssertionError("credentials were touched")

    monkeypatch.setattr(deps, "oauth_client", _boom)

    out = tools_audit.find_unindexed(PROPERTY, source="everything")

    assert out["error"] == "bad_source"


def test_find_unindexed_refuses_a_property_the_account_does_not_have(
        home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties", lambda provider: [])

    out = tools_audit.find_unindexed("https://not-yours.example.net/")

    assert out["ok"] is False
    assert out["error"] == "unknown_property"


def test_find_unindexed_reports_an_api_error_with_its_status(
        home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())

    def _refuse(provider):
        raise api.ApiError("refused", status=403)

    monkeypatch.setattr(api, "list_properties", _refuse)

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "api_error"
    assert out["status"] == 403
    assert out["fix"]
    # api.ApiError subclasses RuntimeError, as does deps.NotConfigured --
    # a mis-ordered ladder reports one as the other.
    assert "detail" not in out


def test_an_unexpected_failure_reports_a_type_name_never_a_message(
        home, monkeypatch):
    class _Boom(Exception):
        pass

    _configured(monkeypatch)

    def _explode(*args, **kwargs):
        raise _Boom(LEAKY)

    monkeypatch.setattr(tools_audit.discovery, "find_unindexed", _explode)

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "_Boom"
    _assert_clean(out)


def test_find_unindexed_survives_an_oserror_from_the_store(home, monkeypatch):
    # An OSError's message carries a filesystem path. This is the concrete
    # case the type-name-only rule exists for, and it must return rather
    # than raise out to the protocol.
    _configured(monkeypatch)

    def _explode():
        raise OSError(LEAKY)

    monkeypatch.setattr(deps, "connection", _explode)

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "OSError"
    _assert_clean(out)


def test_find_unindexed_passes_the_configured_concurrency_and_ttl(
        home, monkeypatch):
    seen: dict = {}

    _configured(monkeypatch)

    def _spy(conn, property, provider, properties, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(tools_audit.discovery, "find_unindexed", _spy)

    tools_audit.find_unindexed(PROPERTY, source="store", limit=5)

    assert seen["source"] == "store"
    assert seen["limit"] == 5
    assert seen["ttl_days"] == 7
    assert seen["concurrency"] == 8


def test_find_unindexed_passes_the_property_and_the_property_list(
        home, monkeypatch):
    seen: dict = {}

    _configured(monkeypatch)

    def _spy(conn, property, provider, properties, **kwargs):
        seen["property"] = property
        seen["properties"] = properties
        return {"ok": True, "property": property}

    monkeypatch.setattr(tools_audit.discovery, "find_unindexed", _spy)

    out = tools_audit.find_unindexed(PROPERTY)

    assert seen["property"] == PROPERTY
    assert seen["properties"] == [PROPERTY]
    assert out["ok"] is True


def test_find_unindexed_returns_the_core_result_unchanged(home, monkeypatch):
    payload = {"ok": True, "property": PROPERTY, "candidates_total": 3,
               "quota": {}}

    _configured(monkeypatch)
    monkeypatch.setattr(tools_audit.discovery, "find_unindexed",
                        lambda *a, **k: payload)

    assert tools_audit.find_unindexed(PROPERTY) == payload


# ----------------------------------------------------------------------- audit


def test_audit_refuses_when_no_oauth_client_is_configured(home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client",
                        lambda: (_ for _ in ()).throw(deps.NotConfigured()))

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "not_configured"
    assert out["fix"]


def test_audit_reports_auth_required_when_the_token_is_gone(
        home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())

    def _refuse(provider):
        raise gauth.AuthRequired()

    monkeypatch.setattr(api, "list_properties", _refuse)

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "auth_required"
    assert out["fix"]


def test_audit_reports_an_api_error_with_its_status(home, monkeypatch):
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())

    def _refuse(provider):
        raise api.ApiError("refused", status=500)

    monkeypatch.setattr(api, "list_properties", _refuse)

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "api_error"
    assert out["status"] == 500


def test_audit_needs_no_token_probe_but_still_refuses_an_unknown_property(
        home, monkeypatch):
    # gsc_audit reads the store only -- no HTTP, no quota. It still has to
    # know the property exists, or it would return a confident all-zeros
    # audit for a typo.
    _configured(monkeypatch)

    out = tools_audit.audit("https://typo.example.net/")

    assert out["ok"] is False
    assert out["error"] == "unknown_property"


def test_audit_reports_an_unexpected_failure_by_type_name_only(
        home, monkeypatch):
    _configured(monkeypatch)

    def _explode(*args, **kwargs):
        raise OSError(LEAKY)

    monkeypatch.setattr(tools_audit.audit_core, "audit", _explode)

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "OSError"
    _assert_clean(out)


def test_audit_returns_the_point_in_time_payload(home, monkeypatch):
    _configured(monkeypatch)

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is True
    assert out["basis"] == "point_in_time"
    assert out["property"] == PROPERTY


def test_audit_passes_the_configured_ttl(home, monkeypatch):
    seen: dict = {}

    _configured(monkeypatch)

    def _spy(conn, property, **kwargs):
        seen.update(kwargs)
        seen["property"] = property
        return {"ok": True}

    monkeypatch.setattr(tools_audit.audit_core, "audit", _spy)

    tools_audit.audit(PROPERTY)

    assert seen["ttl_days"] == 7
    assert seen["property"] == PROPERTY
