# tests/test_perf_logic.py
from datetime import UTC, date, datetime

import pytest

from gsc_core import perf


# ------------------------------------------------------------- date_range

def test_window_ends_yesterday_not_today():
    start, end = perf.date_range(7, today=date(2026, 8, 2))
    assert end == "2026-08-01"


def test_window_is_inclusive_of_both_ends():
    start, end = perf.date_range(28, today=date(2026, 8, 2))
    assert start == "2026-07-05"
    assert end == "2026-08-01"
    assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 27


def test_explicit_end_date_is_honoured():
    start, end = perf.date_range(3, end="2026-07-10")
    assert (start, end) == ("2026-07-08", "2026-07-10")


def test_zero_or_negative_days_is_rejected():
    with pytest.raises(ValueError):
        perf.date_range(0)


# ---------------------------------------------------------- validate_query

def test_unknown_dimension_is_rejected():
    with pytest.raises(ValueError, match="dimension"):
        perf.validate_query(["nonsense"], "all", "web")


def test_hourly_all_is_rejected_for_ordinary_queries():
    with pytest.raises(ValueError, match="data_state"):
        perf.validate_query(["date"], "hourly_all", "web")


def test_unknown_search_type_is_rejected():
    with pytest.raises(ValueError, match="type"):
        perf.validate_query(None, "all", "audio")


def test_valid_combination_passes():
    assert perf.validate_query(["query", "page"], "final", "image") is None


# ---------------------------------------------------------- normalize_row

def test_keys_are_zipped_onto_dimension_names():
    row = {"keys": ["shoes", "https://example.com/a"], "clicks": 3,
           "impressions": 90, "ctr": 0.033, "position": 7.5}
    out = perf.normalize_row(row, ["query", "page"])
    assert out["query"] == "shoes"
    assert out["page"] == "https://example.com/a"
    assert out["clicks"] == 3


def test_a_keyless_totals_row_normalizes():
    out = perf.normalize_row({"clicks": 5, "impressions": 100,
                              "ctr": 0.05, "position": 3.0}, None)
    assert out == {"clicks": 5, "impressions": 100, "ctr": 0.05, "position": 3.0}


def test_missing_metrics_default_to_zero():
    out = perf.normalize_row({}, None)
    assert out == {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}


# ---------------------------------------------------------- aggregate_rows

def test_clicks_and_impressions_sum():
    rows = [{"clicks": 3, "impressions": 100, "position": 5.0},
            {"clicks": 7, "impressions": 300, "position": 9.0}]
    out = perf.aggregate_rows(rows)
    assert out["clicks"] == 10
    assert out["impressions"] == 400


def test_ctr_is_recomputed_over_the_whole_set_not_averaged():
    rows = [{"clicks": 3, "impressions": 100, "ctr": 0.03, "position": 5.0},
            {"clicks": 7, "impressions": 300, "ctr": 0.023, "position": 9.0}]
    assert perf.aggregate_rows(rows)["ctr"] == pytest.approx(10 / 400)


def test_position_is_impression_weighted_not_a_plain_mean():
    """A plain mean would give 7.0 and read far too optimistic."""
    rows = [{"clicks": 0, "impressions": 100, "position": 5.0},
            {"clicks": 0, "impressions": 300, "position": 9.0}]
    assert perf.aggregate_rows(rows)["position"] == pytest.approx(8.0)


def test_zero_impressions_does_not_divide_by_zero():
    out = perf.aggregate_rows([{"clicks": 0, "impressions": 0, "position": 0.0}])
    assert out["ctr"] == 0.0
    assert out["position"] == 0.0


def test_empty_rows_aggregate_to_zeroes():
    assert perf.aggregate_rows([]) == {"clicks": 0, "impressions": 0,
                                       "ctr": 0.0, "position": 0.0}


# -------------------------------------------------------------- build_body

def test_body_carries_the_window_and_defaults_to_data_state_all():
    body = perf.build_body("2026-07-01", "2026-07-28")
    assert body["startDate"] == "2026-07-01"
    assert body["endDate"] == "2026-07-28"
    assert body["dataState"] == "all"


def test_dimensions_are_omitted_when_not_requested():
    assert "dimensions" not in perf.build_body("2026-07-01", "2026-07-28")


def test_row_limit_is_clamped_to_the_api_maximum():
    assert perf.build_body("2026-07-01", "2026-07-28",
                           row_limit=999_999)["rowLimit"] == perf.MAX_ROW_LIMIT


def test_row_limit_is_at_least_one():
    assert perf.build_body("2026-07-01", "2026-07-28", row_limit=0)["rowLimit"] == 1


def test_filters_become_dimension_filter_groups():
    groups = [{"filters": [{"dimension": "page", "expression": "/blog"}]}]
    assert perf.build_body("2026-07-01", "2026-07-28",
                           filters=groups)["dimensionFilterGroups"] == groups


# ------------------------------------------------------- filter_hourly_rows

def test_rows_outside_the_window_are_dropped():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    rows = [{"hour": "2026-08-02T11:00:00+00:00"},
            {"hour": "2026-08-01T11:00:00+00:00"}]
    kept = perf.filter_hourly_rows(rows, hours=6, now=now)
    assert len(kept) == 1


def test_rows_are_returned_oldest_first():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    rows = [{"hour": "2026-08-02T11:00:00+00:00"},
            {"hour": "2026-08-02T09:00:00+00:00"}]
    kept = perf.filter_hourly_rows(rows, hours=6, now=now)
    assert [r["hour"] for r in kept] == ["2026-08-02T09:00:00+00:00",
                                         "2026-08-02T11:00:00+00:00"]


def test_an_unparseable_timestamp_is_kept_not_silently_dropped():
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    kept = perf.filter_hourly_rows([{"hour": "not-a-time"}], hours=6, now=now)
    assert len(kept) == 1
