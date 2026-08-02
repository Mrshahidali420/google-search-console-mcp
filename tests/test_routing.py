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


def test_route_all_preserves_input_order_and_flags_misses():
    urls = ["https://example.com/a", "https://elsewhere.test/b"]
    assert routing.route_all(urls, [DOMAIN]) == [
        ("https://example.com/a", DOMAIN),
        ("https://elsewhere.test/b", None),
    ]


def test_empty_property_list_matches_nothing():
    assert routing.resolve_property("https://example.com/a", []) is None
