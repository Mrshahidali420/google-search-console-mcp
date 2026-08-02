import threading
import urllib.request
from urllib.parse import urlparse

import pytest

from gsc_core import gauth


def test_receiver_binds_a_loopback_port():
    with gauth.LoopbackReceiver() as receiver:
        parsed = urlparse(receiver.redirect_uri)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.port > 0


def test_receiver_generates_unpredictable_state():
    with gauth.LoopbackReceiver() as first:
        with gauth.LoopbackReceiver() as second:
            assert first.state != second.state
            assert len(first.state) >= 16


def test_receiver_captures_the_code():
    with gauth.LoopbackReceiver() as receiver:
        def visit():
            url = f"{receiver.redirect_uri}/?code=the-code&state={receiver.state}"
            urllib.request.urlopen(url, timeout=5).read()

        threading.Thread(target=visit, daemon=True).start()
        assert receiver.wait(timeout=10) == "the-code"


def test_receiver_rejects_mismatched_state():
    with gauth.LoopbackReceiver() as receiver:
        def visit():
            url = f"{receiver.redirect_uri}/?code=the-code&state=wrong"
            try:
                urllib.request.urlopen(url, timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=visit, daemon=True).start()
        with pytest.raises(gauth.ConsentFailed, match="state"):
            receiver.wait(timeout=10)


def test_receiver_surfaces_user_denial():
    with gauth.LoopbackReceiver() as receiver:
        def visit():
            url = f"{receiver.redirect_uri}/?error=access_denied&state={receiver.state}"
            try:
                urllib.request.urlopen(url, timeout=5).read()
            except Exception:
                pass

        threading.Thread(target=visit, daemon=True).start()
        with pytest.raises(gauth.ConsentFailed, match="access_denied"):
            receiver.wait(timeout=10)


def test_receiver_times_out():
    with gauth.LoopbackReceiver() as receiver:
        with pytest.raises(gauth.ConsentFailed, match="timed out"):
            receiver.wait(timeout=0.5)
