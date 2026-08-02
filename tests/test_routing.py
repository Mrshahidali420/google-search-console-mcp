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
