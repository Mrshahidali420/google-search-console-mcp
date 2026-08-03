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


def test_the_non_answers_are_undetermined_not_reasons():
    # "unknown" is classify's verdict fallback, "error" is a failed
    # inspection, "no_property" is a URL outside every property, and
    # "skipped_quota" is one the inspection gate refused. None of them is
    # evidence that a page is not indexed, so none may appear in the
    # unindexed set.
    assert reasons.UNDETERMINED_STATUSES == frozenset(
        {"unknown", "error", "no_property", "skipped_quota"})
    for status in reasons.UNDETERMINED_STATUSES:
        assert reasons.reason_for(status) is None


def test_a_quota_skip_is_undetermined_but_named_apart_from_a_real_unknown():
    # api._rows (api.py:666-668) mints "skipped_quota" outside
    # api.COVERAGE_MAP, which is why the sweep above cannot see it. It is
    # undetermined -- but "we never got to look" is answered by running
    # again tomorrow, and "we looked and could not tell" is not, so the two
    # stay distinguishable by name.
    assert reasons.NOT_INSPECTED_STATUSES == frozenset({"skipped_quota"})
    assert reasons.NOT_INSPECTED_STATUSES <= reasons.UNDETERMINED_STATUSES
    assert not (reasons.NOT_INSPECTED_STATUSES & reasons.INDEXED_STATUSES)
    assert not (reasons.NOT_INSPECTED_STATUSES
                & set(reasons.REASON_BY_STATUS))


def test_submitting_helps_names_every_member_it_has():
    # Literal equality, not a subset check, for the same reason
    # NEEDS_SITE_ACCESS below gets one: dropping a member silently flips
    # that code to `submitting_helps: false` and drops it out of
    # gsc_audit's submittable bucket, with no other signal anywhere.
    # crawled-not-indexed is a member: Google fetching a page and passing
    # on it is not a permanent verdict, and a re-crawl can reverse it.
    assert reasons.SUBMITTING_HELPS == frozenset(
        {"discovered-not-indexed", "unknown-to-google",
         "crawled-not-indexed"})


def test_crawled_and_discovered_order_the_work_differently():
    # The whole reason the vocabulary is ten codes rather than the spec's
    # eight. Both are submittable, so `submitting_helps` alone cannot tell
    # them apart -- what differs is the sequence, and collapsing the codes
    # would lose it: discovered needs only the submission, crawled needs
    # the page improved BEFORE the slot is worth spending.
    crawled = reasons.describe("crawled-not-indexed")
    discovered = reasons.describe("discovered-not-indexed")
    assert discovered["submitting_helps"] is True
    assert crawled["submitting_helps"] is True
    assert crawled["action"] != discovered["action"]
    # Not merely different strings: crawled's action must actually carry
    # the precondition, or the two codes collapse in the only place a
    # caller reads them apart.
    assert "then submit" in crawled["action"]


def test_every_reason_has_a_non_empty_action():
    for reason in reasons.REASONS:
        assert reasons.ACTION_BY_REASON[reason].strip()


def test_needs_site_access_is_a_subset_of_the_reasons():
    assert reasons.NEEDS_SITE_ACCESS <= reasons.REASONS

def test_needs_site_access_names_every_member_it_has():
    # Literal equality, not a subset check. Gutting this set to
    # {noindex, duplicate} left all 1187 tests green: 404, redirect,
    # soft-404 and robots-blocked would then report
    # `needs_site_access: false` and vanish from gsc_audit's buckets with
    # no signal anywhere. Removing any single member must redden this.
    assert reasons.NEEDS_SITE_ACCESS == frozenset({
        "404", "redirect", "noindex", "soft-404", "robots-blocked",
        "duplicate",
    })


def test_a_reason_that_needs_site_access_is_never_one_submitting_helps():
    # A page you must edit is not a page a quota slot fixes.
    assert not (reasons.NEEDS_SITE_ACCESS & reasons.SUBMITTING_HELPS)


def test_describe_refuses_an_unknown_reason():
    with pytest.raises(KeyError):
        reasons.describe("not-a-real-reason")
