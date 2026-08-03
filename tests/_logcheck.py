"""Shared privacy guard for log assertions. One copy, on purpose.

This started as a helper inside one test file and was copy-pasted into a
second. The third copy is where a privacy guard quietly drifts and stops
guarding, so it lives here now and every call site imports it.

Two pieces:

* `capturing(logger)` — a context manager that attaches a collector to a
  module's own logger. NOT caplog: `runlog` sets `propagate = False` on the
  package root, so records never climb to pytest's root handler, and
  `caplog.at_level(0)` is NOTSET, which drops debug records entirely. A
  negative assertion written either way passes over an empty buffer
  forever. Every file using this must also keep a live-capture canary.

* `logged_text(records)` — everything a record could carry a secret in,
  joined. `getMessage()` alone is NOT enough, and the gap is not
  theoretical: a site changed from `log.debug` to `log.exception`, or given
  `exc_info=True`, puts the exception — and with it a client secret, an
  address, or an OSError's filesystem path — into `exc_info`, where the
  default formatter writes it to the log FILE and a message-only assertion
  never sees it. `args` is included for the same reason: an argument the
  format string does not consume still reaches a locals-capturing handler.

`tests/test_logcheck.py` pins both, so weakening this module reddens a test
rather than silently disarming every guard that depends on it.
"""
from __future__ import annotations

import logging
import re
import traceback
from contextlib import contextmanager


def logged_text(records) -> str:
    """Join every field of every record that could carry a secret."""
    parts: list[str] = []
    for record in records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
        if record.exc_text:
            parts.append(record.exc_text)
        if record.stack_info:
            parts.append(record.stack_info)
    return "\n".join(parts)


def carries_an_exception(record) -> bool:
    """True if this record renders an exception into the log file.

    The rule these guards enforce is not "do not log the exception this
    test happens to raise" — it is that the site logs a bare type NAME. A
    traceback whose frames happen to hold no address today holds one the
    first time the failure mode changes, so the shape is pinned directly.
    """
    return bool(record.exc_info or record.exc_text or record.stack_info)


class Captured(list):
    """Log records from one module's logger, captured directly."""

    def __init__(self, logger: logging.Logger):
        super().__init__()
        self._logger = logger
        self._handler = logging.Handler(level=logging.DEBUG)
        self._handler.emit = self.append
        self._level = logger.level

    def __enter__(self) -> "Captured":
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *exc) -> bool:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)
        return False

    @property
    def text(self) -> str:
        return logged_text(self)

    def assert_says_nothing_identifying(self, *secrets, shape=None) -> None:
        """Every record is prose plus a bare exception TYPE NAME.

        The positive assertion is load-bearing: without it the negative
        ones below pass over an empty buffer. `shape` is an optional regex
        every message must fully match, for modules whose log lines follow
        one fixed form.
        """
        assert self, "the failure should have been logged at all"
        blob = logged_text(self)
        assert "@" not in blob, blob
        for secret in secrets:
            assert str(secret) not in blob, blob
        for record in self:
            assert not carries_an_exception(record), logged_text([record])
            if shape is not None:
                assert re.fullmatch(shape, record.getMessage()), \
                    record.getMessage()


@contextmanager
def capturing(logger: logging.Logger):
    """`Captured` as a context manager, for use from a pytest fixture."""
    with Captured(logger) as records:
        yield records
