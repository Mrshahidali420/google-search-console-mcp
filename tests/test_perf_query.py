# tests/test_perf_query.py
from datetime import UTC, datetime

import pytest
import requests

from _fakes import FakeProvider, FakeResponse, FakeSession
from gsc_core import perf

PROPS = ["sc-domain:example.com", "https://www.example.net/"]
# Deliberately NOT a 28-day (DEFAULT_DAYS) span: if _resolve_window's
# explicit-date passthrough were ever bypassed in favour of falling back to
# date_range(days=DEFAULT_DAYS), a 28-day WINDOW would silently produce the
# identical dates and hide the bug. A 14-day span makes that mutation visible.
WINDOW = {"start_date": "2026-07-01", "end_date": "2026-07-14"}


def rows_payload(*rows):
    return {"rows": list(rows)}


def row(clicks=1, impressions=10, keys=None):
    out = {"clicks": clicks, "impressions": impressions, "ctr": 0.1, "position": 4.0}
    if keys:
        out["keys"] = keys
    return out


# -------------------------------------------------------------- post_query

def test_401_refreshes_the_token_and_retries():
    session = FakeSession(FakeResponse(401), FakeResponse(200, rows_payload()))
    provider = FakeProvider("stale")
    perf.post_query("sc-domain:example.com", {}, provider, session=session,
                    sleep=lambda _: None)
    assert provider.invalidated == 1


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
def test_transient_statuses_retry(status_code):
    session = FakeSession(FakeResponse(status_code), FakeResponse(200, rows_payload()))
    slept = []
    perf.post_query("sc-domain:example.com", {}, FakeProvider(),
                    session=session, sleep=slept.append)
    assert slept == [2]


def test_non_transient_status_raises_perf_error():
    session = FakeSession(FakeResponse(403, text="denied"))
    with pytest.raises(perf.PerfError, match="403"):
        perf.post_query("sc-domain:example.com", {}, FakeProvider(), session=session)


def test_non_transient_status_is_exposed_on_the_error():
    """PerfError.status is a log-safe fact about the failure — distinct
    from str(exc), which may carry a truncated response body."""
    session = FakeSession(FakeResponse(403, text="denied"))
    with pytest.raises(perf.PerfError) as exc_info:
        perf.post_query("sc-domain:example.com", {}, FakeProvider(), session=session)
    assert exc_info.value.status == 403


def test_exhausted_retries_raise_perf_error():
    session = FakeSession(*[FakeResponse(503) for _ in range(4)])
    with pytest.raises(perf.PerfError, match="retries exhausted"):
        perf.post_query("sc-domain:example.com", {}, FakeProvider(),
                        session=session, sleep=lambda _: None)


def test_transport_exception_backs_off_and_retries():
    """Parity with api.inspect_url: a transport exception backs off with
    2 ** attempt seconds (not the *2 transient-status formula) and retries."""
    session = FakeSession(requests.ConnectionError("boom"),
                          FakeResponse(200, rows_payload()))
    slept = []
    perf.post_query("sc-domain:example.com", {}, FakeProvider(), session=session,
                    sleep=slept.append)
    assert slept == [1]


def test_401_consumes_no_sleep():
    """Parity with api.inspect_url: an expired token is not a rate-limit
    signal, so retrying past a 401 must not spend a backoff sleep."""
    session = FakeSession(FakeResponse(401), FakeResponse(200, rows_payload()))
    slept = []
    perf.post_query("sc-domain:example.com", {}, FakeProvider(), session=session,
                    sleep=slept.append)
    assert slept == []


# ------------------------------------------------------------------- query

def test_unroutable_site_raises_perf_error():
    with pytest.raises(perf.PerfError, match="no Search Console property"):
        perf.query("elsewhere.test", PROPS, FakeProvider(),
                   session=FakeSession(), **WINDOW)


def test_query_targets_the_resolved_property_url():
    session = FakeSession(FakeResponse(200, rows_payload(row())))
    perf.query("example.com", PROPS, FakeProvider(), session=session, **WINDOW)
    assert "sc-domain%3Aexample.com" in session.calls[0]["url"]


def test_query_accepts_the_exact_property_string_as_site():
    """gsc_list_sites() returns property strings, not page URLs — the most
    plausible thing an AI assistant hands back in as `site`. This must
    resolve via routing.resolve_property's identity pass, not raise."""
    session = FakeSession(FakeResponse(200, rows_payload(row())))
    perf.query("sc-domain:example.com", PROPS, FakeProvider(), session=session,
              **WINDOW)
    assert "sc-domain%3Aexample.com" in session.calls[0]["url"]


def test_rows_are_normalized_with_dimension_names():
    session = FakeSession(FakeResponse(200, rows_payload(row(keys=["shoes"]))))
    out = perf.query("example.com", PROPS, FakeProvider(), dimensions=["query"],
                     session=session, **WINDOW)
    assert out[0]["query"] == "shoes"


def test_a_short_page_ends_pagination():
    session = FakeSession(FakeResponse(200, rows_payload(row(), row())))
    out = perf.query("example.com", PROPS, FakeProvider(), limit=0,
                     session=session, **WINDOW)
    assert len(out) == 2
    assert len(session.calls) == 1


def test_a_full_page_triggers_another_request():
    full = rows_payload(*[row() for _ in range(perf.MAX_ROW_LIMIT)])
    session = FakeSession(FakeResponse(200, full), FakeResponse(200, rows_payload(row())))
    out = perf.query("example.com", PROPS, FakeProvider(), limit=0,
                     session=session, **WINDOW)
    assert len(session.calls) == 2
    assert session.calls[1]["json"]["startRow"] == perf.MAX_ROW_LIMIT
    assert len(out) == perf.MAX_ROW_LIMIT + 1


def test_limit_caps_the_rows_requested():
    session = FakeSession(FakeResponse(200, rows_payload(row())))
    perf.query("example.com", PROPS, FakeProvider(), limit=5,
               session=session, **WINDOW)
    assert session.calls[0]["json"]["rowLimit"] == 5


def test_invalid_dimension_is_rejected_before_any_request():
    session = FakeSession()
    with pytest.raises(ValueError):
        perf.query("example.com", PROPS, FakeProvider(), dimensions=["nope"],
                   session=session, **WINDOW)
    assert session.calls == []


def test_query_forwards_the_sleep_seam():
    """A retry inside the pagination loop must honour the caller's injected
    sleep, not fall back to the real clock three layers down."""
    session = FakeSession(FakeResponse(503), FakeResponse(200, rows_payload(row())))
    slept = []
    perf.query("example.com", PROPS, FakeProvider(), session=session,
              sleep=slept.append, **WINDOW)
    assert slept == [2]


# ------------------------------------------------------------------ totals

def test_totals_returns_aggregates_with_the_resolved_property():
    session = FakeSession(FakeResponse(200, rows_payload(row(clicks=9, impressions=90))))
    out = perf.totals("example.com", PROPS, FakeProvider(), session=session, **WINDOW)
    assert out["clicks"] == 9
    assert out["site"] == "sc-domain:example.com"
    assert out["start"] == "2026-07-01"


def test_totals_issues_exactly_one_http_call():
    """Pins _paginate's `page_size <= 0` break: a single full (non-short)
    row exactly exhausts limit=1, and the loop must stop there rather than
    issuing a second page request for a limit that is already spent."""
    session = FakeSession(FakeResponse(200, rows_payload(row(clicks=9, impressions=90))))
    perf.totals("example.com", PROPS, FakeProvider(), session=session, **WINDOW)
    assert len(session.calls) == 1


# --------------------------------------------------------------- portfolio

def test_portfolio_covers_every_property():
    session = FakeSession(*[FakeResponse(200, rows_payload(row())) for _ in range(2)])
    out = perf.portfolio(PROPS, FakeProvider(), session=session, concurrency=1, **WINDOW)
    assert len(out) == 2


def test_portfolio_sorts_busiest_first():
    session = FakeSession(
        FakeResponse(200, rows_payload(row(clicks=1, impressions=10))),
        FakeResponse(200, rows_payload(row(clicks=50, impressions=500))),
    )
    out = perf.portfolio(PROPS, FakeProvider(), session=session, concurrency=1, **WINDOW)
    assert out[0]["clicks"] == 50


def test_one_failing_property_does_not_sink_the_run():
    session = FakeSession(
        FakeResponse(403, text="no access"),
        FakeResponse(200, rows_payload(row(clicks=4))),
    )
    out = perf.portfolio(PROPS, FakeProvider(), session=session, concurrency=1, **WINDOW)
    assert len(out) == 2
    assert any("error" in r for r in out)
    assert any(r.get("clicks") == 4 for r in out)


def test_portfolio_failure_log_carries_status_but_not_body(caplog):
    """A truncated response body is fine to return to a caller (it lands in
    the row's "error" key) but must never reach a logger — dropping the
    status code along with the body would be an over-correction, since a
    403 and a 429 then read identically in the log."""
    session = FakeSession(
        FakeResponse(403, text="SECRET-BODY-user@example.com quota detail"),
        FakeResponse(200, rows_payload(row(clicks=4))),
    )
    with caplog.at_level("WARNING", logger="gsc.gsc_core.perf"):
        out = perf.portfolio(PROPS, FakeProvider(), session=session, concurrency=1,
                             **WINDOW)
    assert any("SECRET-BODY" in r["error"] for r in out if "error" in r)
    assert "403" in caplog.text
    assert "SECRET-BODY" not in caplog.text


def test_portfolio_forwards_the_sleep_seam():
    session = FakeSession(FakeResponse(503), FakeResponse(200, rows_payload(row())))
    slept = []
    perf.portfolio(["sc-domain:example.com"], FakeProvider(), session=session,
                   concurrency=1, sleep=slept.append, **WINDOW)
    assert slept == [2]


# ------------------------------------------------------------------ hourly

def test_hourly_uses_the_hour_dimension_and_hourly_data_state():
    session = FakeSession(FakeResponse(200, rows_payload()))
    perf.hourly("example.com", PROPS, FakeProvider(), hours=24, session=session)
    body = session.calls[0]["json"]
    assert body["dimensions"] == [perf.HOUR_DIMENSION]
    assert body["dataState"] == perf.HOURLY_DATA_STATE


def test_hourly_rejects_a_window_beyond_what_google_retains():
    with pytest.raises(ValueError):
        perf.hourly("example.com", PROPS, FakeProvider(),
                    hours=perf.HOURLY_WINDOW_DAYS * 24 + 1, session=FakeSession())


def test_hourly_targets_the_resolved_property_url():
    session = FakeSession(FakeResponse(200, rows_payload()))
    perf.hourly("example.com", PROPS, FakeProvider(), hours=24, session=session)
    assert "sc-domain%3Aexample.com" in session.calls[0]["url"]


def test_hourly_unroutable_site_raises_perf_error():
    with pytest.raises(perf.PerfError, match="no Search Console property"):
        perf.hourly("elsewhere.test", PROPS, FakeProvider(), hours=24,
                    session=FakeSession())


def test_hourly_requests_the_maximum_row_limit():
    session = FakeSession(FakeResponse(200, rows_payload()))
    perf.hourly("example.com", PROPS, FakeProvider(), hours=24, session=session)
    assert session.calls[0]["json"]["rowLimit"] == perf.MAX_ROW_LIMIT


def test_hourly_pins_the_calendar_span():
    session = FakeSession(FakeResponse(200, rows_payload()))
    perf.hourly("example.com", PROPS, FakeProvider(), hours=24, session=session,
               now=datetime(2020, 1, 15, 12, 0, tzinfo=UTC))
    body = session.calls[0]["json"]
    assert body["startDate"] == "2020-01-14"
    assert body["endDate"] == "2020-01-15"


def test_hourly_normalizes_rows_under_the_hour_key():
    payload = rows_payload(row(keys=["2020-01-15T10:00:00+00:00"]))
    session = FakeSession(FakeResponse(200, payload))
    out = perf.hourly("example.com", PROPS, FakeProvider(), hours=24, session=session,
                      now=datetime(2020, 1, 15, 12, 0, tzinfo=UTC))
    assert out[0]["hour"] == "2020-01-15T10:00:00+00:00"


def test_hourly_sub_24_hour_window_returns_rows_not_a_whole_calendar_day():
    """Calendar-day semantics (the reverted bug) anchor "now" at the end of
    a date and measure the cutoff from there, so any window under 24 hours
    excludes rows that sit earlier in the day than that artificial cutoff —
    0 rows, every time. A true rolling window measured from the actual
    instant `now` keeps them."""
    now = datetime(2026, 1, 2, 10, 30, tzinfo=UTC)
    session = FakeSession(FakeResponse(
        200, rows_payload(row(keys=["2026-01-02T09:00:00+00:00"]))))
    out = perf.hourly("example.com", PROPS, FakeProvider(), hours=3,
                      session=session, now=now)
    assert len(out) == 1


def test_hourly_24_hour_window_spans_two_calendar_dates():
    """Production's rolling 24-hour window crosses midnight; a date-only
    seam cannot express that at all — this is what proves the seam is a
    true rolling window and not calendar-day semantics wearing a
    trailing-window name."""
    now = datetime(2026, 1, 2, 10, 30, tzinfo=UTC)
    rows = (
        [row(keys=[f"2026-01-01T{h:02d}:00:00+00:00"]) for h in range(24)]
        + [row(keys=[f"2026-01-02T{h:02d}:00:00+00:00"]) for h in range(11)]
    )
    session = FakeSession(FakeResponse(200, rows_payload(*rows)))
    out = perf.hourly("example.com", PROPS, FakeProvider(), hours=24,
                      session=session, now=now)
    dates_seen = {r["hour"][:10] for r in out}
    assert len(out) == 24
    assert dates_seen == {"2026-01-01", "2026-01-02"}
