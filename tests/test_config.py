import json

import pytest

from gsc_core import config


def test_load_returns_defaults_when_file_absent(tmp_path):
    loaded = config.load(tmp_path / "absent.json")
    assert loaded == config.DEFAULTS


def test_load_merges_user_values_over_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"inspection_ttl_days": 30}), encoding="utf-8")
    loaded = config.load(target)
    assert loaded["inspection_ttl_days"] == 30
    assert loaded["property_slots"] == config.DEFAULTS["property_slots"]


def test_load_ignores_a_corrupt_file(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{ broken", encoding="utf-8")
    assert config.load(target) == config.DEFAULTS


def test_load_falls_back_when_the_path_is_a_directory(tmp_path):
    directory = tmp_path / "config.json"
    directory.mkdir()
    assert config.load(directory) == config.DEFAULTS


def test_load_falls_back_on_undecodable_bytes(tmp_path):
    target = tmp_path / "config.json"
    target.write_bytes(b"\xff\xfe\x00\x00not utf-8")
    assert config.load(target) == config.DEFAULTS


def test_defaults_carry_no_client_data():
    """The one constraint that decides whether this repo is safe to publish.

    A denylist of previously-seen strings only catches what already leaked, so
    this matches the *shapes* client data takes: addresses, absolute paths from
    a developer machine, and long opaque resource identifiers.
    """
    import re

    serialized = json.dumps(config.DEFAULTS)
    forbidden = {
        "email address": r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}",
        "Windows path": r"[A-Za-z]:\\\\",
        "POSIX home path": r"/(?:home|Users)/\w+",
        "long opaque id": r"[A-Za-z0-9_-]{30,}",
    }
    for label, pattern in forbidden.items():
        assert not re.search(pattern, serialized), (
            f"DEFAULTS appears to contain a {label}: {serialized}"
        )

    # Sites come from the Search Console API, never from config — a sites key
    # would mean per-client data had been reintroduced here.
    assert "sites" not in config.DEFAULTS


def test_defaults_have_no_account_ceiling():
    assert config.DEFAULTS["account_slots"] is None


def test_default_reserve_is_zero():
    assert config.DEFAULTS["daily_reserve"] == 0


def test_default_concurrency_uses_the_inspection_budget():
    assert config.DEFAULTS["inspect_concurrency"] >= 8


def test_default_submit_delay_matches_proven_pacing():
    assert config.DEFAULTS["submit_delay_range"] == [130, 180]


def test_validate_accepts_defaults():
    assert config.validate(config.DEFAULTS) == []


def test_validate_rejects_negative_slots():
    broken = {**config.DEFAULTS, "property_slots": -1}
    assert "property_slots must be a positive integer" in config.validate(broken)


def test_validate_rejects_reserve_above_slots():
    broken = {**config.DEFAULTS, "daily_reserve": 99}
    assert ("daily_reserve (99) must be below property_slots (11)"
            in config.validate(broken))


def test_validate_rejects_malformed_delay_range():
    broken = {**config.DEFAULTS, "submit_delay_range": [180, 130]}
    assert ("submit_delay_range must be [low, high] seconds with low <= high"
            in config.validate(broken))


def test_validate_rejects_non_boolean_stop_on_throttle():
    broken = {**config.DEFAULTS, "stop_on_throttle": "yes"}
    assert "stop_on_throttle must be true or false" in config.validate(broken)


def test_validate_rejects_an_oversized_retry_budget():
    """Every retry is another full trip through the browser UI — a live
    failure took over two minutes to arrive — so a big number turns one
    broken URL into a stalled run."""
    broken = {**config.DEFAULTS, "submit_retries": 9}
    assert "submit_retries must be between 0 and 3" in config.validate(broken)


def test_validate_accepts_retries_turned_off():
    assert config.validate({**config.DEFAULTS, "submit_retries": 0}) == []


def test_validate_rejects_boolean_slots():
    """bool subclasses int, so a bare isinstance check would accept true."""
    broken = {**config.DEFAULTS, "property_slots": True}
    assert "property_slots must be a positive integer" in config.validate(broken)


def test_a_non_positive_inspection_ttl_is_rejected():
    problems = config.validate({**config.DEFAULTS, "inspection_ttl_days": 0})
    assert any("inspection_ttl_days" in p for p in problems)


def test_a_non_integer_inspection_ttl_is_rejected():
    problems = config.validate(
        {**config.DEFAULTS, "inspection_ttl_days": "weekly"})
    assert any("inspection_ttl_days" in p for p in problems)


def test_a_boolean_inspection_ttl_is_rejected():
    """bool subclasses int: `true` would otherwise validate as a one-day
    TTL, silently re-inspecting — and re-charging for — everything daily."""
    problems = config.validate({**config.DEFAULTS, "inspection_ttl_days": True})
    assert any("inspection_ttl_days" in p for p in problems)


def test_a_sensible_inspection_ttl_is_accepted():
    assert not [p for p in config.validate(
        {**config.DEFAULTS, "inspection_ttl_days": 7})
        if "inspection_ttl_days" in p]


def test_invalid_slots_does_not_produce_a_fabricated_reserve_message(tmp_path):
    broken = {**config.DEFAULTS, "property_slots": "foo", "daily_reserve": 99}
    problems = config.validate(broken)
    assert "property_slots must be a positive integer" in problems
    assert not any("must be below property_slots" in p for p in problems)


def test_save_then_load_round_trip(tmp_path):
    target = tmp_path / "config.json"
    config.save({**config.DEFAULTS, "inspection_ttl_days": 14}, target)
    assert config.load(target)["inspection_ttl_days"] == 14


def test_save_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "config.json"
    config.save(config.DEFAULTS, target)
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_load_does_not_mutate_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"property_slots": 3}), encoding="utf-8")
    config.load(target)
    assert config.DEFAULTS["property_slots"] == 11


# --- Bridge keys -----------------------------------------------------------


def test_bridge_defaults_are_present(tmp_path):
    cfg = config.load(tmp_path / "does-not-exist.json")
    assert cfg["bridge_port"] == 8765
    assert cfg["authuser"] == "0"
    assert cfg["auto_launch_browser"] is True
    assert cfg["bridge_connect_timeout"] == 60


@pytest.mark.parametrize("port", [0, -1, 65536, "8765", 1023, True, None])
def test_out_of_range_bridge_port_is_rejected(tmp_path, port):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"bridge_port": port}), encoding="utf-8")
    problems = config.validate(config.load(target))
    assert "bridge_port must be an integer from 1024 to 65535" in problems


@pytest.mark.parametrize("port", [1024, 8765, 65535])
def test_in_range_bridge_port_is_accepted(tmp_path, port):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"bridge_port": port}), encoding="utf-8")
    assert config.validate(config.load(target)) == []


@pytest.mark.parametrize("authuser", ["work", "", 0, None, "0.1", "-1"])
def test_authuser_must_be_a_digit_string(tmp_path, authuser):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"authuser": authuser}), encoding="utf-8")
    problems = config.validate(config.load(target))
    assert any(p.startswith("authuser must be a digit string") for p in problems)


def test_authuser_accepts_a_nonzero_index(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"authuser": "2"}), encoding="utf-8")
    loaded = config.load(target)
    assert config.validate(loaded) == []
    assert loaded["authuser"] == "2"


def test_validate_rejects_non_boolean_auto_launch_browser():
    broken = {**config.DEFAULTS, "auto_launch_browser": "yes"}
    assert "auto_launch_browser must be true or false" in config.validate(broken)


@pytest.mark.parametrize("timeout", [0, -1, "60", 1.5, True, None])
def test_validate_rejects_bad_bridge_connect_timeout(timeout):
    broken = {**config.DEFAULTS, "bridge_connect_timeout": timeout}
    assert ("bridge_connect_timeout must be a positive integer"
            in config.validate(broken))
