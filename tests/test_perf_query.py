# tests/test_perf_query.py
import pytest

from _fakes import FakeProvider, FakeResponse, FakeSession
from gsc_core import perf

PROPS = ["sc-domain:example.com", "https://www.example.net/"]
WINDOW = {"start_date": "2026-07-01", "end_date": "2026-07-28"}


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


def test_exhausted_retries_raise_perf_error():
    session = FakeSession(*[FakeResponse(503) for _ in range(4)])
    with pytest.raises(perf.PerfError, match="retries exhausted"):
        perf.post_query("sc-domain:example.com", {}, FakeProvider(),
                        session=session, sleep=lambda _: None)


# ------------------------------------------------------------------- query

def test_unroutable_site_raises_perf_error():
    with pytest.raises(perf.PerfError, match="no Search Console property"):
        perf.query("elsewhere.test", PROPS, FakeProvider(),
                   session=FakeSession(), **WINDOW)


def test_query_targets_the_resolved_property_url():
    session = FakeSession(FakeResponse(200, rows_payload(row())))
    perf.query("example.com", PROPS, FakeProvider(), session=session, **WINDOW)
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


# ------------------------------------------------------------------ totals

def test_totals_returns_aggregates_with_the_resolved_property():
    session = FakeSession(FakeResponse(200, rows_payload(row(clicks=9, impressions=90))))
    out = perf.totals("example.com", PROPS, FakeProvider(), session=session, **WINDOW)
    assert out["clicks"] == 9
    assert out["site"] == "sc-domain:example.com"
    assert out["start"] == "2026-07-01"


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
