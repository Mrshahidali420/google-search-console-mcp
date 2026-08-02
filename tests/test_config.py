import json

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


def test_defaults_carry_no_client_data():
    serialized = json.dumps(config.DEFAULTS)
    assert "gmail.com" not in serialized
    assert "C:\\\\Users" not in serialized
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


def test_save_then_load_round_trip(tmp_path):
    target = tmp_path / "config.json"
    config.save({**config.DEFAULTS, "inspection_ttl_days": 14}, target)
    assert config.load(target)["inspection_ttl_days"] == 14


def test_load_does_not_mutate_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"property_slots": 3}), encoding="utf-8")
    config.load(target)
    assert config.DEFAULTS["property_slots"] == 11
