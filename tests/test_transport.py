"""transport.transport_failure — the message that replaces str(exc)."""
from __future__ import annotations

import requests

from gsc_core.transport import transport_failure


def test_the_exception_message_does_not_survive():
    exc = requests.exceptions.ProxyError(
        "Cannot connect to proxy https://user:hunter2@proxy.internal.invalid:8080")
    message = transport_failure(exc)
    for fragment in ("hunter2", "user:", "proxy.internal.invalid", "8080"):
        assert fragment not in message


def test_the_exception_type_does_survive():
    """A bare constant would make every network failure look identical.

    The type name is the whole diagnostic that remains, so it has to be
    there -- and it is safe to include because it is a class name from
    requests, not text composed from the user's environment.
    """
    assert "ProxyError" in transport_failure(
        requests.exceptions.ProxyError("anything"))
    assert "ConnectTimeout" in transport_failure(
        requests.exceptions.ConnectTimeout("anything"))


def test_two_different_failures_produce_two_different_messages():
    """Pins the above against a message that merely mentions a type name.

    Asserting one substring appears cannot tell "(ProxyError)" from a
    constant sentence that happens to contain the word.
    """
    assert (transport_failure(requests.exceptions.ProxyError("x"))
            != transport_failure(requests.exceptions.ConnectTimeout("x")))
