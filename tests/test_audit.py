from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from gsc_core import audit, reasons, store

PROPERTY = "https://example.com/"
NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def _seed(conn: sqlite3.Connection,
          rows: list[tuple[str, str | None, str | None, int | None]]) -> None:
    with store.tx(conn):
        for url, status, reason, days_ago in rows:
            checked = None if days_ago is None else store.utc_iso(
                NOW - timedelta(days=days_ago))
            store.upsert_url(conn, url, PROPERTY, status, reason, checked)


def test_an_empty_property_reports_zeros_and_a_null_percentage(store_conn):
    out = audit.audit(store_conn, PROPERTY, now=NOW)

    assert out["total_known"] == 0
    assert out["indexed"] == 0
    # Not 0.0: a percentage of nothing reads as a real measurement.
    assert out["indexed_pct"] is None


def test_counts_split_across_indexed_unindexed_and_undetermined(store_conn):
    _seed(store_conn, [
        ("https://example.com/a", "indexed", "indexed", 1),
        ("https://example.com/b", "noindex", "noindex", 1),
        ("https://example.com/c", "error", "HTTP 503", 1),
        ("https://example.com/d", None, None, None),
    ])

    out = audit.audit(store_conn, PROPERTY, now=NOW)

    assert out["total_known"] == 4
    assert out["checked"] == 3
    assert out["never_checked"] == 1
    assert out["indexed"] == 1
    assert out["unindexed"] == 1
    assert out["undetermined"] == 1
    # 1 of 3 checked, not 1 of 4 known.
    assert out["indexed_pct"] == 33.3


def test_the_reason_histogram_uses_the_ten_code_vocabulary(store_conn):
    # One URL per classifier status that maps to a distinct reason code --
    # not just the handful a smaller fixture would exercise. A bucket that
    # silently stops being counted (e.g. "duplicate" dropped from
    # by_reason) must fail this test; a fixture covering only 2-4 of the
    # ten codes cannot detect that regression.
    _seed(store_conn, [
        (f"https://example.com/{status}", status, "", 1)
        for status in reasons.REASON_BY_STATUS
    ])

    out = audit.audit(store_conn, PROPERTY, now=NOW)

    expected = {code: 1 for code in reasons.REASON_BY_STATUS.values()}
    assert out["by_reason"] == expected
    assert set(out["by_reason"]) == reasons.REASONS


def test_submittable_counts_only_where_submitting_helps(store_conn):
    _seed(store_conn, [
        ("https://example.com/a", "discovered_not_indexed", "", 1),
        ("https://example.com/b", "crawled_not_indexed", "", 1),
        ("https://example.com/c", "noindex", "", 1),
    ])

    out = audit.audit(store_conn, PROPERTY, now=NOW)

    assert out["submittable"] == 1
    assert out["needs_site_access"] == 1  # noindex; crawled needs neither


def test_alt_canonical_is_counted_as_needing_no_action(store_conn):
    _seed(store_conn, [
        ("https://example.com/a", "alternate_canonical", "", 1),
    ])

    out = audit.audit(store_conn, PROPERTY, now=NOW)

    assert out["no_action_needed"] == 1
    assert out["submittable"] == 0
    assert out["needs_site_access"] == 0


def test_staleness_is_measured_against_ttl_days(store_conn):
    _seed(store_conn, [
        ("https://example.com/fresh", "indexed", "", 1),
        ("https://example.com/old", "indexed", "", 30),
        ("https://example.com/never", None, None, None),
    ])

    out = audit.audit(store_conn, PROPERTY, ttl_days=7, now=NOW)

    # never-checked counts as stale: it has never been asked.
    assert out["stale"] == 2
    assert out["ttl_days"] == 7


def test_the_audit_reports_no_movement_fields_at_all(store_conn):
    # Ruling: point-in-time for this milestone. The store has no
    # first_indexed / de_indexed history, so these are underivable --
    # and emitting them as zero would read as "nothing changed".
    _seed(store_conn, [("https://example.com/a", "indexed", "", 1)])

    out = audit.audit(store_conn, PROPERTY, now=NOW)

    for field in ("moved_to_indexed", "de_indexed", "submitted_then_dropped",
                  "submitted_now_indexed", "monthly_breakdown"):
        assert field not in out
    assert out["basis"] == "point_in_time"


def test_pct_is_a_number_not_a_string_and_null_without_a_denominator():
    assert audit.pct(1, 2) == 50.0
    assert audit.pct(1, 3) == 33.3
    assert audit.pct(0, 0) is None
    assert audit.pct(5, 0) is None
