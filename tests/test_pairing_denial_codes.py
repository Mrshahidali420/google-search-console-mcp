"""A refused pairing must say WHICH rule refused it — without a path.

The four denials need four different fixes from the user (pair first,
reload the extension, point Chrome at the right directory, stop whatever
else is connecting), so a log that records only THAT a denial happened
turns diagnosis into guesswork. The prose reason cannot be logged: it
interpolates `extension_dir()`, an absolute path carrying the operator's
account name, and this logger writes to a file that outlives the session.

So a closed vocabulary of reason CODES is logged instead, and the prose
still travels to the extension unchanged. These tests drive the real
`_handle_pair` against the real `verify_pair_request` with a fake socket —
no server, no network — and assert both halves at once: the code IS in the
log, the path is NOT, and the wire frame still carries the whole reason.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from _logcheck import Captured

from gsc_core import bridge, browsers, pairing, profiles

TOKEN = "test-token-not-a-real-secret"
VALID_ID = "a" * 32
OTHER_ID = "b" * 32
#: Stands in for extension_dir(). The account name is the thing that must
#: never reach a log line, so the fixture puts one in deliberately.
FAKE_EXT_DIR = r"C:\Users\a-real-person\AppData\Roaming\gsc-mcp\extension"


class FakeConn:
    """Just enough socket for `_handle_pair`: an Origin header and a send."""

    def __init__(self, origin: str | None = None) -> None:
        self.request = type("Req", (), {"headers": {"Origin": origin}})()
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


def _installed() -> browsers.Installed:
    return browsers.Installed(brand=browsers.BRANDS["chrome"],
                              exe_path="C:/x/chrome.exe",
                              user_data_dir="C:/x/User Data")


def _profile() -> profiles.Profile:
    return profiles.Profile(directory="Default", name="Person 1",
                            email=None, path="C:/x/User Data/Default")


@pytest.fixture
def target() -> SimpleNamespace:
    return SimpleNamespace(installed=_installed(), profile=_profile())


@pytest.fixture(autouse=True)
def _no_real_extension_dir(monkeypatch):
    """`extension_dir()` extracts a wheel to the real config dir. Never here."""
    monkeypatch.setattr(pairing, "extension_dir", lambda: Path(FAKE_EXT_DIR))


def _pair(target, claimed: str = VALID_ID, origin: str | None = None):
    """Run one pair_request through the real handler. Returns (reply, log)."""
    session = bridge.BridgeSession(port=0, token=TOKEN, target=target)
    conn = FakeConn(origin)
    with Captured(bridge.log) as records:
        session._handle_pair(conn, {"type": "pair_request",
                                    "extension_id": claimed})
    return json.loads(conn.sent[0]), records


def _installed_extension(monkeypatch, ext_id: str | None) -> None:
    monkeypatch.setattr(pairing, "look_up_extension",
                        lambda *a, **k: pairing.Lookup(ext_id, True))


# --------------------------------------------------------------- one per rule

def test_no_browser_target_logs_the_no_target_code():
    reply, records = _pair(None)
    assert reply["type"] == "pair_denied"
    assert pairing.PairCode.NO_TARGET.value in records.text
    records.assert_says_nothing_identifying(FAKE_EXT_DIR, TOKEN)


def test_a_malformed_id_logs_the_malformed_id_code(target):
    reply, records = _pair(target, claimed="not-an-id")
    assert reply["type"] == "pair_denied"
    assert pairing.PairCode.MALFORMED_ID.value in records.text
    records.assert_says_nothing_identifying(FAKE_EXT_DIR, TOKEN)


def test_a_disagreeing_origin_logs_the_bad_origin_code(target, monkeypatch):
    _installed_extension(monkeypatch, VALID_ID)
    reply, records = _pair(target, origin=f"chrome-extension://{OTHER_ID}")
    assert reply["type"] == "pair_denied"
    assert pairing.PairCode.BAD_ORIGIN.value in records.text
    records.assert_says_nothing_identifying(FAKE_EXT_DIR, TOKEN)


def test_no_extension_loaded_from_our_directory_logs_the_dir_mismatch_code(
        target, monkeypatch):
    _installed_extension(monkeypatch, None)
    reply, records = _pair(target)
    assert reply["type"] == "pair_denied"
    assert pairing.PairCode.DIR_MISMATCH.value in records.text
    records.assert_says_nothing_identifying(FAKE_EXT_DIR, TOKEN)


def test_a_different_extension_logs_the_id_mismatch_code(target, monkeypatch):
    _installed_extension(monkeypatch, OTHER_ID)
    reply, records = _pair(target)
    assert reply["type"] == "pair_denied"
    assert pairing.PairCode.ID_MISMATCH.value in records.text
    records.assert_says_nothing_identifying(FAKE_EXT_DIR, TOKEN)


# ------------------------------------------------- the wire is not the log

@pytest.mark.parametrize("installed_id", [None, OTHER_ID])
def test_the_denial_frame_still_carries_the_whole_prose_reason(
        target, monkeypatch, installed_id):
    """The path stays ON THE WIRE. The extension shows it to the one person
    who already owns it, and without it the user cannot act on the refusal."""
    _installed_extension(monkeypatch, installed_id)
    expected = pairing.verify_pair_request(
        target.installed, target.profile, VALID_ID, None).reason
    reply, records = _pair(target)
    assert reply["reason"] == expected
    assert FAKE_EXT_DIR in reply["reason"]
    assert FAKE_EXT_DIR not in records.text


def test_a_verified_request_still_hands_the_token_over(target, monkeypatch):
    _installed_extension(monkeypatch, VALID_ID)
    reply, _ = _pair(target, origin=f"chrome-extension://{VALID_ID}")
    assert reply == {"type": "pair_ok", "token": TOKEN}


def test_the_denial_log_never_carries_the_token(target, monkeypatch):
    _installed_extension(monkeypatch, OTHER_ID)
    reply, records = _pair(target)
    assert TOKEN not in json.dumps(reply)
    assert TOKEN not in records.text
