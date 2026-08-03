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


def test_list_sites_reports_not_configured_as_data_not_an_exception(home, monkeypatch):
    """deps.provider() raises NotConfigured whenever no OAuth client is
    configured — which is every install today, since the embedded
    constants are deliberately empty (D1). This must answer, not raise."""
    monkeypatch.delenv(server.deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(server.deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_ID", "")
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_SECRET", "")

    out = server.gsc_list_sites()
    assert out["ok"] is False
    assert out["error"] == "not_configured"
    assert out["fix"]


def test_list_sites_preserves_existing_sitemaps_on_refresh(home, monkeypatch):
    """gsc_submit_sitemaps (Task 9) records submitted sitemaps on a site
    row so a later bare gsc_list_sites refresh can reuse them; a refresh
    that always writes [] would silently erase that on every call."""
    with store.session() as conn:
        store.upsert_site(conn, "sc-domain:example.com", "example.com",
                          "siteOwner", ["https://example.com/sitemap.xml"])

    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [
        {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}])
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    server.gsc_list_sites()

    with store.session() as conn:
        sites = store.get_sites(conn)
    assert sites[0]["sitemaps"] == ["https://example.com/sitemap.xml"]


def test_doctor_reports_every_check(home, monkeypatch):
    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [])
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    out = server.gsc_doctor()
    names = [c["name"] for c in out["checks"]]
    assert names == ["oauth_client", "token", "config", "store", "properties",
                     "browser", "extension"]


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


def test_doctor_check_detail_is_exactly_the_exception_type_name(home, monkeypatch):
    """A stronger form of the leak test above: pins detail to being EQUAL
    to the type name, not merely free of one planted literal — this
    catches any message leak, not just the specific one a test happens to
    plant."""
    def boom(*a, **k):
        raise RuntimeError("token=ya29.abc123 refresh_token=1//0gXYZ")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_doctor()
    properties_check = next(c for c in out["checks"] if c["name"] == "properties")
    assert properties_check["ok"] is False
    assert properties_check["detail"] == "RuntimeError"


def test_doctor_properties_check_blames_the_oauth_client_when_not_configured(
    home, monkeypatch
):
    """With no OAuth client configured (the default state on every
    install today), the properties check must not tell the user to check
    their Search Console properties/scope — that's the wrong cause."""
    monkeypatch.delenv(server.deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(server.deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_ID", "")
    monkeypatch.setattr(server.deps, "EMBEDDED_CLIENT_SECRET", "")

    out = server.gsc_doctor()
    properties_check = next(c for c in out["checks"] if c["name"] == "properties")
    assert properties_check["detail"] == "NotConfigured"
    assert properties_check["fix"] == server._FIX_OAUTH_CLIENT


def test_doctor_continues_after_a_failing_check(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(server.deps, "provider", boom)
    out = server.gsc_doctor()
    assert len(out["checks"]) == 7


# --- failures the tool does not model still keep the contract (A1 + B3) ------

def test_list_sites_reports_a_refused_call_as_data_not_an_empty_list(home,
                                                                    monkeypatch):
    """A1's real damage: list_properties used to swallow a non-200 and return
    [], which reads as 'this account owns no properties'. It must now surface
    as a structured failure, and must not have written that emptiness to the
    store either."""
    from gsc_core import api

    def boom(*a, **k):
        raise api.ApiError("sites.list returned HTTP 403", status=403)

    monkeypatch.setattr(server.api, "list_properties", boom)
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    out = server.gsc_list_sites()
    assert out["ok"] is False
    assert out["error"] == "api_error"
    assert out["status"] == 403
    assert "scope" in out["fix"]
    with store.session() as conn:
        assert store.get_sites(conn) == []


def test_a_403_and_a_500_get_different_fixes(home, monkeypatch):
    """403 is a scope or permission problem the user can act on; a 500 is
    not. One shared fix string would send them to the wrong place."""
    from gsc_core import api

    monkeypatch.setattr(server.deps, "provider", lambda: object())
    fixes = {}
    for status in (403, 500):
        monkeypatch.setattr(
            server.api, "list_properties",
            lambda *a, s=status, **k: (_ for _ in ()).throw(
                api.ApiError(f"HTTP {s}", status=s)))
        fixes[status] = server.gsc_list_sites()["fix"]
    assert fixes[403] != fixes[500]


def test_list_sites_reports_an_unexpected_failure_as_data(home, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("token=ya29.LEAK")

    monkeypatch.setattr(server.api, "list_properties", boom)
    monkeypatch.setattr(server.deps, "provider", lambda: object())
    out = server.gsc_list_sites()
    assert out["ok"] is False
    assert out["error"] == "unexpected"
    assert out["detail"] == "RuntimeError"
    assert out["fix"]
    assert "ya29.LEAK" not in repr(out)


def test_a_database_failure_after_a_good_fetch_is_still_reported(home,
                                                                monkeypatch):
    """The store write lives inside the try too. It did not always: a failure
    there escaped as an exception while the network part was contract-safe."""
    monkeypatch.setattr(server.api, "list_properties", lambda *a, **k: [
        {"siteUrl": "sc-domain:example.com", "permissionLevel": "siteOwner"}])
    monkeypatch.setattr(server.deps, "provider", lambda: object())

    def no_write(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(server.store, "upsert_site", no_write)
    out = server.gsc_list_sites()
    assert out["error"] == "unexpected"
