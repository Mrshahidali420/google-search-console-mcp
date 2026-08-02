import dataclasses
from datetime import datetime, timedelta, UTC

import pytest

from gsc_core import quota, store

PROP_A = "sc-domain:example.com"
PROP_B = "sc-domain:other.example"
ACCOUNT = "user@example.com"
NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)


def _conn(tmp_path):
    return store.connect(tmp_path / "state.db")


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "state.db")
    yield connection
    connection.close()


def _spend(conn, property, when, account=ACCOUNT):
    conn.execute(
        "INSERT INTO quota_slots (account, property, used_at) VALUES (?, ?, ?)",
        (account, property, store.utc_iso(when)),
    )


def _open_submission(conn, property, when, account=ACCOUNT):
    conn.execute(
        "INSERT INTO submissions "
        "(url, property, account, requested_at, committed) VALUES (?, ?, ?, ?, 0)",
        ("https://example.com/pending", property, account, store.utc_iso(when)),
    )


def test_fresh_store_allows_submission(tmp_path):
    conn = _conn(tmp_path)
    verdict = quota.check(conn, ACCOUNT, PROP_A)
    assert verdict.allowed is True
    assert verdict.binding is None
    assert verdict.property_free == quota.DEFAULT_PROPERTY_SLOTS
    assert verdict.account_free is None


def test_exhausted_property_blocks_and_reports_binding(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for _ in range(quota.DEFAULT_PROPERTY_SLOTS):
        _spend(conn, PROP_A, now - timedelta(minutes=5))

    verdict = quota.check(conn, ACCOUNT, PROP_A, now=now)
    assert verdict.allowed is False
    assert verdict.binding == "property"
    assert verdict.next_free_at is not None


def test_account_ceiling_is_tracked_but_not_enforced_by_default(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for index in range(40):
        target = PROP_A if index % 2 == 0 else PROP_B
        _spend(conn, target, now - timedelta(minutes=5))

    assert quota.account_used(conn, ACCOUNT, now=now) == 40
    verdict = quota.check(conn, ACCOUNT, PROP_A, property_slots=100, now=now)
    assert verdict.allowed is True
    assert verdict.account_free is None


def test_account_ceiling_binds_when_configured(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for index in range(12):
        target = PROP_A if index % 2 == 0 else PROP_B
        _spend(conn, target, now - timedelta(minutes=5))

    verdict = quota.check(conn, ACCOUNT, PROP_A, property_slots=100,
                          account_slots=12, now=now)
    assert verdict.allowed is False
    assert verdict.binding == "account"
    assert verdict.account_free == 0


def test_property_binds_first_when_both_are_exhausted(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for _ in range(quota.DEFAULT_PROPERTY_SLOTS):
        _spend(conn, PROP_A, now - timedelta(minutes=5))

    verdict = quota.check(conn, ACCOUNT, PROP_A, account_slots=1, now=now)
    assert verdict.allowed is False
    assert verdict.binding == "property"


def test_account_usage_ignores_slots_outside_the_window(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _spend(conn, PROP_A, now - timedelta(minutes=10))
    _spend(conn, PROP_A, now - timedelta(minutes=2000))
    assert quota.account_used(conn, ACCOUNT, now=now) == 1


def test_other_accounts_do_not_count(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _spend(conn, PROP_A, now - timedelta(minutes=5), account="other@example.com")
    assert quota.account_used(conn, ACCOUNT, now=now) == 0


def test_account_used_counts_open_submissions(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _open_submission(conn, PROP_A, now - timedelta(minutes=5))
    assert quota.account_used(conn, ACCOUNT, now=now) == 1


def test_account_bound_verdict_carries_a_real_wait_time(tmp_path):
    """binding=='account' is only reachable while property capacity remains,
    and next_free() returns None in that case — so the account side needs its
    own wait time or the verdict says 'blocked, go ahead' at once.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    oldest = now - timedelta(minutes=100)
    _spend(conn, PROP_A, oldest)
    _spend(conn, PROP_B, now - timedelta(minutes=99))

    verdict = quota.check(conn, ACCOUNT, PROP_A, property_slots=100,
                          account_slots=2, now=now)
    assert verdict.allowed is False
    assert verdict.binding == "account"
    assert verdict.next_free_at is not None
    assert verdict.next_free_at == datetime.fromisoformat(
        store.utc_iso(oldest)
    ) + timedelta(minutes=quota.SLOT_WINDOW_MINUTES)


def test_account_wait_time_counts_open_submissions(tmp_path):
    """An account filled entirely by in-flight submissions must still report
    when its next slot frees — the account mirror of the property-side case.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    oldest = now - timedelta(minutes=100)
    _open_submission(conn, PROP_A, oldest)
    _open_submission(conn, PROP_B, now - timedelta(minutes=99))

    verdict = quota.check(conn, ACCOUNT, PROP_A, property_slots=100,
                          account_slots=2, now=now)
    assert verdict.binding == "account"
    assert verdict.next_free_at == datetime.fromisoformat(
        store.utc_iso(oldest)
    ) + timedelta(minutes=quota.SLOT_WINDOW_MINUTES)


def test_a_blocked_verdict_never_reports_no_wait_time(monkeypatch, tmp_path):
    """A slot can free between check()'s two reads, leaving the wait-time
    query with nothing to return. The verdict must still carry a time —
    'blocked' plus next_free_at=None reads as 'go ahead' to a caller.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for _ in range(quota.DEFAULT_PROPERTY_SLOTS):
        _spend(conn, PROP_A, now - timedelta(minutes=5))

    monkeypatch.setattr(quota, "next_free", lambda *a, **k: None)
    verdict = quota.check(conn, ACCOUNT, PROP_A, now=now)

    assert verdict.allowed is False
    assert verdict.next_free_at == now


def test_quota_verdict_is_frozen():
    verdict = quota.QuotaVerdict(allowed=True, binding=None, property_free=11,
                                 account_free=None, next_free_at=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        verdict.allowed = False


def test_daily_reserve_lowers_the_effective_ceiling(conn):
    for _ in range(9):
        _spend(conn, PROP_A, NOW - timedelta(minutes=5))
    assert quota.check(conn, ACCOUNT, PROP_A, property_slots=11,
                       daily_reserve=0, now=NOW).allowed
    assert not quota.check(conn, ACCOUNT, PROP_A, property_slots=11,
                           daily_reserve=2, now=NOW).allowed


def test_daily_reserve_of_zero_changes_nothing(conn):
    """An explicit daily_reserve=0 must be indistinguishable from omitting
    the argument entirely -- not merely "still allowed at full capacity",
    which a fresh, unspent property reports regardless of whether the
    reserve is honoured, ignored, or even computed at all. Spending first
    forces the comparison through the real free() subtraction, and
    checking both calls against each other (rather than against a
    hardcoded 11) also catches a wrong default for the daily_reserve
    parameter itself, which neither call's own number alone would reveal.
    """
    for _ in range(4):
        _spend(conn, PROP_A, NOW - timedelta(minutes=5))

    with_explicit_zero = quota.check(conn, ACCOUNT, PROP_A, property_slots=11,
                                     daily_reserve=0, now=NOW)
    with_default = quota.check(conn, ACCOUNT, PROP_A, property_slots=11, now=NOW)

    assert with_explicit_zero.property_free == with_default.property_free == 7
    assert with_explicit_zero.next_free_at == with_default.next_free_at
