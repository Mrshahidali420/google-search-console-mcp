"""Layer 1 browser detection.

Every test here is fixture-driven: no test reads the real registry, the
real /Applications, or the real PATH. The suite has to pass identically on
a CI box with no browser installed and on a workstation with six.
"""
from __future__ import annotations

import sys

import pytest

from gsc_core import browsers


# --------------------------------------------------------------------------
# The brand table
# --------------------------------------------------------------------------

def test_every_brand_carries_the_fields_detection_needs():
    for key, brand in browsers.BRANDS.items():
        assert brand.key == key
        assert brand.label
        assert brand.exe_name
        assert brand.extensions_url.endswith("://extensions")
        assert brand.win_vendor and len(brand.win_vendor) == 2
        assert brand.mac_bundle_id
        assert brand.linux_binaries


def test_the_six_expected_brands_are_present():
    assert set(browsers.BRANDS) == {
        "chrome", "brave", "edge", "vivaldi", "opera", "chromium"}


def test_each_brand_has_its_own_extensions_page():
    """The bug this prevents: opening chrome://extensions in Brave.

    Chromium is the one deliberate alias: it registers no ``chromium://``
    scheme, so ``chrome://extensions`` is its real page and inventing a
    private scheme would ship a URL that simply fails to open. Every other
    brand must be distinct.
    """
    aliased = {"chrome", "chromium"}
    assert browsers.BRANDS["chromium"].extensions_url == "chrome://extensions"

    distinct = {k: b.extensions_url for k, b in browsers.BRANDS.items()
                if k not in aliased}
    assert len(set(distinct.values())) == len(distinct)
    assert "chrome://extensions" not in set(distinct.values())

    assert browsers.BRANDS["brave"].extensions_url == "brave://extensions"
    assert browsers.BRANDS["edge"].extensions_url == "edge://extensions"


def test_no_brand_borrows_another_brands_scheme():
    """Every scheme must be one this brand actually answers to."""
    for key, brand in browsers.BRANDS.items():
        scheme = brand.extensions_url.split("://")[0]
        expected = "chrome" if key in {"chrome", "chromium"} else key
        assert scheme == expected


def test_brands_are_frozen_so_a_caller_cannot_rewrite_a_scheme():
    with pytest.raises(Exception):
        browsers.BRANDS["chrome"].extensions_url = "brave://extensions"


# --------------------------------------------------------------------------
# detect() contract
# --------------------------------------------------------------------------

def test_detect_returns_nothing_rather_than_raising_on_a_bare_machine(
        monkeypatch, tmp_path):
    """A machine with no Chromium browser is a supported state — the
    recommendation becomes 'install one', not a traceback."""
    monkeypatch.setattr(browsers, "_detect_windows", lambda: [])
    monkeypatch.setattr(browsers, "_detect_macos", lambda: [])
    monkeypatch.setattr(browsers, "_detect_linux", lambda: [])
    assert browsers.detect() == []


def test_detect_swallows_a_detector_explosion(monkeypatch, caplog):
    def boom():
        raise OSError("C:/Users/somebody/AppData/Local/Whatever")

    monkeypatch.setattr(browsers, "_detect_windows", boom)
    monkeypatch.setattr(browsers, "_detect_macos", boom)
    monkeypatch.setattr(browsers, "_detect_linux", boom)
    with caplog.at_level("WARNING"):
        assert browsers.detect() == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "OSError" in logged
    assert "somebody" not in logged  # message body must never be logged


def test_detect_dispatches_on_platform(monkeypatch):
    calls = []
    monkeypatch.setattr(browsers, "_detect_windows",
                        lambda: calls.append("win") or [])
    monkeypatch.setattr(browsers, "_detect_macos",
                        lambda: calls.append("mac") or [])
    monkeypatch.setattr(browsers, "_detect_linux",
                        lambda: calls.append("lin") or [])

    monkeypatch.setattr(sys, "platform", "win32")
    browsers.detect()
    monkeypatch.setattr(sys, "platform", "darwin")
    browsers.detect()
    monkeypatch.setattr(sys, "platform", "linux")
    browsers.detect()
    assert calls == ["win", "mac", "lin"]


# --------------------------------------------------------------------------
# user_data_dir
# --------------------------------------------------------------------------

def test_user_data_dir_is_derived_per_platform(monkeypatch, tmp_path):
    brand = browsers.BRANDS["chrome"]
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    result = browsers.user_data_dir(brand)
    assert result.endswith("User Data")
    assert str(tmp_path) in result


def test_user_data_dir_on_linux_honours_xdg_config_home(monkeypatch, tmp_path):
    brand = browsers.BRANDS["brave"]
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = browsers.user_data_dir(brand)
    assert str(tmp_path) in result
    assert result.endswith(brand.linux_config_dir.replace("/", _sep()))


def test_user_data_dir_on_macos_uses_application_support(monkeypatch, tmp_path):
    brand = browsers.BRANDS["chrome"]
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))
    result = browsers.user_data_dir(brand)
    assert "Application Support" in result
    assert result.endswith("Chrome")


def _sep():
    import os
    return os.sep


# --------------------------------------------------------------------------
# Windows: registry command parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ('"D:\\Apps\\Brave\\brave.exe" -- "%1"', "D:\\Apps\\Brave\\brave.exe"),
    ('"D:\\Apps\\Brave\\brave.exe"', "D:\\Apps\\Brave\\brave.exe"),
    ('D:\\Apps\\Brave\\brave.exe --single-argument %1',
     "D:\\Apps\\Brave\\brave.exe"),
    ('D:\\Apps\\Brave\\brave.exe', "D:\\Apps\\Brave\\brave.exe"),
    ('', None),
    ('   ', None),
])
def test_registry_command_values_are_parsed_not_assumed_bare(value, expected):
    assert browsers._parse_command_path(value) == expected


def test_ambiguous_exe_names_are_disambiguated_by_vendor():
    """chrome.exe belongs to two brands; only the vendor tells them apart."""
    chrome = browsers.BRANDS["chrome"]
    chromium = browsers.BRANDS["chromium"]
    path = "D:\\X\\Google\\Chrome\\Application\\chrome.exe"
    assert browsers._win_exe_matches(chrome, path)
    assert not browsers._win_exe_matches(chromium, path)


def test_unambiguous_exe_name_matches_any_install_location():
    brave = browsers.BRANDS["brave"]
    assert browsers._win_exe_matches(brave, "D:\\Somewhere\\Odd\\brave.exe")
    assert not browsers._win_exe_matches(brave, "D:\\Somewhere\\vivaldi.exe")


# --------------------------------------------------------------------------
# Windows: detection
# --------------------------------------------------------------------------

def _fake_exe(tmp_path, *parts):
    target = tmp_path.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"MZ")
    return target


def test_windows_registry_hit_wins_over_a_path_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    exe = _fake_exe(tmp_path, "Registered", "brave.exe")

    def fake_registry(brand):
        return str(exe) if brand.key == "brave" else None

    monkeypatch.setattr(browsers, "_win_registry_exe", fake_registry)
    monkeypatch.setattr(browsers, "_win_scan_roots", lambda: [])

    found = browsers._detect_windows()
    assert [i.brand.key for i in found] == ["brave"]
    assert found[0].exe_path == str(exe)
    assert found[0].user_data_dir.endswith("User Data")


def test_windows_falls_back_to_a_path_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    brand = browsers.BRANDS["vivaldi"]
    exe = _fake_exe(tmp_path, "PF", *brand.win_vendor, "Application",
                    brand.exe_name)

    monkeypatch.setattr(browsers, "_win_registry_exe", lambda b: None)
    monkeypatch.setattr(browsers, "_win_scan_roots", lambda: [tmp_path / "PF"])

    found = browsers._detect_windows()
    assert [i.brand.key for i in found] == ["vivaldi"]
    assert found[0].exe_path == str(exe)


def test_one_brands_registry_failure_does_not_hide_the_others(
        monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    edge = _fake_exe(tmp_path, "Edge", "msedge.exe")

    def fake_registry(brand):
        if brand.key == "chrome":
            raise OSError("access denied")
        return str(edge) if brand.key == "edge" else None

    monkeypatch.setattr(browsers, "_win_registry_exe", fake_registry)
    monkeypatch.setattr(browsers, "_win_scan_roots", lambda: [])

    found = browsers._detect_windows()
    assert [i.brand.key for i in found] == ["edge"]


def test_windows_results_come_back_in_brands_order(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    exes = {
        "opera": _fake_exe(tmp_path, "o", "opera.exe"),
        "chrome": _fake_exe(tmp_path, "c", "chrome.exe"),
        "brave": _fake_exe(tmp_path, "b", "brave.exe"),
    }
    monkeypatch.setattr(browsers, "_win_registry_exe",
                        lambda b: str(exes[b.key]) if b.key in exes else None)
    monkeypatch.setattr(browsers, "_win_scan_roots", lambda: [])

    order = [i.brand.key for i in browsers._detect_windows()]
    assert order == [k for k in browsers.BRANDS if k in exes]
    assert order == ["chrome", "brave", "opera"]


# --------------------------------------------------------------------------
# macOS: detection
# --------------------------------------------------------------------------

def _fake_app(root, brand):
    app = root / f"{brand.mac_app_name}.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (macos / brand.mac_app_name).write_bytes(b"\xcf\xfa")
    (app / "Contents" / "Info.plist").write_text(
        "<plist><dict><key>CFBundleIdentifier</key>"
        f"<string>{brand.mac_bundle_id}</string></dict></plist>",
        encoding="utf-8")
    return app


def test_macos_finds_an_app_bundle_and_its_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))
    apps = tmp_path / "Applications"
    apps.mkdir()
    brand = browsers.BRANDS["vivaldi"]
    app = _fake_app(apps, brand)
    monkeypatch.setattr(browsers, "_mac_app_roots", lambda: [apps])

    found = browsers._detect_macos()
    assert [i.brand.key for i in found] == ["vivaldi"]
    assert found[0].exe_path == str(
        app / "Contents" / "MacOS" / brand.mac_app_name)
    assert "Application Support" in found[0].user_data_dir


def test_macos_rejects_a_bundle_whose_identifier_belongs_to_another_brand(
        monkeypatch, tmp_path):
    """A directory named 'Brave Browser.app' is not proof it is Brave."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))
    apps = tmp_path / "Applications"
    apps.mkdir()
    brave = browsers.BRANDS["brave"]
    app = apps / f"{brave.mac_app_name}.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text(
        "<plist><dict><key>CFBundleIdentifier</key>"
        "<string>com.example.NotBrave</string></dict></plist>",
        encoding="utf-8")
    monkeypatch.setattr(browsers, "_mac_app_roots", lambda: [apps])

    assert browsers._detect_macos() == []


# --------------------------------------------------------------------------
# Linux: detection
# --------------------------------------------------------------------------

def test_linux_prefers_a_binary_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))
    binary = _fake_exe(tmp_path, "bin", "brave-browser")
    monkeypatch.setattr(
        browsers.shutil, "which",
        lambda name, **kw: str(binary) if name == "brave-browser" else None)
    monkeypatch.setattr(browsers, "_linux_desktop_dirs", lambda: [])

    found = browsers._detect_linux()
    assert [i.brand.key for i in found] == ["brave"]
    assert found[0].exe_path == str(binary)
    assert found[0].user_data_dir.endswith(
        browsers.BRANDS["brave"].linux_config_dir.replace("/", _sep()))


def test_linux_reads_a_desktop_entry_when_nothing_is_on_path(
        monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))
    binary = _fake_exe(tmp_path, "opt", "vivaldi")
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "vivaldi.desktop").write_text(
        # A path containing a space must be quoted in a .desktop Exec line;
        # tmp_path is one on some machines, so this also covers the quoted form.
        "[Desktop Entry]\nType=Application\n"
        f'Exec="{binary}" --no-sandbox %U\n', encoding="utf-8")
    monkeypatch.setattr(browsers.shutil, "which", lambda name, **kw: None)
    monkeypatch.setattr(browsers, "_linux_desktop_dirs", lambda: [apps])

    found = browsers._detect_linux()
    assert [i.brand.key for i in found] == ["vivaldi"]
    assert found[0].exe_path == str(binary)


def test_linux_finds_a_flatpak_install_and_its_own_config_dir(
        monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))
    brand = browsers.BRANDS["chromium"]
    (tmp_path / ".var" / "app" / brand.mac_bundle_id / "config").mkdir(
        parents=True)
    flatpak = _fake_exe(tmp_path, "bin", "flatpak")

    def which(name, **kw):
        return str(flatpak) if name == "flatpak" else None

    monkeypatch.setattr(browsers.shutil, "which", which)
    monkeypatch.setattr(browsers, "_linux_desktop_dirs", lambda: [])

    found = browsers._detect_linux()
    assert [i.brand.key for i in found] == ["chromium"]
    assert brand.mac_bundle_id in found[0].exe_path
    assert ".var" in found[0].user_data_dir
    assert found[0].user_data_dir.endswith(brand.linux_config_dir)


def test_linux_returns_empty_on_a_machine_with_no_browser(
        monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(browsers.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(browsers.shutil, "which", lambda name, **kw: None)
    monkeypatch.setattr(browsers, "_linux_desktop_dirs", lambda: [])
    assert browsers._detect_linux() == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux discovery path")
def test_linux_detection_finds_a_binary_on_path(tmp_path, monkeypatch):
    fake = tmp_path / "brave-browser"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    found = {i.brand.key for i in browsers.detect()}
    assert "brave" in found


# --------------------------------------------------------------------------
# No re-derivation escape hatch
# --------------------------------------------------------------------------

def test_module_offers_no_way_to_re_derive_a_brand_from_a_string():
    """Every consumer takes the resolved Installed from detect(). A lookup
    helper would reopen the launch-Chrome-while-checking-Brave bug."""
    public = {n for n in dir(browsers) if not n.startswith("_")}
    assert not {n for n in public
                if "for_" in n or n in {"brand_for", "from_key", "resolve"}}
