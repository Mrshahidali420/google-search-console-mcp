"""Why a URL is not indexed, in a closed vocabulary.

api.classify() answers "what state is this URL in" for the whole tool
surface. This module answers the narrower question gsc_find_unindexed and
gsc_audit ask: given that state, is the page missing from the index, why,
and would spending a quota slot on it help?

TEN codes, not the eight the product spec lists. The spec's eight are all
here; the additions are discovered-not-indexed and unknown-to-google, both
of which are states where Request Indexing genuinely helps. Folding
discovered-not-indexed into crawled-not-indexed -- the nearest spec code --
would tell a caller to resubmit a page Google has already crawled and
declined, which is the one piece of advice this tool must never give. A
deviation from an eight-item list costs less than inverted advice.

This module is pure data plus three total functions. No I/O, no logging,
no clock.
"""
from __future__ import annotations

# Statuses api.classify() emits that mean the page IS in the index.
INDEXED_STATUSES = frozenset({"indexed"})

# Statuses that are not an answer either way. "unknown" is classify's
# verdict fallback; "error" is an inspection that failed; "no_property" is
# a URL no Search Console property covers (and which therefore cost no
# quota). None is evidence of absence, so none may enter the unindexed set
# -- reporting one as "unindexed, reason unknown" would be a fabricated
# finding.
UNDETERMINED_STATUSES = frozenset({"unknown", "error", "no_property"})

# The map. Keys are api.classify() statuses; values are the ten codes.
REASON_BY_STATUS: dict[str, str] = {
    "not_found": "404",
    "redirect": "redirect",
    "noindex": "noindex",
    "crawled_not_indexed": "crawled-not-indexed",
    "discovered_not_indexed": "discovered-not-indexed",
    "unknown_to_google": "unknown-to-google",
    "duplicate": "duplicate",
    "soft_404": "soft-404",
    "blocked_robots": "robots-blocked",
    "alternate_canonical": "alt-canonical",
}

REASONS = frozenset(REASON_BY_STATUS.values())

# The two states where Google has not yet judged the page: it knows the
# URL but has not crawled it, or does not know it at all. These are the
# only ones where a submission changes anything.
SUBMITTING_HELPS = frozenset({"discovered-not-indexed", "unknown-to-google"})

# Reasons that need a change to the site itself, not a tool action. Kept
# separate from SUBMITTING_HELPS because "no point submitting" and "you
# must go edit the page" are different instructions to a calling model.
NEEDS_SITE_ACCESS = frozenset({
    "404", "redirect", "noindex", "soft-404", "robots-blocked", "duplicate",
})

ACTION_BY_REASON: dict[str, str] = {
    "404": "the page is gone — remove it from the sitemap, or restore it",
    "redirect": "the URL redirects — list the destination in the sitemap "
                "instead of this one",
    "noindex": "remove the noindex directive from the page or its response "
               "header",
    "crawled-not-indexed": "Google crawled it and chose not to index it — "
                           "resubmitting will not help; improve the page or "
                           "its internal links",
    "discovered-not-indexed": "Google knows the URL but has not crawled it — "
                              "submit it for indexing",
    "unknown-to-google": "Google has never seen the URL — confirm it is in a "
                         "submitted sitemap, add internal links, then submit "
                         "it for indexing",
    "duplicate": "Google chose a different canonical — consolidate the "
                 "duplicates or set the canonical you want",
    "soft-404": "the page returns 200 but reads as empty — give it real "
                "content or return a real 404",
    "robots-blocked": "robots.txt blocks the URL — unblock it if it should "
                      "be indexed",
    # Working as intended: an alternate page with a correct canonical tag is
    # SUPPOSED to be absent from the index. Reported so a caller can see it
    # was considered, with no action attached.
    "alt-canonical": "no action — this is an alternate page pointing at its "
                     "canonical, which is working as intended",
}


def reason_for(status: str) -> str | None:
    """The reason code for an inspection status, or None.

    None means the status is not evidence of a missing page: either it is
    indexed, or it is one of the undetermined non-answers.
    """
    return REASON_BY_STATUS.get(status)


def describe(reason: str) -> dict[str, object]:
    """Everything a caller needs to act on one reason code.

    Raises KeyError for a code outside the vocabulary, deliberately: a
    reason built by interpolation is exactly what the closed set exists to
    prevent, and failing loudly here beats emitting a row with a null
    action.
    """
    if reason not in REASONS:
        raise KeyError(reason)
    return {
        "reason": reason,
        "action": ACTION_BY_REASON[reason],
        "submitting_helps": reason in SUBMITTING_HELPS,
        "needs_site_access": reason in NEEDS_SITE_ACCESS,
    }
