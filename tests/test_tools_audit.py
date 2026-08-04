"""The MCP-facing layer for gsc_find_unindexed and gsc_audit.

Three properties are load-bearing here and are tested for directly rather
than incidentally: neither tool ever raises out to the protocol, no
exception MESSAGE reaches the returned envelope, and no exception message
reaches a LOG LINE either. The last one is easy to leave untested — the
envelope is the visible half — but a log file is shipped, and an OSError's
message is a filesystem path carrying the operator's account name.

Every caplog assertion below is paired with a POSITIVE CONTROL asserting
something that must be present. Without it a negative assertion over an
empty buffer passes vacuously, which is worthless: it would stay green if
the tool logged nothing at all, or if capture were not live at all.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gsc_core import api, gauth
from gsc_mcp import deps, tools_audit

PROPERTY = "https://example.com/"

# The shape api.list_properties ACTUALLY returns — entries straight from
# Google, not bare URLs. This constant exists because the fake here used to
# return `[PROPERTY]`, and that one-character-cheaper lie kept the whole
# module green while both tools were refusing every property in
# production: `"https://example.com/" not in [{...}]` is always True.
# Fake the shape the real function documents, never the shape the caller
# wishes it had.
def _entries(*properties: str) -> list[dict]:
    return [{"siteUrl": p, "permissionLevel": "siteOwner"} for p in properties]

# A message shaped like the ones that must never escape: it carries an
# absolute path with an account name in it. Every failure-path test raises
# an exception carrying this and asserts both the envelope and the log are
# clean of it.
LEAKY = r"C:\Users\someone\AppData\gsc-mcp\store.db is locked"

# The fragments of LEAKY that must never appear anywhere. Checked
# piecewise rather than whole so a partially-formatted or truncated leak
# is caught too.
_SECRETS = ("Users", "someone", "AppData", "store.db", "locked", LEAKY)


def _ok_provider() -> Any:
    class _P:
        def access_token(self) -> str:
            return "token"
    return _P()


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties",
                        lambda provider: _entries(PROPERTY))


def _assert_clean(out: dict) -> None:
    """No fragment of a leaked message survives anywhere in the envelope."""
    blob = repr(out)
    for secret in _SECRETS:
        assert secret not in blob


def _assert_log_clean(caplog: pytest.LogCaptureFixture, present: str) -> None:
    """No fragment of a leaked message survives anywhere in the log.

    `present` is the positive control: something the tool is required to
    have logged. It is asserted FIRST, so that if capture is not live —
    the gsc root logger sets propagate = False (runlog.py:24) — this fails
    loudly instead of letting the negative assertions below pass on an
    empty buffer.
    """
    assert present in caplog.text
    for secret in _SECRETS:
        assert secret not in caplog.text


def _raiser(exc: BaseException) -> Any:
    """A zero-argument callable that raises, for stubbing deps.connection."""
    def _boom() -> Any:
        raise exc
    return _boom


def _raising_lookup(exc: BaseException) -> Any:
    """A stub api.list_properties that always raises `exc`."""
    def _refuse(provider: Any) -> Any:
        raise exc
    return _refuse


# --------------------------------------------------------------- find_unindexed


def test_find_unindexed_refuses_when_no_oauth_client_is_configured(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client",
                        lambda: (_ for _ in ()).throw(deps.NotConfigured()))

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "not_configured"
    assert out["fix"]


def test_the_not_configured_fix_names_the_tool_that_resolves_it(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # deps.NotConfigured means there is no OAuth client to sign in WITH.
    # gsc_setup is the answer: onboarding._setup catches this same exception
    # and downloads the bundled client (onboarding.py:243-248), so the caller
    # is not being sent round a circle. The env vars stay named as the
    # alternative for anyone using their own client.
    # `assert out["fix"]` alone is truthy-only and cannot see either half.
    monkeypatch.setattr(deps, "oauth_client",
                        lambda: (_ for _ in ()).throw(deps.NotConfigured()))

    for out in (tools_audit.find_unindexed(PROPERTY),
                tools_audit.audit(PROPERTY)):
        assert "gsc_setup" in out["fix"]
        assert "GSC_MCP_CLIENT_ID" in out["fix"]
        assert "GSC_MCP_CLIENT_SECRET" in out["fix"]


def test_find_unindexed_probes_the_token_before_fetching_a_sitemap(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # api.check_status never raises AuthRequired -- a 401 becomes an
    # "error" row. Without an eager probe a signed-out caller gets a
    # normal-shaped result full of fabricated rows.
    #
    # api.list_properties is stubbed to SUCCEED here on purpose. Left real
    # it would call access_token() itself and raise, so the test would go
    # green whether or not the eager probe exists -- it would pin the
    # ladder, not the ordering. With it stubbed, the only thing standing
    # between a signed-out caller and the sitemap fetch is the probe.
    fetched: list[str] = []

    class _Provider:
        def access_token(self) -> str:
            raise gauth.AuthRequired()

    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _Provider())
    monkeypatch.setattr(api, "list_properties",
                        lambda provider: _entries(PROPERTY))
    monkeypatch.setattr(
        tools_audit.discovery, "find_unindexed",
        lambda *a, **k: fetched.append("fetched") or {})

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "auth_required"
    assert fetched == []


def test_find_unindexed_refuses_an_unknown_source(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, source="everything")

    assert out["ok"] is False
    assert out["error"] == "bad_source"
    assert "sitemap" in out["fix"]


def test_find_unindexed_refuses_a_non_positive_limit(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, limit=0)

    assert out["ok"] is False
    assert out["error"] == "bad_limit"


def test_find_unindexed_refuses_a_negative_limit(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, limit=-3)

    assert out["ok"] is False
    assert out["error"] == "bad_limit"


def test_find_unindexed_refuses_a_boolean_limit(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # bool is a subclass of int, and True would sail through a naive
    # `limit < 1` check and silently cap the run at one URL.
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))

    out = tools_audit.find_unindexed(PROPERTY, limit=True)

    assert out["ok"] is False
    assert out["error"] == "bad_limit"


def test_find_unindexed_validates_the_source_before_touching_credentials(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A typo must not cost a token load or an API round trip.
    def _boom() -> Any:
        raise AssertionError("credentials were touched")

    monkeypatch.setattr(deps, "oauth_client", _boom)

    out = tools_audit.find_unindexed(PROPERTY, source="everything")

    assert out["error"] == "bad_source"


def test_find_unindexed_refuses_a_property_the_account_does_not_have(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties", lambda provider: [])

    out = tools_audit.find_unindexed("https://not-yours.example.net/")

    assert out["ok"] is False
    assert out["error"] == "unknown_property"


def test_find_unindexed_reports_an_api_error_with_its_status(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties",
                        _raising_lookup(api.ApiError("refused", status=403)))

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "api_error"
    assert out["status"] == 403
    assert out["fix"]
    # api.ApiError subclasses RuntimeError, as does deps.NotConfigured --
    # a mis-ordered ladder reports one as the other.
    assert "detail" not in out


def test_an_unexpected_failure_reports_a_type_name_never_a_message(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(Exception):
        pass

    _configured(monkeypatch)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise _Boom(LEAKY)

    monkeypatch.setattr(tools_audit.discovery, "find_unindexed", _explode)

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "_Boom"
    _assert_clean(out)


def test_find_unindexed_survives_an_oserror_from_the_store(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # An OSError's message carries a filesystem path. This is the concrete
    # case the type-name-only rule exists for, and it must return rather
    # than raise out to the protocol.
    _configured(monkeypatch)
    monkeypatch.setattr(deps, "connection", _raiser(OSError(LEAKY)))

    out = tools_audit.find_unindexed(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "OSError"
    _assert_clean(out)


def test_find_unindexed_passes_the_configured_concurrency_and_ttl(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    _configured(monkeypatch)

    def _spy(conn: Any, property: str, provider: Any,
             properties: list[str], **kwargs: Any) -> dict:
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(tools_audit.discovery, "find_unindexed", _spy)

    tools_audit.find_unindexed(PROPERTY, source="store", limit=5)

    assert seen["source"] == "store"
    assert seen["limit"] == 5
    assert seen["ttl_days"] == 7
    assert seen["concurrency"] == 8


def test_find_unindexed_passes_the_property_and_the_property_list(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    _configured(monkeypatch)

    def _spy(conn: Any, property: str, provider: Any,
             properties: list[str], **kwargs: Any) -> dict:
        seen["property"] = property
        seen["properties"] = properties
        return {"ok": True, "property": property}

    monkeypatch.setattr(tools_audit.discovery, "find_unindexed", _spy)

    out = tools_audit.find_unindexed(PROPERTY)

    assert seen["property"] == PROPERTY
    assert seen["properties"] == [PROPERTY]
    assert out["ok"] is True


def test_find_unindexed_returns_the_core_result_unchanged(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"ok": True, "property": PROPERTY, "candidates_total": 3,
               "quota": {}}

    _configured(monkeypatch)
    monkeypatch.setattr(tools_audit.discovery, "find_unindexed",
                        lambda *a, **k: payload)

    assert tools_audit.find_unindexed(PROPERTY) == payload


# ----------------------------------------------------------------------- audit


def test_audit_refuses_when_no_oauth_client_is_configured(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client",
                        lambda: (_ for _ in ()).throw(deps.NotConfigured()))

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "not_configured"
    assert out["fix"]


def test_audit_reports_auth_required_when_the_token_is_gone(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties",
                        _raising_lookup(gauth.AuthRequired()))

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "auth_required"
    assert out["fix"]


def test_audit_reports_an_api_error_with_its_status(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties",
                        _raising_lookup(api.ApiError("refused", status=500)))

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "api_error"
    assert out["status"] == 500


def test_audit_needs_no_token_probe_but_still_refuses_an_unknown_property(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # gsc_audit reads the store only -- no HTTP, no quota. It still has to
    # know the property exists, or it would return a confident all-zeros
    # audit for a typo.
    _configured(monkeypatch)

    out = tools_audit.audit("https://typo.example.net/")

    assert out["ok"] is False
    assert out["error"] == "unknown_property"


def test_audit_reports_an_unexpected_failure_by_type_name_only(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise OSError(LEAKY)

    monkeypatch.setattr(tools_audit.audit_core, "audit", _explode)

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "OSError"
    _assert_clean(out)


def test_audit_returns_the_point_in_time_payload(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configured(monkeypatch)

    out = tools_audit.audit(PROPERTY)

    assert out["ok"] is True
    assert out["basis"] == "point_in_time"
    assert out["property"] == PROPERTY


def test_audit_passes_the_configured_ttl(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    _configured(monkeypatch)

    def _spy(conn: Any, property: str, **kwargs: Any) -> dict:
        seen.update(kwargs)
        seen["property"] = property
        return {"ok": True}

    monkeypatch.setattr(tools_audit.audit_core, "audit", _spy)

    tools_audit.audit(PROPERTY)

    assert seen["ttl_days"] == 7
    assert seen["property"] == PROPERTY


# ------------------------------------------------------- the log leg of no-leak
#
# The tests above cover only what the CALLER sees. Log files are shipped,
# read by whoever is debugging, and pasted into issues, so the constraint
# is broader than the envelope: no path, address, or token may reach a log
# line AT ANY LEVEL. Every arm of the ladder writes one line, and none of
# those lines may carry the exception's message. Each test below raises
# with LEAKY and asserts the arm's own wording is present before asserting
# the message is not.


def test_find_unindexed_logs_a_failure_by_type_name_never_its_message(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    _configured(monkeypatch)
    monkeypatch.setattr(deps, "connection", _raiser(OSError(LEAKY)))

    with caplog.at_level("DEBUG"):
        tools_audit.find_unindexed(PROPERTY)

    _assert_log_clean(caplog, "OSError")


def test_audit_logs_a_failure_by_type_name_never_its_message(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    _configured(monkeypatch)
    monkeypatch.setattr(deps, "connection", _raiser(OSError(LEAKY)))

    with caplog.at_level("DEBUG"):
        tools_audit.audit(PROPERTY)

    _assert_log_clean(caplog, "OSError")


def test_the_not_configured_arm_logs_nothing_from_the_exception(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    # deps.NotConfigured comes from the layer that reads configuration, so
    # its message is exactly as dangerous as an OSError's.
    monkeypatch.setattr(
        deps, "oauth_client",
        lambda: (_ for _ in ()).throw(deps.NotConfigured(LEAKY)))

    with caplog.at_level("DEBUG"):
        tools_audit.find_unindexed(PROPERTY)
        tools_audit.audit(PROPERTY)

    _assert_log_clean(caplog, "no OAuth client configured")


def test_the_auth_required_arm_logs_nothing_from_the_exception(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties",
                        _raising_lookup(gauth.AuthRequired(LEAKY)))

    with caplog.at_level("DEBUG"):
        tools_audit.find_unindexed(PROPERTY)
        tools_audit.audit(PROPERTY)

    _assert_log_clean(caplog, "no usable token")


def test_the_api_error_arm_logs_the_status_and_nothing_else(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    # ApiError's own docstring promises its text never holds a response
    # body -- but that is a promise made by a different module, and this
    # layer is the one whose log is shipped. It records the status number
    # and nothing more.
    monkeypatch.setattr(deps, "oauth_client", lambda: ("id", "secret"))
    monkeypatch.setattr(deps, "provider", lambda: _ok_provider())
    monkeypatch.setattr(api, "list_properties",
                        _raising_lookup(api.ApiError(LEAKY, status=503)))

    with caplog.at_level("DEBUG"):
        tools_audit.find_unindexed(PROPERTY)
        tools_audit.audit(PROPERTY)

    _assert_log_clean(caplog, "503")


# ------------------------------------------- the shape the stub must not lie about

def test_the_stubbed_property_list_matches_what_the_api_really_returns():
    """The guard for the defect this module shipped with.

    Every test above stubs `api.list_properties`, and a stub is worth only
    what its SHAPE is worth. This one drives the real function over a fake
    HTTP response and asserts it produces exactly what `_entries` produces,
    so a stub that drifts back to a list of bare URLs fails here — in a
    named test about shape — rather than in production, where the only
    symptom was `unknown_property` for every property the account owns and
    the suite stayed green throughout.
    """
    from test_api import FakeProvider, FakeResponse, FakeSession

    session = FakeSession(FakeResponse(200, {"siteEntry": _entries(PROPERTY)}))
    assert api.list_properties(FakeProvider(), session=session) == _entries(PROPERTY)


@pytest.mark.parametrize("call", ("find_unindexed", "audit"))
def test_a_property_the_account_owns_is_never_reported_unknown(
        home: Path, monkeypatch: pytest.MonkeyPatch, call: str) -> None:
    """The production symptom, asserted directly.

    `site not in properties` against a list of dicts is valid Python and
    quietly always True, so both tools refused every property that existed.
    Nothing downstream of the check can be reached while it is wrong, which
    is why this asserts the negative — not that the tool succeeded, but
    that it did not stop at the gate.
    """
    _configured(monkeypatch)
    # Stopped one step PAST the gate on purpose: reaching the store is not
    # what is under test, getting through the property check is.
    monkeypatch.setattr(deps, "connection", _raiser(OSError("stopped here")))

    out = getattr(tools_audit, call)(PROPERTY)

    assert out.get("error") != "unknown_property"
