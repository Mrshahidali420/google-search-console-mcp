import pytest

from gsc_core import routing

DOMAIN = "sc-domain:example.com"
PREFIX = "https://www.example.net/"
BARE = "https://example.org/"


def test_host_of_lowercases_and_strips_port():
    assert routing.host_of("https://WWW.Example.COM:8443/a/b") == "www.example.com"


def test_host_of_strips_userinfo():
    assert routing.host_of("https://user:pw@example.com/x") == "example.com"


def test_host_of_accepts_a_bare_host_without_a_scheme():
    assert routing.host_of("example.com/page") == "example.com"


def test_url_prefix_property_matches_its_exact_host():
    assert routing.resolve_property("https://www.example.net/a", [PREFIX]) == PREFIX


def test_www_toggle_matches_in_both_directions():
    assert routing.resolve_property("https://example.net/a", [PREFIX]) == PREFIX
    assert routing.resolve_property("https://www.example.org/a", [BARE]) == BARE


def test_domain_property_covers_the_bare_domain():
    assert routing.resolve_property("https://example.com/a", [DOMAIN]) == DOMAIN


def test_domain_property_covers_any_subdomain():
    assert routing.resolve_property("https://shop.eu.example.com/a", [DOMAIN]) == DOMAIN


def test_domain_property_does_not_cover_a_lookalike_suffix():
    """notexample.com must NOT match sc-domain:example.com."""
    assert routing.resolve_property("https://notexample.com/a", [DOMAIN]) is None


def test_domain_property_matches_regardless_of_its_own_case():
    """Search Console does not guarantee the case of the domain half of an
    sc-domain: property string, so step 3 matching must not be case-sensitive."""
    assert routing.resolve_property(
        "https://example.com/a", ["sc-domain:EXAMPLE.com"]
    ) == "sc-domain:EXAMPLE.com"


def test_unmatched_host_returns_none():
    assert routing.resolve_property("https://elsewhere.test/a", [DOMAIN, PREFIX]) is None


def test_most_specific_domain_property_wins_regardless_of_order():
    """A host covered by two sc-domain properties routes to the longer one, and
    the answer must not depend on the order the properties arrive in."""
    broad, narrow = "sc-domain:example.com", "sc-domain:shop.example.com"
    url = "https://cart.shop.example.com/x"
    assert routing.resolve_property(url, [broad, narrow]) == narrow
    assert routing.resolve_property(url, [narrow, broad]) == narrow


def test_exact_host_beats_a_domain_property():
    """Match order is exact-host first, so the URL-prefix property wins."""
    prefix = "https://sub.example.com/"
    assert routing.resolve_property("https://sub.example.com/a",
                                    [DOMAIN, prefix]) == prefix


def test_bare_host_suffix_matches_a_deeper_subdomain():
    """Step 4: a URL-prefix property's host can cover a deeper subdomain,
    the same way an sc-domain: property does at step 3. No sc-domain
    property is present, so this can only resolve at step 4."""
    prefix = "https://example.com/"
    assert routing.resolve_property("https://blog.example.com/a", [prefix]) == prefix


def test_most_specific_bare_host_suffix_wins_regardless_of_order():
    """Step 4's tie-break: when two URL-prefix properties both cover the
    host as a suffix, the longer (more specific) one wins, independent of
    the order the properties arrive in."""
    broad, narrow = "https://example.com/", "https://shop.example.com/"
    url = "https://cart.shop.example.com/x"
    assert routing.resolve_property(url, [broad, narrow]) == narrow
    assert routing.resolve_property(url, [narrow, broad]) == narrow


def test_route_all_preserves_input_order_and_flags_misses():
    urls = ["https://example.com/a", "https://elsewhere.test/b"]
    assert routing.route_all(urls, [DOMAIN]) == [
        ("https://example.com/a", DOMAIN),
        ("https://elsewhere.test/b", None),
    ]


def test_empty_property_list_matches_nothing():
    assert routing.resolve_property("https://example.com/a", []) is None


# --------------------------------------------------------- identity match

def test_sc_domain_property_resolves_against_its_own_exact_string():
    """sc-domain: properties are not URLs — host_of() cannot parse one, so
    without an identity pass this can never resolve at all."""
    assert routing.resolve_property(DOMAIN, [DOMAIN]) == DOMAIN


def test_url_prefix_property_resolves_against_its_own_exact_string():
    assert routing.resolve_property(PREFIX, [PREFIX]) == PREFIX


def test_identity_match_is_case_insensitive():
    assert routing.resolve_property(
        "SC-DOMAIN:EXAMPLE.COM", ["sc-domain:example.com"]
    ) == "sc-domain:example.com"


def test_identity_match_does_not_disturb_host_based_matching():
    """The identity pass is additive: ordinary page URLs still resolve
    exactly as before, through the host-based steps."""
    assert routing.resolve_property("https://example.com/a", [DOMAIN]) == DOMAIN
    assert routing.resolve_property("https://blog.example.com/a", [DOMAIN]) == DOMAIN
    assert routing.resolve_property("https://elsewhere.test/a", [DOMAIN, PREFIX]) is None
    assert routing.resolve_property("https://www.example.net/a", [PREFIX]) == PREFIX


# --------------------------------------------------------- scheme awareness
#
# There was zero http:// coverage here before these. A host registered under
# BOTH schemes is two separate Search Console properties holding separate
# data, and host matching alone cannot tell them apart.

HTTP_PREFIX = "http://example.com/"
HTTPS_PREFIX = "https://example.com/"


@pytest.mark.parametrize("properties", [
    [HTTP_PREFIX, HTTPS_PREFIX],
    [HTTPS_PREFIX, HTTP_PREFIX],
])
def test_https_url_picks_the_https_property_whatever_the_list_order(properties):
    """store.get_sites() sorts by property string, so "http://" comes first
    and an https URL used to resolve to the http property every time.
    Reversing the list flipped the answer — the tell that the old result was
    an accident of ordering."""
    assert routing.resolve_property(
        "https://example.com/page", properties) == HTTPS_PREFIX


@pytest.mark.parametrize("properties", [
    [HTTP_PREFIX, HTTPS_PREFIX],
    [HTTPS_PREFIX, HTTP_PREFIX],
])
def test_http_url_picks_the_http_property_whatever_the_list_order(properties):
    assert routing.resolve_property(
        "http://example.com/page", properties) == HTTP_PREFIX


def test_scheme_also_breaks_the_www_toggle_step():
    properties = ["http://www.example.com/", "https://www.example.com/"]
    assert routing.resolve_property(
        "https://example.com/page", properties) == "https://www.example.com/"


def test_scheme_breaks_ties_at_the_suffix_step_too():
    properties = ["http://example.com/", "https://example.com/"]
    assert routing.resolve_property(
        "https://deep.example.com/page", properties) == HTTPS_PREFIX


def test_a_longer_host_still_beats_a_scheme_match():
    """Specificity stays PRIMARY; scheme only separates equally specific
    candidates. Weakening that would send a shop.example.com URL to the
    bare-domain property just because the schemes lined up."""
    properties = ["https://example.com/", "http://shop.example.com/"]
    assert routing.resolve_property(
        "https://a.shop.example.com/x", properties) == "http://shop.example.com/"


def test_a_lone_http_property_still_matches_an_https_url():
    """No scheme match available means fall back to what was there before —
    matching on host alone. Refusing to route would be a regression."""
    assert routing.resolve_property("https://example.com/page",
                                    [HTTP_PREFIX]) == HTTP_PREFIX


def test_a_scheme_less_url_falls_back_to_list_order():
    """"" is not http: a bare host said nothing about a scheme, so it must
    not be treated as preferring the http property."""
    assert routing.resolve_property(
        "example.com/page", [HTTP_PREFIX, HTTPS_PREFIX]) == HTTP_PREFIX
    assert routing.resolve_property(
        "example.com/page", [HTTPS_PREFIX, HTTP_PREFIX]) == HTTPS_PREFIX


def test_sc_domain_property_covers_both_schemes():
    """An sc-domain: property has no scheme; step 3 is untouched."""
    assert routing.resolve_property("http://example.com/a", [DOMAIN]) == DOMAIN
    assert routing.resolve_property("https://example.com/a", [DOMAIN]) == DOMAIN


def test_scheme_of_reports_empty_for_a_bare_host():
    assert routing.scheme_of("https://example.com/a") == "https"
    assert routing.scheme_of("HTTP://example.com/a") == "http"
    assert routing.scheme_of("example.com/a") == ""


def test_route_all_is_scheme_aware_too():
    properties = [HTTP_PREFIX, HTTPS_PREFIX]
    assert routing.route_all(
        ["https://example.com/a", "http://example.com/b"], properties) == [
        ("https://example.com/a", HTTPS_PREFIX),
        ("http://example.com/b", HTTP_PREFIX),
    ]


# ------------------------------------------------------------ path awareness
#
# A URL-prefix property carries a PATH as well as a host:
# "https://example.com/blog/" is a different property from
# "https://example.com/", holds different data, and covers only the pages
# beneath it. Matching on host alone made those two indistinguishable, so
# every URL on the host resolved to whichever appeared first in the list.
#
# That is worse than a coin flip. Because attribution is stable, every URL
# gets WRITTEN under the winner, and a later
# gsc_find_unindexed(site="https://example.com/blog/", source="store")
# reads back nothing -- which a calling model reads as "no indexing
# problems here" rather than "the data is filed elsewhere".

ROOT = "https://example.com/"
BLOG = "https://example.com/blog/"
DEEP = "https://example.com/blog/2026/"


def test_path_of_normalises_to_leading_and_trailing_slashes():
    assert routing.path_of("https://example.com/blog/post") == "/blog/post/"
    assert routing.path_of("https://example.com/blog/") == "/blog/"
    assert routing.path_of("https://example.com") == "/"
    assert routing.path_of("example.com/a") == "/a/"


def test_path_of_ignores_the_query_and_fragment():
    # ?page=2 is not part of what a property covers. Folding it into the
    # path would make the same page resolve differently with and without
    # its query string.
    assert routing.path_of("https://example.com/blog/?page=2#top") == "/blog/"


@pytest.mark.parametrize("properties", [[ROOT, BLOG], [BLOG, ROOT]])
def test_the_sub_path_property_wins_whatever_the_list_order(properties):
    """The headline case from issue #11."""
    assert routing.resolve_property(
        "https://example.com/blog/post", properties) == BLOG


@pytest.mark.parametrize("properties", [[ROOT, BLOG], [BLOG, ROOT]])
def test_a_url_outside_the_sub_path_still_goes_to_the_root(properties):
    assert routing.resolve_property(
        "https://example.com/shop/item", properties) == ROOT


def test_the_deepest_covering_path_wins():
    properties = [ROOT, BLOG, DEEP]
    assert routing.resolve_property(
        "https://example.com/blog/2026/post", properties) == DEEP


def test_a_path_property_does_not_claim_a_lookalike_sibling():
    """/blog/ must not swallow /blogger/ the way a bare startswith would.

    The same class of bug as notexample.com matching example.com: string
    prefixes are not path prefixes unless the boundary is enforced.
    """
    assert routing.resolve_property(
        "https://example.com/blogger/post", [BLOG]) is None


def test_the_property_path_itself_resolves():
    # The property's own landing page, with and without the trailing slash.
    assert routing.resolve_property("https://example.com/blog", [BLOG]) == BLOG
    assert routing.resolve_property("https://example.com/blog/", [BLOG]) == BLOG


def test_an_uncovered_url_returns_none_rather_than_guessing():
    """None means "no property covers this", which callers already handle.

    Returning the blog property for a /shop/ URL would be a confident wrong
    answer -- the failure mode this whole module exists to avoid.
    """
    assert routing.resolve_property(
        "https://example.com/shop/item", [BLOG]) is None


def test_a_domain_property_takes_over_where_no_path_property_covers():
    """Falling through to step 3 is the RIGHT outcome, not a miss.

    sc-domain: has no path and covers the whole host, so it still owns the
    URLs the sub-path property does not.
    """
    properties = [BLOG, "sc-domain:example.com"]
    assert routing.resolve_property(
        "https://example.com/shop/item", properties) == "sc-domain:example.com"
    assert routing.resolve_property(
        "https://example.com/blog/post", properties) == BLOG


def test_the_www_toggle_step_is_path_aware_too():
    properties = ["https://www.example.com/", "https://www.example.com/blog/"]
    assert routing.resolve_property(
        "https://example.com/blog/post", properties) == "https://www.example.com/blog/"


def test_the_suffix_step_is_path_aware_too():
    properties = ["https://example.com/", "https://example.com/blog/"]
    assert routing.resolve_property(
        "https://deep.example.com/blog/post", properties) == "https://example.com/blog/"


def test_a_longer_host_still_beats_a_deeper_path():
    """Host specificity stays PRIMARY at the suffix step.

    Path depth is a tie-break WITHIN a host, not across hosts: a property
    for the actual subdomain owns the URL even when a broader property has
    a deeper path that also covers it.
    """
    properties = ["https://example.org/x/", "https://b.example.org/"]
    assert routing.resolve_property(
        "https://a.b.example.org/x/y", properties) == "https://b.example.org/"


def test_scheme_still_breaks_ties_between_equal_paths():
    properties = ["http://example.com/blog/", "https://example.com/blog/"]
    assert routing.resolve_property(
        "https://example.com/blog/post", properties) == "https://example.com/blog/"


def test_paths_are_compared_case_sensitively():
    """Hosts are case-insensitive; paths are not.

    A server may serve /Blog/ and /blog/ as different pages, and Search
    Console treats the two as different properties. Lowercasing the path
    the way host_of() lowercases the host would merge them.
    """
    assert routing.resolve_property("https://example.com/Blog/post", [BLOG]) is None


def test_route_all_is_path_aware_too():
    properties = [ROOT, BLOG]
    assert routing.route_all(
        ["https://example.com/blog/a", "https://example.com/shop/b"],
        properties) == [
        ("https://example.com/blog/a", BLOG),
        ("https://example.com/shop/b", ROOT),
    ]


def test_a_property_stored_without_its_trailing_slash_still_has_a_boundary():
    """The case that proves path_of's normalisation is load-bearing.

    test_a_path_property_does_not_claim_a_lookalike_sibling above passes
    even with normalisation removed, because "https://example.com/blog/"
    carries its own trailing slash -- it tests the literal, not the code.
    Search Console hands back a trailing slash today, but nothing here
    validates that, and one property string missing it would otherwise let
    /blog swallow every /blogger and /blogroll URL on the host.
    """
    stored = "https://example.com/blog"
    assert routing.resolve_property(
        "https://example.com/blogger/post", [stored]) is None
    assert routing.resolve_property(
        "https://example.com/blog/post", [stored]) == stored
