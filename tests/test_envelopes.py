"""The shared refusal envelope: one definition, and a parameter where the
tools genuinely differ.

These tests exist because the copies drifted once already. Asserting the
shape of a dict would not have caught that -- both copies had the right
shape and different words. What catches it is asserting that the modules
resolve to the SAME OBJECT, which only holds while there is one definition.
"""
from __future__ import annotations

import logging

import pytest

from gsc_core import api
from gsc_mcp import envelopes, server, tools_audit, tools_submit


def test_refuse_builds_the_four_key_envelope() -> None:
    out = envelopes.refuse("no_urls", "no URLs were given", "pass some")

    assert out == {"ok": False, "error": "no_urls",
                   "detail": "no URLs were given", "fix": "pass some"}


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, None])
def test_api_fix_uses_the_generic_remedy_for_everything_but_403(
        status: int | None) -> None:
    assert envelopes.api_fix(status, "the 403 remedy") == envelopes.FIX_API


def test_api_fix_takes_the_403_remedy_from_its_caller() -> None:
    # The whole reason `forbidden` is a parameter: 403 means "wrong
    # property" to a tool that took a site, and "no properties or no
    # scope" to one that did not. A constant here would force one of the
    # two tools to give the wrong next step.
    assert envelopes.api_fix(403, "the 403 remedy") == "the 403 remedy"


def test_the_two_tool_families_pass_different_403_remedies() -> None:
    # tools_audit's tools take a `site`; server.py's do not. If these ever
    # collapse to one string, the parameter has stopped earning itself and
    # one of the two tools is now lying about the next step.
    assert tools_audit._FORBIDDEN == envelopes.FIX_UNKNOWN_PROPERTY
    assert envelopes.FIX_UNKNOWN_PROPERTY != envelopes.FIX_PROPERTIES


def test_api_error_reports_the_status_and_no_response_body(
        caplog: pytest.LogCaptureFixture) -> None:
    exc = api.ApiError("https://example.com/ returned 403", status=403)

    with caplog.at_level(logging.WARNING):
        out = envelopes.api_error("gsc_audit", exc, envelopes.FIX_UNKNOWN_PROPERTY)

    assert out == {"ok": False, "error": "api_error", "status": 403,
                   "fix": envelopes.FIX_UNKNOWN_PROPERTY}
    assert "example.com" not in caplog.text
    assert "403" in caplog.text


def test_unexpected_records_the_type_name_and_never_the_message(
        caplog: pytest.LogCaptureFixture) -> None:
    exc = OSError(r"C:\Users\a-real-person\AppData\gsc.sqlite3 is locked")

    with caplog.at_level(logging.WARNING):
        out = envelopes.unexpected("gsc_audit", exc)

    assert out == {"ok": False, "error": "unexpected", "detail": "OSError",
                   "fix": envelopes.FIX_UNEXPECTED}
    assert "a-real-person" not in caplog.text
    assert "a-real-person" not in repr(out)


def test_unexpected_accepts_a_caller_supplied_fix() -> None:
    out = envelopes.unexpected("gsc_detect_browsers", ValueError(), "install one")

    assert out["fix"] == "install one"
    assert out["detail"] == "ValueError"


def test_every_consumer_resolves_to_the_same_strings() -> None:
    # `is`, not `==`: equality would still pass if someone re-declared a
    # constant locally with the same words, and the next edit to one of
    # them is exactly how the drift started. There must be one object.
    for module in (server, tools_audit, tools_submit):
        assert module.envelopes.FIX_TOKEN is envelopes.FIX_TOKEN
        assert module.envelopes.FIX_OAUTH_CLIENT is envelopes.FIX_OAUTH_CLIENT
        assert module.envelopes.FIX_UNEXPECTED is envelopes.FIX_UNEXPECTED


def test_no_module_keeps_a_private_copy_of_a_shared_name() -> None:
    # The extraction is only complete while these names are absent. A
    # re-introduced server._FIX_TOKEN would not fail any other test here:
    # the tools would keep working, with two strings again.
    for module in (server, tools_audit, tools_submit):
        for name in ("_FIX_TOKEN", "_FIX_OAUTH_CLIENT", "_FIX_API",
                     "_FIX_PROPERTIES", "_FIX_UNKNOWN_PROPERTY",
                     "_FIX_UNEXPECTED", "_api_fix", "_api_error",
                     "_unexpected", "_refuse"):
            assert not hasattr(module, name), (
                f"{module.__name__}.{name} is back -- see envelopes.py")
