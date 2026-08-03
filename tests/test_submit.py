"""Tests for the submission policy layer.

The disposition table is the only thing standing between a run and a
mis-charged quota ledger, so these tests pin CONSEQUENCES wherever they can:
what the store looks like after settle(), how much capacity a later reserve()
sees, and whether a hard_stop actually ends a run. Asserting that a frozen
dataclass field holds the value the table literal put there would prove
nothing about any of that.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from gsc_core import bridge, quota, store, submit

PROPERTY = "sc-domain:example.com"
OTHER_PROPERTY = "sc-domain:example.net"
ACCOUNT = "account-a"


@pytest.fixture
def conn(tmp_path):
    connection = store.connect(tmp_path / "gsc.sqlite3")
    yield connection
    connection.close()


def _fill_slots(conn: sqlite3.Connection, count: int, *,
                property: str = PROPERTY, account: str = ACCOUNT) -> None:
    now = datetime.now(UTC)
    for index in range(count):
        conn.execute(
            "INSERT INTO quota_slots (account, property, used_at) VALUES (?,?,?)",
            (account, property, store.utc_iso(now - timedelta(minutes=index))))
    conn.commit()


def _reserve(conn: sqlite3.Connection, url: str, **kwargs) -> submit.Reservation:
    params = {"property_slots": 11, "account_slots": None, "daily_reserve": 0}
    params.update(kwargs)
    return submit.reserve(conn, url, PROPERTY, ACCOUNT, None, **params)


# --- the table ---------------------------------------------------------------


def test_every_extension_outcome_has_a_disposition():
    # A new outcome reaching settle() with no entry would fall through to a
    # default and quietly mis-charge quota, which is the exact class of bug
    # this table exists to make impossible.
    assert set(bridge.KNOWN_OUTCOMES) == set(submit.DISPOSITIONS)


@pytest.mark.parametrize("outcome", ["submitted", "captcha_after_click", "timeout"])
def test_outcomes_that_reached_google_spend_a_slot(outcome):
    assert submit.disposition_for(outcome, stop_on_throttle=True).spends_slot


@pytest.mark.parametrize("outcome", ["already_indexed", "quota_exceeded",
                                     "rate_limited", "auth_required",
                                     "account_mismatch", "captcha",
                                     "skipped", "error"])
def test_outcomes_before_the_click_spend_nothing(outcome):
    assert not submit.disposition_for(outcome, stop_on_throttle=True).spends_slot


@pytest.mark.parametrize("outcome", ["quota_exceeded", "rate_limited",
                                     "auth_required", "account_mismatch",
                                     "captcha", "captcha_after_click"])
def test_session_level_failures_hard_stop(outcome):
    assert submit.disposition_for(outcome, stop_on_throttle=True).hard_stop


@pytest.mark.parametrize("outcome", ["submitted", "already_indexed",
                                     "skipped", "error", "timeout"])
def test_per_url_outcomes_do_not_stop_the_run(outcome):
    assert not submit.disposition_for(outcome, stop_on_throttle=True).hard_stop


def test_stop_on_throttle_false_lets_a_rate_limit_continue():
    assert not submit.disposition_for("rate_limited", stop_on_throttle=False).hard_stop
    # quota_exceeded is NOT governed by that switch: the property has no
    # capacity, so continuing would submit into a guaranteed refusal.
    assert submit.disposition_for("quota_exceeded", stop_on_throttle=False).hard_stop


@pytest.mark.parametrize("outcome", ["quota_exceeded", "auth_required",
                                     "account_mismatch", "captcha",
                                     "captcha_after_click"])
def test_stop_on_throttle_false_does_not_soften_session_failures(outcome):
    # Only rate_limited is a throttle. A switch that leaked into the others
    # would keep a signed-out or challenged session hammering every later URL.
    assert submit.disposition_for(outcome, stop_on_throttle=False).hard_stop


def test_stop_on_throttle_never_changes_what_a_slot_costs():
    # The switch governs whether the RUN continues, never the ledger. If it
    # ever bled into spends_slot the same outcome would cost a different
    # amount depending on an unrelated user preference.
    for outcome in bridge.KNOWN_OUTCOMES:
        assert (submit.disposition_for(outcome, stop_on_throttle=True).spends_slot
                is submit.disposition_for(outcome,
                                          stop_on_throttle=False).spends_slot)


def test_an_unknown_outcome_is_refused_loudly():
    with pytest.raises(KeyError):
        submit.disposition_for("invented", stop_on_throttle=True)


@pytest.mark.parametrize("outcome", ["no_property", "no_quota"])
def test_loop_level_outcomes_are_not_dispositions(outcome):
    # Neither ever opened a submission row, so neither has a disposition.
    # If one reaches this function that is a routing bug, and swallowing it
    # here would hide it.
    assert outcome not in submit.DISPOSITIONS
    with pytest.raises(KeyError):
        submit.disposition_for(outcome, stop_on_throttle=True)


def test_dispositions_are_immutable():
    with pytest.raises(Exception):
        submit.DISPOSITIONS["submitted"].spends_slot = False


def test_every_label_names_its_own_outcome():
    # settle() writes the label into the submissions row; a mismatched label
    # would make the audit trail disagree with what the extension reported.
    for outcome, disposition in submit.DISPOSITIONS.items():
        assert disposition.label == outcome


# --- reserve() ---------------------------------------------------------------


def test_reserve_opens_a_row_when_the_property_has_capacity(conn):
    reservation = _reserve(conn, "https://example.com/a")
    assert reservation.submission_id is not None
    assert reservation.verdict.allowed
    assert len(store.open_submissions(conn)) == 1


def test_reserve_records_the_row_it_was_asked_for(conn):
    reservation = submit.reserve(conn, "https://example.com/a", PROPERTY,
                                 ACCOUNT, "job-1", property_slots=11,
                                 account_slots=None, daily_reserve=0)
    row = store.open_submissions(conn)[0]
    assert row["id"] == reservation.submission_id
    assert row["url"] == "https://example.com/a"
    assert row["property"] == PROPERTY
    assert row["account"] == ACCOUNT
    assert row["job_id"] == "job-1"
    assert row["committed"] == 0


def test_reserve_refuses_and_opens_nothing_when_the_property_is_full(conn):
    _fill_slots(conn, 11)
    reservation = _reserve(conn, "https://example.com/a")
    assert reservation.submission_id is None
    assert reservation.verdict.allowed is False
    assert reservation.verdict.binding == "property"
    assert store.open_submissions(conn) == []


def test_a_refused_reservation_writes_nothing_at_all(conn):
    _fill_slots(conn, 11)
    _reserve(conn, "https://example.com/a")
    assert conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"] == 11


def test_reserve_honours_daily_reserve(conn):
    _fill_slots(conn, 9)
    # 11 slots, 2 held back, 9 spent -> nothing spendable left
    reservation = _reserve(conn, "https://example.com/a", daily_reserve=2)
    assert reservation.submission_id is None


def test_the_reserved_slots_stay_reachable_without_the_reserve(conn):
    # The reserve holds slots back from THIS caller, it does not destroy them:
    # the same store with daily_reserve=0 still has capacity.
    _fill_slots(conn, 9)
    assert _reserve(conn, "https://example.com/a", daily_reserve=2).submission_id is None
    assert _reserve(conn, "https://example.com/b", daily_reserve=0).submission_id is not None


def test_reserve_is_scoped_to_one_property(conn):
    # Quota is per property. A property drained to zero must not block a
    # different one on the same account.
    _fill_slots(conn, 11)
    reservation = submit.reserve(conn, "https://example.net/a", OTHER_PROPERTY,
                                 ACCOUNT, None, property_slots=11,
                                 account_slots=None, daily_reserve=0)
    assert reservation.submission_id is not None


def test_reserve_counts_its_own_open_rows(conn):
    # Eleven reservations in a row must exhaust an 11-slot property's capacity
    # even though none has been closed: an open submission has really spent
    # its slot at Google.
    for index in range(11):
        assert _reserve(conn, f"https://example.com/{index}").submission_id is not None
    assert _reserve(conn, "https://example.com/x").submission_id is None


def test_reserve_reports_shrinking_capacity(conn):
    # property_free is what the run loop shows the user; a constant value
    # would look identical to a working one until the run hit the wall.
    seen = [_reserve(conn, f"https://example.com/{i}").verdict.property_free
            for i in range(3)]
    assert seen == [11, 10, 9]


def test_a_refusal_says_when_capacity_returns(conn):
    _fill_slots(conn, 11)
    verdict = _reserve(conn, "https://example.com/a").verdict
    # "blocked, with no wait time" reads as "go ahead" to a caller.
    assert verdict.next_free_at is not None


def test_reserve_leaves_no_transaction_open(conn):
    _reserve(conn, "https://example.com/a")
    assert conn.in_transaction is False
    _fill_slots(conn, 11)
    _reserve(conn, "https://example.com/b")
    assert conn.in_transaction is False


def test_reserve_is_atomic_against_a_concurrent_writer(conn, tmp_path):
    # The defect this guards: quota.check() and open_submission() as two
    # separate transactions let two callers both read "one slot free" and
    # both open a row. One shared BEGIN IMMEDIATE makes the second wait and
    # re-read. Two CONNECTIONS, never one shared across threads — tx()'s
    # re-entrancy is connection-scoped.
    _fill_slots(conn, 10)  # exactly one slot left

    results: list[int | None] = []
    errors: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def racer(index: int) -> None:
        own = store.connect(tmp_path / "gsc.sqlite3")
        try:
            start.wait(timeout=30)
            reservation = submit.reserve(own, f"https://example.com/{index}",
                                         PROPERTY, ACCOUNT, None,
                                         property_slots=11, account_slots=None,
                                         daily_reserve=0)
            with lock:
                results.append(reservation.submission_id)
        except Exception as exc:  # noqa: BLE001 — recorded, asserted below
            with lock:
                errors.append(type(exc).__name__)
        finally:
            own.close()

    threads = [threading.Thread(target=racer, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    # Every racer reported before the assertions below mean anything: a
    # thread still alive at the join timeout leaves `results` short, and a
    # single winner among four arrivals satisfies "exactly one" just as
    # happily as one among six.
    assert len(results) + len(errors) == 6
    assert errors == []
    assert sum(1 for r in results if r is not None) == 1
    # And the store agrees: the losers wrote nothing.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 1


# --- settle() ----------------------------------------------------------------


def test_settle_commits_a_slot_for_a_submission(conn):
    reservation = _reserve(conn, "https://example.com/a")
    submit.settle(conn, reservation.submission_id, "submitted",
                  stop_on_throttle=True)
    assert store.open_submissions(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"] == 1


def test_settle_abandons_without_charging_a_slot(conn):
    reservation = _reserve(conn, "https://example.com/a")
    submit.settle(conn, reservation.submission_id, "already_indexed",
                  stop_on_throttle=True)
    assert store.open_submissions(conn) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"] == 0
    verdict = conn.execute("SELECT verdict FROM submissions").fetchone()["verdict"]
    assert "already_indexed" in verdict


@pytest.mark.parametrize("outcome", sorted(bridge.KNOWN_OUTCOMES))
def test_the_ledger_agrees_with_the_table_for_every_outcome(conn, outcome):
    # The consequence test for the whole table, driven off the real store:
    # every outcome, reserved and settled for real, and the quota ledger
    # checked against what the table promised. An implementation that
    # returned the right Disposition but wired settle() to the wrong store
    # call passes every assertion above and fails here.
    expected = submit.DISPOSITIONS[outcome].spends_slot
    reservation = _reserve(conn, "https://example.com/a")
    submit.settle(conn, reservation.submission_id, outcome,
                  stop_on_throttle=True)

    spent = conn.execute(
        "SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"]
    assert spent == (1 if expected else 0)
    # No row is left open either way: a straggler would be reconciled later
    # and charged a slot, silently converting a no-charge outcome into one.
    assert store.open_submissions(conn) == []
    # And the capacity a LATER reserve sees moved by exactly the same amount.
    assert quota.free(conn, PROPERTY, slots=11) == (10 if expected else 11)


def test_a_settled_no_charge_outcome_frees_the_property_again(conn):
    # A run of eleven already_indexed results must not exhaust the property.
    for index in range(11):
        reservation = _reserve(conn, f"https://example.com/{index}")
        assert reservation.submission_id is not None
        submit.settle(conn, reservation.submission_id, "already_indexed",
                      stop_on_throttle=True)
    assert _reserve(conn, "https://example.com/last").submission_id is not None


def test_eleven_submitted_results_exhaust_the_property(conn):
    for index in range(11):
        reservation = _reserve(conn, f"https://example.com/{index}")
        assert reservation.submission_id is not None
        submit.settle(conn, reservation.submission_id, "submitted",
                      stop_on_throttle=True)
    assert _reserve(conn, "https://example.com/last").submission_id is None


def test_settle_returns_the_disposition_it_applied(conn):
    reservation = _reserve(conn, "https://example.com/a")
    disposition = submit.settle(conn, reservation.submission_id, "rate_limited",
                                stop_on_throttle=False)
    assert disposition.hard_stop is False
    assert disposition.spends_slot is False


def test_settle_refuses_an_unknown_outcome_without_touching_the_row(conn):
    reservation = _reserve(conn, "https://example.com/a")
    with pytest.raises(KeyError):
        submit.settle(conn, reservation.submission_id, "invented",
                      stop_on_throttle=True)
    # Neither charged nor abandoned: the row is still open, so reconcile()
    # can decide later rather than the mistake being baked in now.
    assert len(store.open_submissions(conn)) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM quota_slots").fetchone()["n"] == 0


def test_settle_on_an_unknown_submission_raises(conn):
    with pytest.raises(KeyError):
        submit.settle(conn, 9999, "submitted", stop_on_throttle=True)


def test_settling_a_timeout_charges_even_though_nothing_was_confirmed(conn):
    # The deliberate conservative bias: "timeout" means a frame reached the
    # socket, so the click may have landed. Under-counting here earns a hard
    # Quota Exceeded from Google, which the rolling window cannot undo.
    reservation = _reserve(conn, "https://example.com/a")
    submit.settle(conn, reservation.submission_id, "timeout",
                  stop_on_throttle=True)
    assert quota.free(conn, PROPERTY, slots=11) == 10


def test_settling_an_error_does_not_charge(conn):
    # The matching accepted risk, in the other direction: bridge "error" can
    # follow a send that already went out, so this under-counts in an
    # already-degraded path rather than burning slots on every aborted run.
    reservation = _reserve(conn, "https://example.com/a")
    submit.settle(conn, reservation.submission_id, "error",
                  stop_on_throttle=True)
    assert quota.free(conn, PROPERTY, slots=11) == 11


# --- hard_stop, as a run actually consumes it --------------------------------


def _drive(conn: sqlite3.Connection, urls: list[str], outcomes: list[str], *,
           stop_on_throttle: bool = True) -> list[str]:
    """A minimal stand-in for the Task 6 run loop.

    Only exists so hard_stop is tested as a run-controlling signal rather
    than as a boolean read back off the object that produced it.
    """
    processed: list[str] = []
    for url, outcome in zip(urls, outcomes, strict=True):
        reservation = _reserve(conn, url)
        if reservation.submission_id is None:
            break
        processed.append(url)
        disposition = submit.settle(conn, reservation.submission_id, outcome,
                                    stop_on_throttle=stop_on_throttle)
        if disposition.hard_stop:
            break
    return processed


def test_a_hard_stop_outcome_ends_the_run_and_leaves_later_urls_untouched(conn):
    urls = [f"https://example.com/{i}" for i in range(4)]
    processed = _drive(conn, urls, ["submitted", "auth_required",
                                    "submitted", "submitted"])
    assert processed == urls[:2]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 2
    # Only the first spent a slot; auth_required never reached the button.
    assert quota.free(conn, PROPERTY, slots=11) == 10


def test_a_rate_limit_continues_the_run_when_the_user_allows_it(conn):
    urls = [f"https://example.com/{i}" for i in range(3)]
    processed = _drive(conn, urls, ["rate_limited", "submitted", "submitted"],
                       stop_on_throttle=False)
    assert processed == urls
    assert quota.free(conn, PROPERTY, slots=11) == 9


def test_a_run_stops_itself_when_the_property_runs_out(conn):
    _fill_slots(conn, 10)
    urls = [f"https://example.com/{i}" for i in range(3)]
    processed = _drive(conn, urls, ["submitted"] * 3)
    assert processed == urls[:1]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM submissions").fetchone()["n"] == 1
