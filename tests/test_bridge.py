from __future__ import annotations

import json

import pytest

from gsc_core import bridge


def test_token_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.paths, "ensure_config_dir", lambda: tmp_path)
    first = bridge.load_or_create_token()
    second = bridge.load_or_create_token()
    assert first == second
    assert len(first) >= 32


def test_an_empty_token_file_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge.paths, "ensure_config_dir", lambda: tmp_path)
    (tmp_path / "bridge_token.txt").write_text("   ", encoding="utf-8")
    assert bridge.load_or_create_token().strip()


def test_the_token_is_never_logged(tmp_path, monkeypatch, caplog):
    from tests._logcheck import Captured
    monkeypatch.setattr(bridge.paths, "ensure_config_dir", lambda: tmp_path)
    with Captured(bridge.log) as records:
        token = bridge.load_or_create_token()
    assert records, "creating a token should have logged something"
    assert token not in records.text


@pytest.mark.parametrize("raw", ["", "not json", "[]", '"a string"', '{"no":"type"}', None, 7])
def test_unusable_frames_parse_to_none(raw):
    assert bridge.parse_message(raw) is None


def test_a_typed_object_parses():
    assert bridge.parse_message('{"type":"pong"}') == {"type": "pong"}


def test_submit_frame_carries_every_field():
    frame = json.loads(bridge.make_submit("abc", "sc-domain:example.com",
                                          "https://example.com/a", "0"))
    assert frame == {"type": "submit", "id": "abc",
                     "property": "sc-domain:example.com",
                     "url": "https://example.com/a", "authuser": "0"}


@pytest.mark.parametrize("outcome", sorted(bridge.KNOWN_OUTCOMES))
def test_known_outcomes_pass_through(outcome):
    assert bridge.map_outcome(outcome) == outcome


@pytest.mark.parametrize("outcome", ["invented", "", None, 7, "SUBMITTED"])
def test_anything_else_becomes_error(outcome):
    assert bridge.map_outcome(outcome) == "error"


def test_skipped_is_in_the_vocabulary():
    # content.js emits "skipped" in five places. Coercing it to "error" would
    # charge a quota slot for a URL that was never submitted.
    assert "skipped" in bridge.KNOWN_OUTCOMES
