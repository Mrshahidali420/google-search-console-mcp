"""The vendored browser extension ships as package data.

The scrub tests here are a permanent guard, not a one-off check. The
extension came out of a private toolkit that sits beside real client
config; anything a future contributor copies back in gets screened by
these before it can reach a public release.
"""

import json
import re
from importlib.resources import files

EXPECTED = {"manifest.json", "background.js", "content.js", "rpc-main.js",
            "connect.html", "connect.js", "options.html", "options.js",
            "popup.html", "popup.js"}


def _extension_files():
    root = files("gsc_mcp") / "extension"
    return {p.name: p for p in root.iterdir() if p.is_file()}


def test_every_extension_file_ships_in_the_package():
    assert EXPECTED <= set(_extension_files())


def test_the_manifest_is_valid_mv3():
    manifest = json.loads((files("gsc_mcp") / "extension" /
                           "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["version"]
    assert "storage" in manifest["permissions"]


def test_no_extension_file_contains_client_data():
    """Backstop for the manual scrub. A real domain or email in a public
    repo is not a bug we get to fix later -- it is already published."""
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    windows_path = re.compile(r"[A-Za-z]:\\\\?Users", re.IGNORECASE)
    allowed_hosts = {"example.com", "example.net", "example.org",
                     "search.google.com", "www.google.com", "127.0.0.1",
                     "localhost"}
    host = re.compile(r"https?://([\w.-]+)")
    for name, path in _extension_files().items():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not email.search(text), f"{name} contains an email address"
        assert not windows_path.search(text), f"{name} contains a local path"
        for found in host.findall(text):
            assert found in allowed_hosts, f"{name} references {found}"


def test_no_extension_file_contains_a_credential_or_install_id():
    """Second half of the scrub: identifiers that are not URLs.

    A Chromium extension ID is 32 chars of [a-p] and names one person's
    install. An OAuth client id ends in .apps.googleusercontent.com. Both
    are individually identifying and neither belongs in a public wheel.
    """
    extension_id = re.compile(r"\b[a-p]{32}\b")
    oauth_client = re.compile(r"[\w-]+\.apps\.googleusercontent\.com")
    secretish = re.compile(
        r"""(?ix)
        \b(?:api[_-]?key|client[_-]?secret|access[_-]?token|
             refresh[_-]?token|bridge[_-]?token|bearer)\b
        \s*[:=]\s*['"][^'"]{8,}['"]
        """)
    for name, path in _extension_files().items():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not extension_id.search(text), f"{name} contains an extension ID"
        assert not oauth_client.search(text), f"{name} contains an OAuth client ID"
        assert not secretish.search(text), f"{name} hardcodes a credential"


def test_the_manifest_version_matches_the_background_script():
    """Two hand-maintained copies of one version string drift silently,
    and the handshake checks the one in background.js."""
    root = files("gsc_mcp") / "extension"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    background = (root / "background.js").read_text(encoding="utf-8")
    match = re.search(r'VERSION\s*=\s*["\']([\d.]+)["\']', background)
    assert match, "background.js declares no VERSION"
    assert match.group(1) == manifest["version"]
