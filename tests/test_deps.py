import pytest

from gsc_mcp import deps


def test_oauth_client_prefers_environment_variables(monkeypatch):
    monkeypatch.setenv(deps.CLIENT_ID_ENV, "env-id")
    monkeypatch.setenv(deps.CLIENT_SECRET_ENV, "env-secret")
    assert deps.oauth_client() == ("env-id", "env-secret")


def test_oauth_client_raises_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv(deps.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(deps.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_ID", "")
    monkeypatch.setattr(deps, "EMBEDDED_CLIENT_SECRET", "")
    with pytest.raises(deps.NotConfigured):
        deps.oauth_client()


def test_no_real_client_secret_is_committed():
    """A public repo must never carry a live OAuth secret."""
    assert deps.EMBEDDED_CLIENT_SECRET == ""


def test_connection_is_a_fresh_object_each_call(tmp_path, monkeypatch):
    monkeypatch.setenv("GSC_MCP_HOME", str(tmp_path))
    with deps.connection() as first, deps.connection() as second:
        assert first is not second
