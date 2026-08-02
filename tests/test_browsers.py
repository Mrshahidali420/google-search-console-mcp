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


def test_every_brand_states_its_own_data_locations():
    """Cheap guard against a seventh brand being added by copy-paste with a
    neighbour's paths left in place."""
    for key, brand in browsers.BRANDS.items():
        assert brand.mac_data_dir, key
        assert not brand.mac_data_dir.startswith("/"), key
        env_name, subpath = brand.win_data_dir
        assert env_name in {"LOCALAPPDATA", "APPDATA"}, key
        assert subpath and not subpath.startswith("/"), key

    # No two brands may share a data location, on either platform.
    mac_dirs = [b.mac_data_dir for b in browsers.BRANDS.values()]
    win_dirs = [b.win_data_dir for b in browsers.BRANDS.values()]
    assert len(set(mac_dirs)) == len(mac_dirs)
    assert len(set(win_dirs)) == len(win_dirs)


def test_flatpak_ids_are_reverse_dns_and_unique():
    ids = [b.flatpak_id for b in browsers.BRANDS.values()
           if b.flatpak_id is not None]
    assert len(set(ids)) == len(ids)
    for app_id in ids:
        parts = app_id.split(".")
        assert len(parts) >= 3, app_id
        assert all(parts), app_id
        assert not app_id.startswith("."), app_id
        assert " " not in app_id, app_id


def test_flatpak_id_is_not_assumed_to_equal_the_mac_bundle_id():
    """Edge and Opera are the two brands where reusing the bundle id as the
    flatpak id silently produces a directory that never exists."""
    assert browsers.BRANDS["edge"].mac_bundle_id == "com.microsoft.edgemac"
    assert browsers.BRANDS["edge"].flatpak_id == "com.microsoft.Edge"
    assert browsers.BRANDS["opera"].flatpak_id == "com.opera.Opera"
    assert (browsers.BRANDS["opera"].flatpak_id
            != browsers.BRANDS["opera"].mac_bundle_id)


def test_data_locations_are_not_derived_from_the_install_layout():
    """The install path and the data path genuinely disagree for these
    brands; a formula over win_vendor gets all three wrong."""
    # Vivaldi installs under Vivaldi\Application, stores under Vivaldi.
    assert browsers.BRANDS["vivaldi"].win_data_dir == (
        "LOCALAPPDATA", "Vivaldi/User Data")
    # Chromium has no vendor segment at all on Windows.
    assert browsers.BRANDS["chromium"].win_data_dir == (
        "LOCALAPPDATA", "Chromium/User Data")
    # Opera is the only brand under Roaming, and has no "User Data" segment.
    assert browsers.BRANDS["opera"].win_data_dir == (
        "APPDATA", "Opera Software/Opera Stable")
    # macOS Edge is one segment, not the two-segment Windows layout.
    assert browsers.BRANDS["edge"].mac_data_dir == "Microsoft Edge"


def test_opera_user_data_dir_lands_in_roaming_not_local(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    result = browsers.user_data_dir(browsers.BRANDS["opera"])
    assert str(tmp_path / "Roaming") in result
    # Compare against the fixture's own Local dir: tmp_path itself sits under
    # AppData\Local on Windows, so a bare "Local" substring check is useless.
    assert str(tmp_path / "Local") not in result
    assert result.endswith("Opera Stable")


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
        # Invented, not from any real machine: it stands in for the kind of
        # value a registry error carries — an account name in a path.
        raise OSError(r"X:\Users\example-account\AppData\Local\Example")

    monkeypatch.setattr(browsers, "_detect_windows", boom)
    monkeypatch.setattr(browsers, "_detect_macos", boom)
    monkeypatch.setattr(browsers, "_detect_linux", boom)
    with caplog.at_level("WARNING"):
        assert browsers.detect() == []
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "OSError" in logged
    assert "example-account" not in logged  # message body is never logged


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
    (tmp_path / ".var" / "app" / brand.flatpak_id / "config").mkdir(
        parents=True)
    flatpak = _fake_exe(tmp_path, "bin", "flatpak")

    def which(name, **kw):
        return str(flatpak) if name == "flatpak" else None

    monkeypatch.setattr(browsers.shutil, "which", which)
    monkeypatch.setattr(browsers, "_linux_desktop_dirs", lambda: [])

    found = browsers._detect_linux()
    assert [i.brand.key for i in found] == ["chromium"]
    assert brand.flatpak_id in found[0].exe_path
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
# Windows: the registry readers themselves, against a fake winreg
#
# These are the highest-risk lines in the module and Windows is the primary
# platform, so they are exercised directly rather than monkeypatched away.
# The fake is installed via monkeypatch.setitem(sys.modules, ...) so pytest
# tears it down: a leaked sys.modules entry would corrupt every later test.
# --------------------------------------------------------------------------

class _FakeKey:
    def __init__(self, hive, subkey):
        self.hive = hive
        self.subkey = subkey

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeWinreg:
    """Just enough of winreg for the two readers under test."""

    HKEY_LOCAL_MACHINE = "HKLM"
    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, values, errors=None):
        self._values = dict(values)      # {(hive, subkey): default value}
        self._errors = dict(errors or {})  # {(hive, subkey): exception}

    def _children(self, hive, subkey):
        prefix = subkey + "\\"
        names = []
        for hive_name, path in self._values:
            if hive_name == hive and path.startswith(prefix):
                child = path[len(prefix):].split("\\")[0]
                if child not in names:
                    names.append(child)
        return names

    def OpenKey(self, hive, subkey):
        error = self._errors.get((hive, subkey))
        if error is not None:
            raise error
        if (hive, subkey) in self._values or self._children(hive, subkey):
            return _FakeKey(hive, subkey)
        raise FileNotFoundError(2, "key not found")

    def QueryValueEx(self, key, name):
        try:
            return self._values[(key.hive, key.subkey)], 1
        except KeyError:
            raise FileNotFoundError(2, "value not found") from None

    def QueryInfoKey(self, key):
        return (len(self._children(key.hive, key.subkey)), 0, 0)

    def EnumKey(self, key, index):
        children = self._children(key.hive, key.subkey)
        try:
            return children[index]
        except IndexError:
            raise OSError(22, "no more entries") from None


@pytest.fixture
def fake_winreg(monkeypatch):
    def install(values, errors=None):
        fake = _FakeWinreg(values, errors)
        monkeypatch.setitem(sys.modules, "winreg", fake)
        return fake
    return install


_START_MENU = "Software\\Clients\\StartMenuInternet"
_APP_PATHS = "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths"


def test_start_menu_reader_parses_a_quoted_command_with_arguments(
        fake_winreg, tmp_path):
    brave_exe = _fake_exe(tmp_path, "Brave", "brave.exe")
    vivaldi_exe = _fake_exe(tmp_path, "Vivaldi", "vivaldi.exe")
    fake = fake_winreg({
        # A decoy under a different vendor key proves the reader matches on
        # the executable, not on the subkey name.
        ("HKLM", f"{_START_MENU}\\Vivaldi\\shell\\open\\command"):
            f'"{vivaldi_exe}" -- "%1"',
        ("HKLM", f"{_START_MENU}\\BraveHTML\\shell\\open\\command"):
            f'"{brave_exe}" -- "%1"',
    })

    found = browsers._win_start_menu_exe(
        fake.HKEY_LOCAL_MACHINE, browsers.BRANDS["brave"])
    assert found == str(brave_exe)


def test_start_menu_reader_returns_none_when_the_key_is_absent(fake_winreg):
    fake = fake_winreg({})
    found = browsers._win_start_menu_exe(
        fake.HKEY_LOCAL_MACHINE, browsers.BRANDS["brave"])
    assert found is None


def test_start_menu_reader_skips_a_registered_but_uninstalled_browser(
        fake_winreg, tmp_path):
    """A stale entry for a removed browser must not be reported as found."""
    missing = tmp_path / "Gone" / "brave.exe"
    fake = fake_winreg({
        ("HKLM", f"{_START_MENU}\\BraveHTML\\shell\\open\\command"):
            f'"{missing}" -- "%1"',
    })
    found = browsers._win_start_menu_exe(
        fake.HKEY_LOCAL_MACHINE, browsers.BRANDS["brave"])
    assert found is None


def test_app_paths_reader_finds_an_unquoted_value(fake_winreg, tmp_path):
    edge_exe = _fake_exe(tmp_path, "Edge", "msedge.exe")
    fake = fake_winreg({("HKCU", f"{_APP_PATHS}\\msedge.exe"): str(edge_exe)})

    found = browsers._win_app_paths_exe(
        fake.HKEY_CURRENT_USER, browsers.BRANDS["edge"])
    assert found == str(edge_exe)


def test_registry_lookup_falls_through_to_the_current_user_hive(
        fake_winreg, tmp_path):
    """A per-user install registers under HKCU only."""
    exe = _fake_exe(tmp_path, "UserInstall", "vivaldi.exe")
    fake_winreg({
        ("HKCU", f"{_START_MENU}\\Vivaldi\\shell\\open\\command"):
            f'"{exe}" -- "%1"',
    })
    assert browsers._win_registry_exe(browsers.BRANDS["vivaldi"]) == str(exe)


def test_an_unreadable_key_is_swallowed_and_the_next_lookup_still_runs(
        fake_winreg, tmp_path):
    """A locked-down hive raises OSError; it must not end the search."""
    exe = _fake_exe(tmp_path, "AppPaths", "brave.exe")
    fake_winreg(
        {("HKLM", f"{_APP_PATHS}\\brave.exe"): str(exe)},
        errors={("HKLM", _START_MENU): PermissionError(5, "access denied")},
    )
    assert browsers._win_registry_exe(browsers.BRANDS["brave"]) == str(exe)


def test_one_brand_raising_in_the_registry_does_not_hide_the_rest(
        fake_winreg, tmp_path, monkeypatch):
    """The per-brand guard, exercised through the real registry readers.

    The injected error is deliberately NOT an OSError: OSError is handled
    inside the reader, so only a non-OSError reaches the outer guard in
    _detect_windows, which is the code path under test here.
    """
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(browsers, "_win_scan_roots", lambda: [])
    brave_exe = _fake_exe(tmp_path, "Brave", "brave.exe")

    fake_winreg(
        {("HKLM", f"{_START_MENU}\\BraveHTML\\shell\\open\\command"):
            f'"{brave_exe}" -- "%1"'},
        errors={("HKLM", f"{_APP_PATHS}\\chrome.exe"):
                RuntimeError("registry subsystem exploded")},
    )

    found = browsers._detect_windows()
    assert [i.brand.key for i in found] == ["brave"]


def test_detect_never_raises_even_when_the_registry_explodes(
        fake_winreg, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(browsers, "_win_scan_roots", explode)
    fake_winreg({})
    assert browsers.detect() == []


# --------------------------------------------------------------------------
# No re-derivation escape hatch
# --------------------------------------------------------------------------

def test_module_offers_no_way_to_re_derive_a_brand_from_a_string():
    """Every consumer takes the resolved Installed from detect(). A lookup
    helper would reopen the launch-Chrome-while-checking-Brave bug."""
    public = {n for n in dir(browsers) if not n.startswith("_")}
    assert not {n for n in public
                if "for_" in n or n in {"brand_for", "from_key", "resolve"}}
