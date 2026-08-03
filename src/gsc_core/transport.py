"""One sentence for a failed HTTP request, carrying no third-party text.

Both API clients — api.inspect_url and perf.post_query — end their retry
loop the same way, and both used to end it by interpolating the exception:
`f"request failed: {exc}"`. That message is composed by requests and
urllib3, not by us, and it leaves the process: into the note field of a
status row, into a PerfError that surfaces through gsc_performance, and
from there into a calling model's transcript and whatever logs the client
keeps.

What can be in it:

- A proxy URL with embedded credentials. requests reads HTTPS_PROXY from
  the environment, and a ProxyError names the proxy it tried;
  https://user:password@proxy.example.com:8080 is an ordinary way to write
  that variable in a corporate environment.
- The caller's own domain — perf.post_query encodes the property into the
  request path, so a connection error's message carries it.
- Internal hostnames and IP addresses from DNS and connection failures,
  which describe the user's network rather than anything they asked about.

None of that is a remote-attacker vulnerability. It is the same class of
defect as the email-address rule this codebase already follows: unbounded
third-party text crossing a boundary meant to carry only what we chose to
put there.

The exception's TYPE NAME is kept, deliberately. It is a class name from
requests — a closed vocabulary that cannot contain user data — and it is
the one thing that distinguishes a proxy misconfiguration from a timeout
from a TLS failure. Replacing the message with a bare constant would make
every network failure look identical to whoever has to debug one.

This lives in its own module rather than in either client because both
need it and neither imports the other. Copying it into both is how
gsc_mcp._api_fix ended up with two versions that disagreed.
"""
from __future__ import annotations

PREFIX = "request failed"
REASON = "the Search Console API could not be reached"


def transport_failure(exc: BaseException) -> str:
    """The message to report for a requests transport exception."""
    return f"{PREFIX}: {REASON} ({type(exc).__name__})"
