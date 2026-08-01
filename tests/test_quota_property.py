from datetime import datetime, timedelta, UTC

from gsc_core import quota, store

PROP_A = "sc-domain:example.com"
PROP_B = "sc-domain:other.example"
ACCOUNT = "user@example.com"


def _conn(tmp_path):
    return store.connect(tmp_path / "state.db")


def _spend(conn, property, when, account=ACCOUNT):
    conn.execute(
        "INSERT INTO quota_slots (account, property, used_at) VALUES (?, ?, ?)",
        (account, property, when.isoformat()),
    )


def test_no_slots_used_on_a_fresh_store(tmp_path):
    conn = _conn(tmp_path)
    assert quota.used(conn, PROP_A) == 0
    assert quota.free(conn, PROP_A) == quota.DEFAULT_PROPERTY_SLOTS


def test_used_counts_only_slots_inside_the_window(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _spend(conn, PROP_A, now - timedelta(minutes=10))
    _spend(conn, PROP_A, now - timedelta(minutes=1440))
    _spend(conn, PROP_A, now - timedelta(minutes=1442))
    assert quota.used(conn, PROP_A, now=now) == 2


def test_slot_frees_exactly_one_minute_after_24h(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _spend(conn, PROP_A, now - timedelta(minutes=quota.SLOT_WINDOW_MINUTES - 1))
    assert quota.used(conn, PROP_A, now=now) == 1

    just_past = now + timedelta(minutes=2)
    assert quota.used(conn, PROP_A, now=just_past) == 0


def test_properties_are_independent(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for _ in range(quota.DEFAULT_PROPERTY_SLOTS):
        _spend(conn, PROP_A, now - timedelta(minutes=5))
    assert quota.free(conn, PROP_A, now=now) == 0
    assert quota.free(conn, PROP_B, now=now) == quota.DEFAULT_PROPERTY_SLOTS


def test_free_never_goes_negative(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for _ in range(quota.DEFAULT_PROPERTY_SLOTS + 3):
        _spend(conn, PROP_A, now - timedelta(minutes=5))
    assert quota.free(conn, PROP_A, now=now) == 0


def test_next_free_is_none_when_slots_remain(tmp_path):
    conn = _conn(tmp_path)
    assert quota.next_free(conn, PROP_A) is None


def test_next_free_is_oldest_slot_plus_window(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    oldest = now - timedelta(minutes=100)
    _spend(conn, PROP_A, oldest)
    for offset in range(1, quota.DEFAULT_PROPERTY_SLOTS):
        _spend(conn, PROP_A, now - timedelta(minutes=100 - offset))

    expected = oldest + timedelta(minutes=quota.SLOT_WINDOW_MINUTES)
    assert quota.next_free(conn, PROP_A, now=now) == expected
