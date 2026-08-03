"""The privacy guard's own guard.

Every negative log assertion in this suite is only as strong as
`_logcheck.logged_text`. Weakened back to `getMessage()`-only — the exact
regression this module was created to fix — the leak assertions in
test_pairing, test_browsers, test_tool_setup and
test_doctor_onboarding_checks all go quietly green while a real secret sits
in the log file. So the helper's coverage is pinned here, field by field,
and degrading it reddens this file.
"""
from __future__ import annotations

import logging

import pytest

from _logcheck import Captured, capturing, carries_an_exception, logged_text

SECRET = "canary-secret-value"


@pytest.fixture
def logger():
    """A private logger, so nothing here depends on runlog's configuration."""
    return logging.getLogger("gsc._logcheck_selftest")


def _record(**kwargs) -> logging.LogRecord:
    record = logging.LogRecord(name="t", level=logging.DEBUG, pathname="p",
                               lineno=1, msg=kwargs.pop("msg", "prose"),
                               args=kwargs.pop("args", ()),
                               exc_info=kwargs.pop("exc_info", None))
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


# ---------------------------------------------------------------------------
# logged_text covers every field a secret can ride in
# ---------------------------------------------------------------------------

def test_logged_text_sees_the_formatted_message():
    assert SECRET in logged_text([_record(msg="prose %s", args=(SECRET,))])


def test_logged_text_sees_an_argument_the_format_string_never_consumed():
    """A locals-capturing or structured handler serialises `args` whole, so
    a value the format string never names is as public as one it does.
    Mapping-style args are the case that reaches `getMessage()` intact and
    still renders nothing."""
    record = _record(msg="prose", args=({"secret": SECRET},))
    assert SECRET not in record.getMessage()      # the gap, demonstrated
    assert SECRET in logged_text([record])


def test_logged_text_sees_the_exception_the_message_never_names():
    """This is the whole bug: `exc_info=True` on a site that logs only a
    type name puts the exception's message — a filesystem path, an address
    — into the log file, invisible to `getMessage()`."""
    try:
        raise OSError(SECRET)
    except OSError:
        import sys
        record = _record(exc_info=sys.exc_info())
    assert SECRET in logged_text([record])


def test_logged_text_sees_a_cached_exc_text():
    """Once a formatter has run, the rendering is cached on the record and
    `exc_info` may have been cleared. The cached copy still reaches disk."""
    assert SECRET in logged_text([_record(exc_text=f"Traceback: {SECRET}")])


def test_logged_text_sees_stack_info():
    assert SECRET in logged_text([_record(stack_info=f"Stack: {SECRET}")])


# ---------------------------------------------------------------------------
# carries_an_exception is the structural half of the rule
# ---------------------------------------------------------------------------

def test_a_plain_record_carries_no_exception():
    assert not carries_an_exception(_record())


@pytest.mark.parametrize("field", ["exc_text", "stack_info"])
def test_a_record_that_renders_an_exception_is_reported_as_such(field):
    assert carries_an_exception(_record(**{field: "anything"}))


def test_exc_info_alone_is_enough_to_be_reported():
    try:
        raise ValueError("x")
    except ValueError:
        import sys
        assert carries_an_exception(_record(exc_info=sys.exc_info()))


# ---------------------------------------------------------------------------
# The guard rejects what it is supposed to reject
# ---------------------------------------------------------------------------

def test_the_guard_fails_over_an_empty_buffer(logger):
    """A negative assertion that captured nothing guards nothing."""
    with Captured(logger) as records:
        pass
    with pytest.raises(AssertionError):
        records.assert_says_nothing_identifying()


def test_the_guard_rejects_an_address(logger):
    with Captured(logger) as records:
        logger.debug("signed in as %s", "operator@example.com")
    with pytest.raises(AssertionError):
        records.assert_says_nothing_identifying()


def test_the_guard_rejects_a_named_secret(logger):
    with Captured(logger) as records:
        logger.debug("failed for %s", SECRET)
    with pytest.raises(AssertionError):
        records.assert_says_nothing_identifying(SECRET)


def test_the_guard_rejects_a_rendered_exception(logger):
    """Even one whose text holds nothing identifying today."""
    with Captured(logger) as records:
        try:
            raise OSError("harmless-for-now")
        except OSError:
            logger.debug("preferences unreadable (OSError)", exc_info=True)
    with pytest.raises(AssertionError):
        records.assert_says_nothing_identifying()


def test_the_guard_rejects_a_message_that_breaks_the_declared_shape(logger):
    with Captured(logger) as records:
        logger.debug("preferences unreadable: %s", "some detail")
    with pytest.raises(AssertionError):
        records.assert_says_nothing_identifying(shape=r"[a-z ]+ \(\w+\)")


def test_the_guard_accepts_a_bare_type_name(logger):
    with Captured(logger) as records:
        logger.debug("preferences unreadable (%s)", "OSError")
    records.assert_says_nothing_identifying(shape=r"[a-z ]+ \(\w+\)")


# ---------------------------------------------------------------------------
# Capture itself
# ---------------------------------------------------------------------------

def test_capturing_sees_debug_records_and_restores_the_logger(logger):
    """`caplog.at_level(0)` is NOTSET and drops these; runlog's
    propagate=False keeps them off caplog's handler either way."""
    before_level = logger.level
    before_handlers = list(logger.handlers)
    with capturing(logger) as records:
        logger.debug("canary")
    assert any("canary" in r.getMessage() for r in records)
    assert logger.level == before_level
    assert logger.handlers == before_handlers
