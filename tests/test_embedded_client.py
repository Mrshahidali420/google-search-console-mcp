"""The OAuth client baked into a release build, and the wall around it.

Decision D1: gsc-mcp ships its own Google OAuth client so a user never has
to create one in the Cloud Console. That value cannot live in tracked
source -- this repo is public, and a secret in git history is permanent
even after it is deleted, which costs the cheap rotation that makes an
installed-app secret tolerable in the first place.

So the client arrives at build time, in a gitignored gsc_mcp/_embedded.py
that CI writes from repository secrets. Two things therefore need proving,
and they pull in opposite directions:

  1. A SOURCE CHECKOUT, where that file does not exist, must still import
     and run -- falling back to the environment variables. This is the
     common case: every developer, every CI run, every test in this suite.
  2. A RELEASE BUILD, where it does exist, must actually pick it up.

The fallback is the dangerous one. If it were written as a bare `from
gsc_mcp._embedded import ...`, every source checkout would die at import
with an ImportError, and the tests would never see it because CI is itself
a source checkout -- it would break only for users, only in the case the
file is missing, which is the case nobody builds for.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import gsc_mcp
from gsc_mcp import deps

REPO_ROOT = Path(__file__).resolve().parents[1]
EMBEDDED_MODULE = "gsc_mcp._embedded"

# Assembled rather than written literally so this file does not itself
# contain the marker the scan below looks for. Google prefixes every
# installed-app client secret with it.
SECRET_PREFIX = "GOCSPX" + "-"


# --------------------------------------------------------------- the loader

@pytest.fixture
def embedded(monkeypatch):
    """Install a fake gsc_mcp._embedded, or block the import entirely.

    A None entry in sys.modules makes the import raise ImportError, which
    is how the absent-file case is simulated without deleting anything on
    disk -- necessary because on a release build, or on the author's own
    machine, the real file IS present.
    Both the package ATTRIBUTE and the sys.modules entry have to move
    together. `from gsc_mcp import _embedded` reaches the attribute on the
    already-imported package first and only falls through to an import
    when it is absent, so patching sys.modules alone silently does
    nothing on any machine where the real file exists — which is exactly
    the machine these tests need to be trustworthy on.
    """
    def _install(**attrs):
        if attrs.pop("missing", False):
            monkeypatch.delattr(gsc_mcp, "_embedded", raising=False)
            monkeypatch.setitem(sys.modules, EMBEDDED_MODULE, None)
            return
        fake = types.ModuleType(EMBEDDED_MODULE)
        for name, value in attrs.items():
            setattr(fake, name, value)
        monkeypatch.setattr(gsc_mcp, "_embedded", fake, raising=False)
        monkeypatch.setitem(sys.modules, EMBEDDED_MODULE, fake)
    return _install


def test_a_source_checkout_without_the_file_falls_back_to_empty(embedded):
    # The load-bearing case. Not "returns empty" for its own sake: empty is
    # what lets oauth_client() fall through to the environment variables
    # and, failing those, raise NotConfigured with an actionable message.
    embedded(missing=True)
    assert deps._embedded_client() == ("", "")


def test_a_release_build_with_the_file_uses_it(embedded):
    embedded(CLIENT_ID="built-in-id", CLIENT_SECRET="built-in-secret")
    assert deps._embedded_client() == ("built-in-id", "built-in-secret")


def test_a_half_written_file_degrades_instead_of_crashing(embedded):
    # A CI step whose secret was unset writes a file with a blank or absent
    # value. Reading the attributes directly would raise AttributeError at
    # IMPORT time, taking the whole server down before any tool can report
    # why. Degrading to empty routes it to the same NotConfigured message a
    # missing file produces, which names the fix.
    embedded(CLIENT_ID="built-in-id")
    assert deps._embedded_client() == ("built-in-id", "")


def test_a_half_written_file_leaves_oauth_client_refusing_to_start(
        embedded, monkeypatch):
    monkeypatch.delenv(deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "built-in-id")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "")
    with pytest.raises(deps.NotConfigured):
        deps.oauth_client()


def test_the_environment_still_beats_an_embedded_client(monkeypatch):
    # Restated here, not only in test_deps.py, because THIS is the file
    # that makes the embedded side non-empty in the field. A release build
    # must remain overridable by a self-hoster with their own Cloud client.
    monkeypatch.setenv(deps.CLIENT_ID_ENV, "env-id")
    monkeypatch.setenv(deps.CLIENT_SECRET_ENV, "env-secret")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "built-in-id")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "built-in-secret")
    assert deps.oauth_client() == ("env-id", "env-secret")


# ------------------------------------------------------------- the wall

def test_the_embedded_module_is_gitignored():
    # This one line is the entire wall between a live secret and a public
    # repository. Everything else in this file assumes it holds.
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "src/gsc_mcp/_embedded.py" in ignored


def _scannable_files():
    """Tracked text files that must never contain a live secret.

    _embedded.py is excluded: holding the secret is its whole job, and it
    is gitignored (asserted above). Everything else is fair game.
    """
    roots = [REPO_ROOT / "src", REPO_ROOT / "tests", REPO_ROOT / "docs",
             REPO_ROOT / ".github"]
    suffixes = {".py", ".md", ".yml", ".yaml", ".toml", ".json", ".js"}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in suffixes or not path.is_file():
                continue
            if path.name == "_embedded.py" or "__pycache__" in path.parts:
                continue
            yield path


def test_no_live_client_secret_appears_in_tracked_source():
    """A public repo must never carry a live OAuth secret.

    Scanning the files, rather than asserting a constant is empty, is what
    keeps this honest now that a build may legitimately populate that
    constant. It also catches a secret pasted anywhere else -- a doc
    example, a test fixture, a workflow file -- which the constant check
    never could.
    """
    offenders = [str(path.relative_to(REPO_ROOT))
                 for path in _scannable_files()
                 if SECRET_PREFIX in path.read_text(
                     encoding="utf-8", errors="ignore")]
    assert offenders == []


def test_the_scan_actually_reaches_the_source_tree():
    # Without this, a bad path or a wrong suffix set would make the scan
    # above pass by inspecting nothing at all -- the classic way a
    # security test becomes decorative. deps.py is the file most likely to
    # hold a secret, so require the scan to have seen it.
    scanned = {path.name for path in _scannable_files()}
    assert "deps.py" in scanned
    assert len(scanned) > 20
