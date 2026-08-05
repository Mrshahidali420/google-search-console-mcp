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
        (account, property, store.utc_iso(when)),
    )


def _open_submission(conn, property, when, account=ACCOUNT):
    conn.execute(
        "INSERT INTO submissions "
        "(url, property, account, requested_at, committed) VALUES (?, ?, ?, ?, 0)",
        ("https://example.com/pending", property, account, store.utc_iso(when)),
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


def test_slot_is_still_held_one_minute_before_the_window_closes(tmp_path):
    """Pins the window length rather than restating it: a slot 1440 minutes
    old is still held. Setting SLOT_WINDOW_MINUTES to 1440 fails this."""
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _spend(conn, PROP_A, now - timedelta(minutes=1440))
    assert quota.used(conn, PROP_A, now=now) == 1


def test_slot_at_exactly_the_window_edge_is_free(tmp_path):
    """Pins '>' rather than '>=': a slot exactly 1441 minutes old is free."""
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _spend(conn, PROP_A, now - timedelta(minutes=1441))
    assert quota.used(conn, PROP_A, now=now) == 0


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


def test_open_submissions_count_against_the_property(tmp_path):
    """A clicked-but-unclosed submission has really spent its slot at Google."""
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _open_submission(conn, PROP_A, now - timedelta(minutes=5))
    assert quota.used(conn, PROP_A, now=now) == 1
    assert quota.free(conn, PROP_A, now=now) == quota.DEFAULT_PROPERTY_SLOTS - 1


def test_next_free_is_none_when_slots_remain(tmp_path):
    conn = _conn(tmp_path)
    assert quota.next_free(conn, PROP_A) is None


def test_next_free_is_none_while_capacity_remains(tmp_path):
    """The early-return guard matters here, not on an empty store.

    With some slots spent but capacity still free, MIN(used_at) is non-NULL —
    so without the guard next_free would report a wait time for a property
    that can be submitted to right now, and a caller would idle instead of
    working.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for _ in range(3):
        _spend(conn, PROP_A, now - timedelta(minutes=5))

    assert quota.free(conn, PROP_A, now=now) > 0
    assert quota.next_free(conn, PROP_A, now=now) is None


def test_next_free_is_oldest_slot_plus_window(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    oldest = now - timedelta(minutes=100)
    _spend(conn, PROP_A, oldest)
    for offset in range(1, quota.DEFAULT_PROPERTY_SLOTS):
        _spend(conn, PROP_A, now - timedelta(minutes=100 - offset))

    expected = oldest + timedelta(minutes=quota.SLOT_WINDOW_MINUTES)
    assert quota.next_free(conn, PROP_A, now=now) == expected


def test_next_free_is_set_when_open_submissions_fill_the_property(tmp_path):
    """used() counts open submissions, so next_free must see them too.

    Otherwise a property filled entirely by in-flight submissions reports zero
    capacity AND no wait time — and None is documented as "go ahead".
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    oldest = now - timedelta(minutes=100)
    _open_submission(conn, PROP_A, oldest)
    for offset in range(1, quota.DEFAULT_PROPERTY_SLOTS):
        _open_submission(conn, PROP_A, now - timedelta(minutes=100 - offset))

    assert quota.free(conn, PROP_A, now=now) == 0
    expected = (datetime.fromisoformat(store.utc_iso(oldest))
                + timedelta(minutes=quota.SLOT_WINDOW_MINUTES))
    assert quota.next_free(conn, PROP_A, now=now) == expected


# --- mark_full: Google's refusal overrides our estimate -----------------------
#
# The ledger can only count what this tool spent. A slot spent by hand in the
# browser, from another machine, or by resubmitting a URL this tool already
# sent, never reaches it -- all ordinary user behaviour, none of it a defect.
# So a refusal on a property the ledger believes is empty is expected, and it
# carries one fact worth recording: every slot is gone.


def test_mark_full_leaves_no_free_slots(tmp_path):
    conn = _conn(tmp_path)

    added = quota.mark_full(conn, ACCOUNT, PROP_A)

    assert added == quota.DEFAULT_PROPERTY_SLOTS
    assert quota.free(conn, PROP_A) == 0


def test_mark_full_only_backfills_the_shortfall(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    _spend(conn, PROP_A, now - timedelta(minutes=5))
    _spend(conn, PROP_A, now - timedelta(minutes=5))

    added = quota.mark_full(conn, ACCOUNT, PROP_A, now=now)

    assert added == quota.DEFAULT_PROPERTY_SLOTS - 2
    assert quota.used(conn, PROP_A, now=now) == quota.DEFAULT_PROPERTY_SLOTS


def test_mark_full_on_an_already_full_property_writes_nothing(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    for _ in range(quota.DEFAULT_PROPERTY_SLOTS):
        _spend(conn, PROP_A, now - timedelta(minutes=5))

    assert quota.mark_full(conn, ACCOUNT, PROP_A, now=now) == 0
    assert quota.used(conn, PROP_A, now=now) == quota.DEFAULT_PROPERTY_SLOTS


def test_mark_full_does_not_touch_another_property(tmp_path):
    conn = _conn(tmp_path)

    quota.mark_full(conn, ACCOUNT, PROP_A)

    assert quota.free(conn, PROP_B) == quota.DEFAULT_PROPERTY_SLOTS


def test_backfilled_slots_age_out_on_the_normal_window(tmp_path):
    """Stamped `now`, so they expire late rather than early.

    The real slots were spent at some earlier, unknowable moment, so freeing
    on our own stamp is pessimistic by design: late costs a wait, early costs
    another hard refusal -- which is the thing this exists to prevent.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)

    quota.mark_full(conn, ACCOUNT, PROP_A, now=now)

    later = now + timedelta(minutes=quota.SLOT_WINDOW_MINUTES + 1)
    assert quota.free(conn, PROP_A, now=now) == 0
    assert quota.free(conn, PROP_A, now=later) == quota.DEFAULT_PROPERTY_SLOTS
