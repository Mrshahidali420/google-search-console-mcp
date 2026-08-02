"""Which Chromium browsers are installed, and where they keep their state.

Layer 1 of three. profiles.py is Layers 2 and 3.

Detection is registry-first on Windows because a path scan misses every
non-default install location, and the registry is what the OS itself
consults to launch a browser. The ProgramFiles/LOCALAPPDATA scan is kept
as a fallback for installs that never registered.

Every consumer is handed ONE resolved Installed object. Deriving the brand
independently at each call site is how a tool ends up launching Chrome
while checking whether Brave is running. There is deliberately no helper
here that turns a brand string back into a Brand: that is the escape hatch
which reopened the bug last time.

Nothing in this module writes to stdout, and detection failures are logged
by exception TYPE NAME only — a registry value or a filesystem path can
carry the operator's account name, and this is a public tool.
"""
from __future__ import annotations

import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from gsc_core import runlog

log = runlog.get("browsers")


@dataclass(frozen=True)
class Brand:
    key: str
    label: str
    exe_name: str
    extensions_url: str
    win_vendor: tuple[str, str]       # (Vendor, Product) under ProgramFiles
    mac_bundle_id: str
    mac_app_name: str
    linux_binaries: tuple[str, ...]
    linux_config_dir: str


@dataclass(frozen=True)
class Installed:
    brand: Brand
    exe_path: str
    user_data_dir: str


BRANDS: dict[str, Brand] = {
    "chrome": Brand(
        key="chrome", label="Google Chrome", exe_name="chrome.exe",
        extensions_url="chrome://extensions",
        win_vendor=("Google", "Chrome"),
        mac_bundle_id="com.google.Chrome", mac_app_name="Google Chrome",
        linux_binaries=("google-chrome", "google-chrome-stable"),
        linux_config_dir="google-chrome"),
    "brave": Brand(
        key="brave", label="Brave", exe_name="brave.exe",
        extensions_url="brave://extensions",
        win_vendor=("BraveSoftware", "Brave-Browser"),
        mac_bundle_id="com.brave.Browser", mac_app_name="Brave Browser",
        linux_binaries=("brave-browser", "brave"),
        linux_config_dir="BraveSoftware/Brave-Browser"),
    "edge": Brand(
        key="edge", label="Microsoft Edge", exe_name="msedge.exe",
        extensions_url="edge://extensions",
        win_vendor=("Microsoft", "Edge"),
        mac_bundle_id="com.microsoft.edgemac", mac_app_name="Microsoft Edge",
        linux_binaries=("microsoft-edge", "microsoft-edge-stable"),
        linux_config_dir="microsoft-edge"),
    "vivaldi": Brand(
        key="vivaldi", label="Vivaldi", exe_name="vivaldi.exe",
        extensions_url="vivaldi://extensions",
        win_vendor=("Vivaldi", "Application"),
        mac_bundle_id="com.vivaldi.Vivaldi", mac_app_name="Vivaldi",
        linux_binaries=("vivaldi", "vivaldi-stable"),
        linux_config_dir="vivaldi"),
    "opera": Brand(
        key="opera", label="Opera", exe_name="opera.exe",
        extensions_url="opera://extensions",
        win_vendor=("Programs", "Opera"),
        mac_bundle_id="com.operasoftware.Opera", mac_app_name="Opera",
        linux_binaries=("opera",), linux_config_dir="opera"),
    "chromium": Brand(
        key="chromium", label="Chromium", exe_name="chrome.exe",
        extensions_url="chrome://extensions",
        win_vendor=("Chromium", "Application"),
        mac_bundle_id="org.chromium.Chromium", mac_app_name="Chromium",
        linux_binaries=("chromium", "chromium-browser"),
        linux_config_dir="chromium"),
}

# chrome.exe is claimed by two brands. Where an executable name is shared,
# the file name alone cannot identify the browser and the install path has
# to agree with the vendor as well.
_AMBIGUOUS_EXE_NAMES = frozenset(
    name for name, count in
    Counter(b.exe_name.lower() for b in BRANDS.values()).items() if count > 1
)

_WIN_START_MENU = r"Software\Clients\StartMenuInternet"
_WIN_APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths"


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def detect() -> list[Installed]:
    """Every Chromium browser installed on this machine, in BRANDS order.

    Never raises: a machine with no Chromium browser at all is a supported
    state that the recommendation layer reports as "install one". Detection
    failures are logged by TYPE NAME and skipped, because a registry read
    that fails on one brand must not hide the other five.
    """
    try:
        if sys.platform == "win32":
            found = _detect_windows()
        elif sys.platform == "darwin":
            found = _detect_macos()
        else:
            found = _detect_linux()
    except Exception as exc:  # noqa: BLE001 — detection is best-effort
        log.warning("browser detection failed (%s)", type(exc).__name__)
        return []
    return found


def user_data_dir(brand: Brand) -> str:
    """Where this brand keeps its profiles on this platform.

    A location only: the directory is not created and may not exist, which
    is exactly how "installed but never launched" is told apart later.
    """
    vendor, product = brand.win_vendor
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return str(root / vendor / product / "User Data")
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
        return str(root / vendor / product)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return str(root.joinpath(*brand.linux_config_dir.split("/")))


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _parse_command_path(value: str | None) -> str | None:
    """Pull the executable out of a registry shell-open command.

    The value is typically a quoted path followed by arguments, but an
    unquoted one is legal too, so neither form may be assumed.
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        candidate = text[1:end] if end > 1 else text[1:]
        return candidate.strip() or None
    lowered = text.lower()
    cut = lowered.find(".exe")
    if cut != -1:
        return text[:cut + 4]
    return text.split(" ", 1)[0] or None


def _win_exe_matches(brand: Brand, exe_path: str) -> bool:
    """Does this executable actually belong to this brand?"""
    name = os.path.basename(exe_path).lower()
    if name != brand.exe_name.lower():
        return False
    if name not in _AMBIGUOUS_EXE_NAMES:
        return True
    parts = {p.lower() for p in Path(exe_path).parts}
    # Only the vendor directory disambiguates. The product segment is not
    # enough: two brands both install under an "Application" folder.
    return brand.win_vendor[0].lower() in parts


def _win_scan_roots() -> list[Path]:
    """Install roots to sweep when the registry knows nothing."""
    names = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
    roots: list[Path] = []
    for name in names:
        value = os.environ.get(name)
        if value:
            root = Path(value)
            if root not in roots:
                roots.append(root)
    return roots


def _win_read_default(hive, subkey: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _kind = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _win_registry_exe(brand: Brand) -> str | None:
    """Ask the registry where this brand lives, HKLM first then HKCU.

    ``winreg`` is imported inside the function on purpose: this module has
    to import cleanly on macOS and Linux.
    """
    import winreg

    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for finder in (_win_start_menu_exe, _win_app_paths_exe):
            exe = finder(hive, brand)
            if exe:
                return exe
    return None


def _win_start_menu_exe(hive, brand: Brand) -> str | None:
    """Walk StartMenuInternet rather than guessing each vendor's key name."""
    import winreg

    try:
        with winreg.OpenKey(hive, _WIN_START_MENU) as key:
            count = winreg.QueryInfoKey(key)[0]
            names = [winreg.EnumKey(key, i) for i in range(count)]
    except OSError:
        return None

    for name in names:
        command = _win_read_default(
            hive, rf"{_WIN_START_MENU}\{name}\shell\open\command")
        exe = _parse_command_path(command)
        if exe and _win_exe_matches(brand, exe) and os.path.isfile(exe):
            return exe
    return None


def _win_app_paths_exe(hive, brand: Brand) -> str | None:
    command = _win_read_default(hive, rf"{_WIN_APP_PATHS}\{brand.exe_name}")
    exe = _parse_command_path(command)
    if exe and _win_exe_matches(brand, exe) and os.path.isfile(exe):
        return exe
    return None


def _win_scan_exe(brand: Brand) -> str | None:
    """Fallback for an install that never registered itself."""
    vendor, product = brand.win_vendor
    for root in _win_scan_roots():
        candidates = (
            root / vendor / product / "Application" / brand.exe_name,
            root / vendor / product / brand.exe_name,
        )
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return str(candidate)
            except OSError:
                continue
    return None


def _detect_windows() -> list[Installed]:
    found: list[Installed] = []
    for brand in BRANDS.values():
        try:
            exe = _win_registry_exe(brand) or _win_scan_exe(brand)
        except Exception as exc:  # noqa: BLE001 — one brand must not hide five
            log.debug("windows detection skipped %s (%s)",
                      brand.key, type(exc).__name__)
            continue
        if exe:
            found.append(Installed(brand, exe, user_data_dir(brand)))
    return found


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

def _mac_app_roots() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def _mac_bundle_id_agrees(app: Path, brand: Brand) -> bool:
    """A folder named 'Brave Browser.app' is not proof that it is Brave.

    An unreadable or absent Info.plist is not treated as a mismatch — the
    bundle is accepted on its name in that case, since refusing would drop
    a browser the user can plainly see.
    """
    plist = app / "Contents" / "Info.plist"
    try:
        if not plist.is_file():
            return True
        text = plist.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True
    if "CFBundleIdentifier" not in text:
        return True
    return brand.mac_bundle_id in text


def _mac_binary(app: Path, brand: Brand) -> str:
    macos = app / "Contents" / "MacOS"
    named = macos / brand.mac_app_name
    try:
        if named.is_file():
            return str(named)
        entries = sorted(p for p in macos.iterdir() if p.is_file())
    except OSError:
        return str(app)
    return str(entries[0]) if entries else str(app)


def _detect_macos() -> list[Installed]:
    found: list[Installed] = []
    for brand in BRANDS.values():
        try:
            for root in _mac_app_roots():
                app = root / f"{brand.mac_app_name}.app"
                if not app.is_dir():
                    continue
                if not _mac_bundle_id_agrees(app, brand):
                    continue
                found.append(Installed(
                    brand, _mac_binary(app, brand), user_data_dir(brand)))
                break
        except Exception as exc:  # noqa: BLE001 — one brand must not hide five
            log.debug("macos detection skipped %s (%s)",
                      brand.key, type(exc).__name__)
    return found


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def _linux_desktop_dirs() -> list[Path]:
    return [
        Path.home() / ".local" / "share" / "applications",
        Path("/usr/share/applications"),
    ]


def _linux_path_exe(brand: Brand) -> str | None:
    for binary in brand.linux_binaries:
        hit = shutil.which(binary)
        if hit:
            return hit
    return None


def _linux_desktop_exe(brand: Brand) -> str | None:
    """Read Exec= from <binary>.desktop rather than globbing every entry."""
    for directory in _linux_desktop_dirs():
        for binary in brand.linux_binaries:
            entry = directory / f"{binary}.desktop"
            try:
                if not entry.is_file():
                    continue
                lines = entry.read_text(
                    encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.startswith("Exec="):
                    continue
                exe = _parse_exec_value(line[len("Exec="):])
                if exe and os.path.exists(exe):
                    return exe
    return None


def _parse_exec_value(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 1 else text[1:]
    return text.split(" ", 1)[0] or None


def _linux_flatpak(brand: Brand) -> tuple[str, str] | None:
    """A flatpak install keeps its config inside the sandbox, not ~/.config.

    Returns (launch command, user data dir) or None. The bundle identifier
    is reused as the flatpak application id — right for most brands, and a
    miss only costs a detection, never a wrong answer.
    """
    app_dir = Path.home() / ".var" / "app" / brand.mac_bundle_id
    try:
        if not app_dir.is_dir():
            return None
    except OSError:
        return None
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return None
    config = app_dir / "config"
    data_dir = config.joinpath(*brand.linux_config_dir.split("/"))
    return f"{flatpak} run {brand.mac_bundle_id}", str(data_dir)


def _detect_linux() -> list[Installed]:
    found: list[Installed] = []
    for brand in BRANDS.values():
        try:
            exe = _linux_path_exe(brand) or _linux_desktop_exe(brand)
            if exe:
                found.append(Installed(brand, exe, user_data_dir(brand)))
                continue
            flat = _linux_flatpak(brand)
            if flat:
                found.append(Installed(brand, flat[0], flat[1]))
        except Exception as exc:  # noqa: BLE001 — one brand must not hide five
            log.debug("linux detection skipped %s (%s)",
                      brand.key, type(exc).__name__)
    return found
