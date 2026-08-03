from __future__ import annotations

import pytest

from gsc_core import api, reasons


def test_there_are_exactly_ten_reason_codes():
    assert reasons.REASONS == frozenset({
        "404", "redirect", "noindex", "crawled-not-indexed",
        "discovered-not-indexed", "unknown-to-google", "duplicate",
        "soft-404", "robots-blocked", "alt-canonical",
    })


def test_every_status_the_classifier_can_produce_has_exactly_one_home():
    # api.COVERAGE_MAP is the full vocabulary of statuses classify() emits
    # from a coverage state. Every one must be indexed, undetermined, or
    # carry a reason -- and never two of those. A new coverage state added
    # to api.py without a reason here fails this test rather than silently
    # dropping URLs out of the unindexed set.
    produced = set(api.COVERAGE_MAP.values())
    for status in produced:
        homes = [
            status in reasons.INDEXED_STATUSES,
            status in reasons.UNDETERMINED_STATUSES,
            status in reasons.REASON_BY_STATUS,
        ]
        assert sum(homes) == 1, f"{status} has {sum(homes)} homes, want 1"


def test_the_three_non_answers_are_undetermined_not_reasons():
    # "unknown" is classify's verdict fallback, "error" is a failed
    # inspection, "no_property" is a URL outside every property. None of
    # them is evidence that a page is not indexed, so none may appear in
    # the unindexed set.
    assert reasons.UNDETERMINED_STATUSES == frozenset(
        {"unknown", "error", "no_property"})
    for status in reasons.UNDETERMINED_STATUSES:
        assert reasons.reason_for(status) is None


def test_submitting_helps_only_where_google_has_not_looked_yet():
    assert reasons.SUBMITTING_HELPS == frozenset(
        {"discovered-not-indexed", "unknown-to-google"})


def test_crawled_and_discovered_give_opposite_advice():
    # The whole reason the vocabulary is ten codes rather than the spec's
    # eight. Collapsing these two would tell a caller to spend an
    # unrecoverable quota slot on a page Google has already declined.
    crawled = reasons.describe("crawled-not-indexed")
    discovered = reasons.describe("discovered-not-indexed")
    assert discovered["submitting_helps"] is True
    assert crawled["submitting_helps"] is False
    assert crawled["action"] != discovered["action"]


def test_every_reason_has_a_non_empty_action():
    for reason in reasons.REASONS:
        assert reasons.ACTION_BY_REASON[reason].strip()


def test_needs_site_access_is_a_subset_of_the_reasons():
    assert reasons.NEEDS_SITE_ACCESS <= reasons.REASONS


def test_a_reason_that_needs_site_access_is_never_one_submitting_helps():
    # A page you must edit is not a page a quota slot fixes.
    assert not (reasons.NEEDS_SITE_ACCESS & reasons.SUBMITTING_HELPS)


def test_describe_refuses_an_unknown_reason():
    with pytest.raises(KeyError):
        reasons.describe("not-a-real-reason")
