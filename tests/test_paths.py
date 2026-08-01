import sys
from pathlib import Path

from gsc_core import paths


def test_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path / "custom"))
    assert paths.config_dir() == tmp_path / "custom"


def test_windows_uses_appdata(monkeypatch):
    monkeypatch.delenv(paths.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    assert paths.config_dir() == Path(r"C:\Users\test\AppData\Roaming") / "gsc-mcp"


def test_macos_uses_application_support(monkeypatch):
    monkeypatch.delenv(paths.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/test")))
    expected = Path("/Users/test/Library/Application Support/gsc-mcp")
    assert paths.config_dir() == expected


def test_linux_respects_xdg(monkeypatch):
    monkeypatch.delenv(paths.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/test/.config")
    assert paths.config_dir() == Path("/home/test/.config/gsc-mcp")


def test_ensure_creates_directory(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path / "made"))
    created = paths.ensure_config_dir()
    assert created.is_dir()


def test_derived_paths_sit_under_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.ENV_OVERRIDE, str(tmp_path))
    assert paths.db_path() == tmp_path / "state.db"
    assert paths.token_path() == tmp_path / "token.json"
    assert paths.config_path() == tmp_path / "config.json"
    assert paths.log_dir() == tmp_path / "logs"
