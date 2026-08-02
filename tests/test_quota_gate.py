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
