import pytest

from gsc_core import store
from gsc_mcp import server


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    return tmp_path


def test_server_import_writes_nothing_to_stdout(capsys):
    """stdout is the MCP JSON-RPC transport and must stay clean."""
    import importlib
    importlib.reload(server)
    assert capsys.readouterr().out == ""


def test_list_sites_persists_properties_to_the_store(home, monkeypatch):
    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [
        {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"},
        {"siteUrl": "https://www.example.net/", "permissionLevel": "siteFullUser"},
    ])
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    out = server.gsc_list_sites()
    assert len(out) == 2
    with store.session() as conn:
        assert len(store.get_sites(conn)) == 2


def test_list_sites_strips_the_sc_domain_prefix_from_the_host(home, monkeypatch):
    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [
        {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}])
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    assert server.gsc_list_sites()[0]["host"] == "example.com"


def test_list_sites_reports_auth_required_as_data_not_an_exception(home, monkeypatch):
    from gsc_core import gauth

    def boom(*a, **k):
        raise gauth.AuthRequired("no token")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_list_sites()
    assert out["ok"] is False
    assert out["error"] == "auth_required"
    assert out["fix"]


def test_doctor_reports_every_check(home, monkeypatch):
    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [])
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    out = server.gsc_doctor()
    names = [c["name"] for c in out["checks"]]
    assert names == ["oauth_client", "token", "config", "store", "properties"]


def test_doctor_gives_a_fix_string_for_every_failing_check(home, monkeypatch):
    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [])
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    out = server.gsc_doctor()
    for check in out["checks"]:
        if not check["ok"]:
            assert check["fix"], f"{check['name']} failed without a fix string"


def test_doctor_never_leaks_an_exception_message(home, monkeypatch):
    # The canary is the specific leaked value, not the bare word "SECRET":
    # the oauth_client check's own mandated fix string legitimately names
    # the GSC_MCP_CLIENT_SECRET environment variable and fails in any test
    # environment (no real OAuth client is configured), so a bare "SECRET"
    # substring check collides with that always-present, non-leaked text.
    def boom(*a, **k):
        raise RuntimeError("token=ya29.SECRET")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_doctor()
    assert "ya29.SECRET" not in repr(out)


def test_doctor_continues_after_a_failing_check(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_doctor()
    assert len(out["checks"]) == 5
