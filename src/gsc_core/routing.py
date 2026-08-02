"""Map a URL to the Search Console property that covers it.

The public product has no config.json sites dict — properties come from the
Search Console API and live in store.sites as a plain list of property
strings (either "sc-domain:<host>" or a URL-prefix like
"https://www.example.com/"). This module takes that list directly so
routing never touches config or the database; it is pure string logic.

Match order, most to least specific:

0. identity — url IS one of properties, verbatim (case-insensitive). This
   is the only way an sc-domain: property can ever resolve against
   itself: "sc-domain:example.com" is not a URL, so host_of() reads the
   colon as a port separator and produces the nonsense host "sc-domain",
   failing every step below. A URL-prefix property mostly survives that
   round trip by accident (its host_of() output usually still matches
   itself), which is what made the gap easy to miss before this step
   existed. Purely additive: it only ever short-circuits an exact match,
   so it cannot change the outcome for a real page URL passed as `url`.
1. exact host match — a URL-prefix property whose host equals the URL's host
2. www/non-www toggle — the same, ignoring a leading "www." on either side
3. sc-domain: property covering the host or any subdomain of it
4. bare host suffix — a URL-prefix property whose host is a suffix of the URL's

A host can be covered by more than one property at steps 3 and 4 (e.g. both
"sc-domain:example.com" and "sc-domain:shop.example.com" cover
"cart.shop.example.com"). Search Console itself resolves that ambiguity by
preferring the most specific (longest) match, so this does the same rather
than depending on list order — the caller's property list has no ordering
guarantee the way the original tool's config.json dict insertion order did.

SCHEME is the tie-break of last resort at steps 1, 2 and 4. A host can be
registered twice — "http://example.com/" AND "https://example.com/" are
separate Search Console properties, and the two hold separate data. Host
matching alone cannot tell them apart, so an https URL used to resolve to
whichever came first in the list; store.get_sites() orders by property
string, which sorts "http://" before "https://", making that the http one
every time. Reversing the input flipped the answer, which is the tell that
the old result was an accident of ordering rather than a decision.

Specificity stays PRIMARY and scheme only separates properties that are
otherwise equally good, so "most specific wins" is unchanged: a longer
host still beats a scheme match at step 4. An sc-domain: property has no
scheme and covers both, so step 3 is untouched. A `url` with no scheme at
all matches no property's scheme, and falls back to list order exactly as
before.
"""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse


def host_of(url: str) -> str:
    """Lowercased host, no userinfo, no port. Accepts bare hosts without a scheme."""
    candidate = url if "://" in url else f"http://{url}"
    netloc = urlparse(candidate).netloc.lower()
    netloc = netloc.split("@")[-1]  # drop userinfo
    return netloc.split(":")[0]  # drop port


def scheme_of(url: str) -> str:
    """Lowercased scheme, or "" for a bare host that carries none.

    Deliberately NOT defaulted to "http" the way host_of() defaults its
    parse target: "" means "this string said nothing about a scheme", and
    that has to stay distinguishable from an explicit http:// so a
    scheme-less input falls back to list order rather than being matched
    against http properties in preference to https ones.
    """
    return urlparse(url).scheme.lower() if "://" in url else ""


def _is_covered(host: str, domain: str) -> bool:
    """True if host is domain itself or any subdomain of it.

    Must not be a bare endswith(domain) — that would let "notexample.com"
    match "example.com" because the strings happen to share a suffix.
    """
    return host == domain or host.endswith(f".{domain}")


def _without_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def resolve_property(url: str, properties: list[str]) -> str | None:
    """Return the property string covering url, or None if none does.

    See the module docstring for the full match order — step 0 (identity)
    runs here first, before host_of() ever sees `url`.
    """
    for prop in properties:
        if prop.lower() == url.lower():
            return prop

    host = host_of(url)
    host_bare = _without_www(host)
    scheme = scheme_of(url)

    def same_scheme(prop: str) -> bool:
        return bool(scheme) and scheme_of(prop) == scheme

    def best(candidates: list[str], specificity: Callable[[str], int]) -> str:
        """The winner among equally-eligible properties for one step.

        `specificity` stays PRIMARY and scheme is only a tie-break within
        it, which is what keeps this additive: max() returns the first
        maximal element, so with one candidate, or with no candidate
        sharing the URL's scheme, the answer is exactly what returning the
        first match in list order used to give. It differs only where two
        properties were equally good and the old code picked by list order
        — the case where https://example.com/ resolved to an
        http://example.com/ property purely because get_sites() sorts
        "http" before "https".
        """
        return max(candidates, key=lambda prop: (specificity(prop),
                                                 same_scheme(prop)))

    prefix_hosts = {prop: host_of(prop) for prop in properties
                    if not prop.startswith("sc-domain:")}
    # The domain half of an sc-domain: property is API-supplied and its case
    # is not guaranteed — lower() it here, once, so every comparison below
    # is case-insensitive without each call site having to remember to do it.
    domain_hosts = {prop: prop.removeprefix("sc-domain:").lower()
                    for prop in properties if prop.startswith("sc-domain:")}

    # Step 1: exact host match. Every candidate here is equally specific
    # (the host matched exactly), so scheme is the only tie-break there is.
    exact = [prop for prop, prop_host in prefix_hosts.items()
             if prop_host == host]
    if exact:
        return best(exact, lambda prop: 0)

    # Step 2: www/non-www toggle. Equally specific for the same reason.
    toggled = [prop for prop, prop_host in prefix_hosts.items()
               if _without_www(prop_host) == host_bare]
    if toggled:
        return best(toggled, lambda prop: 0)

    # Step 3: sc-domain: property covering the host or any subdomain of it.
    domain_matches = [prop for prop, domain in domain_hosts.items()
                       if _is_covered(host, domain)]
    if domain_matches:
        return max(domain_matches, key=lambda prop: len(domain_hosts[prop]))

    # Step 4: bare host suffix — the loosest match, so it runs last.
    suffix_matches = [prop for prop, prop_host in prefix_hosts.items()
                       if _is_covered(host, prop_host)]
    if suffix_matches:
        return best(suffix_matches, lambda prop: len(prefix_hosts[prop]))

    return None


def route_all(urls: list[str], properties: list[str]) -> list[tuple[str, str | None]]:
    """Resolve each url against properties, preserving input order."""
    return [(url, resolve_property(url, properties)) for url in urls]
