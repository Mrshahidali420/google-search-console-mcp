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


# --- record_refusal: Google's refusal overrides our estimate, briefly ---------
#
# The ledger can only count what this tool spent. A slot spent by hand in the
# browser, from another machine, or by resubmitting a URL this tool already
# sent, never reaches it -- all ordinary user behaviour, none of it a defect.
# So a refusal on a property the ledger believes is empty is expected.
#
# What it means is narrower than it looks, and the predecessor of this
# function (mark_full, which backfilled the ledger to its ceiling) got it
# wrong: a refusal says zero slots are free AT THAT INSTANT and nothing about
# the next 24 hours. Slots age out one at a time, so a refused property can
# accept a submission minutes later -- observed live on 2026-08-05, three
# times inside 45 minutes, while the backfilled ledger insisted it was full.


def test_a_refusal_blocks_submissions_even_with_slots_on_the_books(tmp_path):
    """The whole point: Google's answer outranks our arithmetic."""
    conn = _conn(tmp_path)
    now = datetime.now(UTC)

    quota.record_refusal(conn, PROP_A, now=now)

    verdict = quota.check(conn, ACCOUNT, PROP_A, now=now)
    assert verdict.allowed is False
    assert verdict.binding == "refused"
    # Not a contradiction, and load-bearing: the ledger still believes it has
    # capacity, and is being overruled rather than rewritten.
    assert verdict.property_free == quota.DEFAULT_PROPERTY_SLOTS


def test_a_refusal_does_not_spend_slots(tmp_path):
    """mark_full wrote 11 rows here. That is the bug, not the fix.

    Backfilled rows were stamped `now`, so they expired 24h after the REFUSAL
    while the real slots -- spent hours earlier -- expired on their own
    schedule. The property read as full straight through a window in which
    Google was handing capacity back every few minutes.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)

    quota.record_refusal(conn, PROP_A, now=now)

    assert quota.used(conn, PROP_A, now=now) == 0
    assert quota.free(conn, PROP_A, now=now) == quota.DEFAULT_PROPERTY_SLOTS


def test_the_cooldown_expires_in_minutes_not_a_day(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    quota.record_refusal(conn, PROP_A, now=now)

    during = now + timedelta(minutes=quota.REFUSAL_COOLDOWN_MINUTES - 1)
    after = now + timedelta(minutes=quota.REFUSAL_COOLDOWN_MINUTES + 1)

    assert quota.cooling_off(conn, PROP_A, now=during) is not None
    assert quota.cooling_off(conn, PROP_A, now=after) is None
    assert quota.check(conn, ACCOUNT, PROP_A, now=after).allowed is True


def test_the_cooldown_says_when_to_ask_again(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)

    quota.record_refusal(conn, PROP_A, now=now)

    verdict = quota.check(conn, ACCOUNT, PROP_A, now=now)
    assert verdict.next_free_at == now + timedelta(
        minutes=quota.REFUSAL_COOLDOWN_MINUTES)


def test_a_refusal_does_not_touch_another_property(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(UTC)

    quota.record_refusal(conn, PROP_A, now=now)

    assert quota.check(conn, ACCOUNT, PROP_B, now=now).allowed is True
    assert quota.free(conn, PROP_B, now=now) == quota.DEFAULT_PROPERTY_SLOTS


def test_a_second_refusal_restarts_the_cooldown(tmp_path):
    """One row per property, overwritten -- not an accumulating history."""
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    quota.record_refusal(conn, PROP_A, now=now)

    later = now + timedelta(minutes=quota.REFUSAL_COOLDOWN_MINUTES - 1)
    quota.record_refusal(conn, PROP_A, now=later)

    just_past_the_first = now + timedelta(
        minutes=quota.REFUSAL_COOLDOWN_MINUTES + 1)
    assert quota.cooling_off(conn, PROP_A, now=just_past_the_first) is not None


def test_last_refusal_outlives_the_cooldown(tmp_path):
    """The cooldown expires; the fact that Google refused does not.

    gsc_quota reports it as the one figure in its submission block that came
    from Google rather than from local arithmetic.
    """
    conn = _conn(tmp_path)
    now = datetime.now(UTC)
    quota.record_refusal(conn, PROP_A, now=now)

    long_after = now + timedelta(days=2)
    assert quota.cooling_off(conn, PROP_A, now=long_after) is None
    assert quota.last_refusal(conn, PROP_A) == datetime.fromisoformat(
        store.utc_iso(now))


def test_last_refusal_is_none_when_google_never_refused(tmp_path):
    conn = _conn(tmp_path)
    assert quota.last_refusal(conn, PROP_A) is None
    assert quota.cooling_off(conn, PROP_A) is None
