import pytest
import requests

from _fakes import FakeProvider, FakeResponse, FakeSession, indexed_payload
from gsc_core import api, gauth


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("coverage,expected", [
    ("Submitted and indexed", "indexed"),
    ("Indexed, not submitted in sitemap", "indexed"),
    ("Crawled - currently not indexed", "crawled_not_indexed"),
    ("Discovered - currently not indexed", "discovered_not_indexed"),
    ("URL is unknown to Google", "unknown_to_google"),
    ("Page with redirect", "redirect"),
    ("Excluded by 'noindex' tag", "noindex"),
    ("Duplicate without user-selected canonical", "duplicate"),
    ("Duplicate, Google chose different canonical than user", "duplicate"),
    ("Alternate page with proper canonical tag", "alternate_canonical"),
    ("Not found (404)", "not_found"),
    ("Soft 404", "soft_404"),
    ("Blocked by robots.txt", "blocked_robots"),
])
def test_every_known_coverage_state_maps(coverage, expected):
    status, _ = api.classify(indexed_payload(coverage))
    assert status == expected


def test_unknown_coverage_with_pass_verdict_is_indexed():
    assert api.classify(indexed_payload("Some new wording", "PASS"))[0] == "indexed"


def test_unknown_coverage_without_pass_verdict_is_unknown():
    assert api.classify(indexed_payload("Some new wording", "NEUTRAL"))[0] == "unknown"


def test_empty_payload_does_not_raise():
    status, detail = api.classify({})
    assert status == "unknown"
    assert detail == "no indexStatusResult"


def test_detail_carries_the_last_crawl_date_only():
    _, detail = api.classify(
        indexed_payload(crawl="2026-07-15T09:31:00Z"))
    assert detail.endswith("| last crawl 2026-07-15")


# -------------------------------------------------------------- inspect_url

def test_inspect_returns_status_and_detail():
    session = FakeSession(FakeResponse(200, indexed_payload()))
    status, _ = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                FakeProvider(), session=session)
    assert status == "indexed"


def test_inspect_sends_the_url_and_property_in_the_body():
    session = FakeSession(FakeResponse(200, indexed_payload()))
    api.inspect_url("https://example.com/a", "sc-domain:example.com",
                    FakeProvider(), session=session)
    assert session.calls[0]["json"] == {
        "inspectionUrl": "https://example.com/a",
        "siteUrl": "sc-domain:example.com",
    }


def test_inspect_sends_a_bearer_token():
    session = FakeSession(FakeResponse(200, indexed_payload()))
    api.inspect_url("https://example.com/a", "sc-domain:example.com",
                    FakeProvider("abc"), session=session)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer abc"


def test_401_invalidates_the_token_and_retries_with_the_new_one():
    session = FakeSession(FakeResponse(401), FakeResponse(200, indexed_payload()))
    provider = FakeProvider("stale")
    slept = []
    status, _ = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                provider, session=session, sleep=slept.append)
    assert status == "indexed"
    assert provider.invalidated == 1
    assert session.calls[1]["headers"]["Authorization"] == "Bearer fresh"
    assert slept == [], "a 401 refresh must not consume a backoff sleep"


@pytest.mark.parametrize("status_code", [429, 500, 502, 503])
def test_transient_statuses_back_off_and_retry(status_code):
    session = FakeSession(FakeResponse(status_code), FakeResponse(200, indexed_payload()))
    slept = []
    status, _ = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                FakeProvider(), session=session, sleep=slept.append)
    assert status == "indexed"
    assert slept == [2]


def test_non_transient_status_returns_error_without_retrying():
    session = FakeSession(FakeResponse(403, text="forbidden"))
    status, detail = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                     FakeProvider(), session=session)
    assert status == "error"
    assert "403" in detail
    assert len(session.calls) == 1


def test_exhausted_retries_report_error():
    session = FakeSession(*[FakeResponse(503) for _ in range(4)])
    status, detail = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                     FakeProvider(), session=session,
                                     max_retries=4, sleep=lambda _: None)
    assert status == "error"
    assert "retries exhausted" in detail
    # Pins the attempt count exactly: asserting only on the eventual error
    # message cannot tell four attempts from three (or five) — an
    # off-by-one in the loop bound still produces the same status/detail.
    assert len(session.calls) == 4


def test_transport_exception_backs_off_and_retries():
    session = FakeSession(requests.ConnectionError("boom"),
                          FakeResponse(200, indexed_payload()))
    slept = []
    status, _ = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                FakeProvider(), session=session, sleep=slept.append)
    assert status == "indexed"
    # 2 ** attempt, deliberately different from the transient-status
    # formula (2 ** attempt * 2) — a test that can't tell the two apart
    # would still pass if this branch used the wrong backoff.
    assert slept == [1]


def test_transport_exception_on_last_attempt_returns_error():
    session = FakeSession(*[requests.ConnectionError("boom") for _ in range(4)])
    status, detail = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                     FakeProvider(), session=session,
                                     max_retries=4, sleep=lambda _: None)
    assert status == "error"
    assert "request failed" in detail
    assert "boom" in detail


def test_error_detail_is_truncated_to_200_characters():
    session = FakeSession(FakeResponse(400, text="x" * 5000))
    _, detail = api.inspect_url("https://example.com/a", "sc-domain:example.com",
                                FakeProvider(), session=session)
    assert len(detail) < 260


# ----------------------------------------------------------- list_properties

def test_list_properties_returns_site_entries():
    session = FakeSession(FakeResponse(200, {"siteEntry": [
        {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}]}))
    props = api.list_properties(FakeProvider(), session=session)
    assert props == [{"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}]


def test_list_properties_on_an_empty_account_returns_an_empty_list():
    session = FakeSession(FakeResponse(200, {}))
    assert api.list_properties(FakeProvider(), session=session) == []


def test_list_properties_raises_auth_required_on_401():
    session = FakeSession(FakeResponse(401, {}))
    with pytest.raises(gauth.AuthRequired):
        api.list_properties(FakeProvider(), session=session)


@pytest.mark.parametrize("status", [403, 404, 429, 500, 503])
def test_list_properties_raises_api_error_on_any_other_non_200(status):
    """A refused call must never be indistinguishable from an empty account.

    403 is the one that matters most: a token without the webmasters scope
    used to come back as [], which gsc_check_status then reports as every
    URL having no matching property — telling the user their sites are
    unknown to Google when the call was simply rejected.
    """
    session = FakeSession(FakeResponse(status, {"error": {"code": status}}))
    with pytest.raises(api.ApiError) as caught:
        api.list_properties(FakeProvider(), session=session)
    assert caught.value.status == status


def test_list_properties_api_error_carries_no_response_body():
    """ApiError's text can reach a log; a response body must not."""
    session = FakeSession(FakeResponse(403, {}, text="secret-ish body"))
    with pytest.raises(api.ApiError) as caught:
        api.list_properties(FakeProvider(), session=session)
    assert "secret-ish" not in str(caught.value)


def test_list_properties_raises_api_error_when_a_200_body_is_not_json():
    class NotJson(FakeResponse):
        def json(self):
            raise ValueError("no json")

    session = FakeSession(NotJson(200))
    with pytest.raises(api.ApiError):
        api.list_properties(FakeProvider(), session=session)


# ------------------------------------------------------------------ sitemaps

def test_submit_sitemap_uses_put_and_reports_ok():
    session = FakeSession(FakeResponse(204))
    out = api.submit_sitemap("sc-domain:example.com",
                             "https://example.com/sitemap.xml",
                             FakeProvider(), session=session)
    assert session.calls[0]["method"] == "PUT"
    assert out["ok"] is True
    assert out["http_status"] == 204


def test_submit_sitemap_percent_encodes_the_property_and_sitemap():
    session = FakeSession(FakeResponse(204))
    api.submit_sitemap("sc-domain:example.com", "https://example.com/sitemap.xml",
                       FakeProvider(), session=session)
    url = session.calls[0]["url"]
    assert "sc-domain%3Aexample.com" in url
    assert "https%3A%2F%2Fexample.com%2Fsitemap.xml" in url


def test_submit_sitemap_failure_carries_a_note():
    session = FakeSession(FakeResponse(404, text="not found"))
    out = api.submit_sitemap("sc-domain:example.com", "https://example.com/s.xml",
                             FakeProvider(), session=session)
    assert out["ok"] is False
    assert out["note"]


def test_list_sitemaps_returns_the_sitemap_array():
    session = FakeSession(FakeResponse(200, {"sitemap": [{"path": "https://example.com/s.xml"}]}))
    assert api.list_sitemaps("sc-domain:example.com", FakeProvider(),
                             session=session) == [{"path": "https://example.com/s.xml"}]


def test_the_default_retry_budget_is_four_attempts():
    """The existing exhaustion tests pass max_retries=4 explicitly, so the
    DEFAULT was pinned by nothing: changing the signature to 1 or to 40 left
    them green. Real callers -- check_status among them -- never pass it, so
    the default is the number that actually ships. Omitting it here is the
    point of the test."""
    session = FakeSession(*[FakeResponse(503) for _ in range(10)])
    status, detail = api.inspect_url("https://example.com/a",
                                     "sc-domain:example.com", FakeProvider(),
                                     session=session, sleep=lambda _: None)
    assert status == "error"
    assert "retries exhausted" in detail
    assert len(session.calls) == 4
