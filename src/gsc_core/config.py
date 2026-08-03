"""User-tunable settings.

Deliberately small. Sites come from the Search Console API, and browsers come
from the detector — neither belongs in a config file a human has to write.
What remains is genuinely a preference.
"""
from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path

from . import paths, runlog

log = runlog.get(__name__)


class ConfigError(ValueError):
    """Configuration that cannot be used as written."""


DEFAULTS: dict = {
    # Request Indexing
    "property_slots": 11,
    "account_slots": None,       # tracked, not enforced — no ceiling observed
    "daily_reserve": 0,          # per property; unspendable by any tool
    "submit_delay_range": [130, 180],   # seconds; proven against live runs

    # URL Inspection
    "inspect_concurrency": 8,    # 600/min per property allows well above this
    "inspection_ttl_days": 7,

    # Behaviour
    "stop_on_throttle": True,
    "sync_submit_cap": 5,

    # Bridge
    "bridge_port": 8765,         # localhost only; the extension defaults to this
    "authuser": "0",             # the /u/N index of the Google account in GSC
    "auto_launch_browser": True,
    "bridge_connect_timeout": 60,  # seconds to wait for the extension to appear
}


def load(path: Path | None = None) -> dict:
    """Defaults with the user's file layered on top. Never raises."""
    target = path or paths.config_path()
    merged = deepcopy(DEFAULTS)
    try:
        user_values = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return merged
    except (OSError, ValueError) as exc:
        # OSError covers unreadable files and a path that is a directory;
        # ValueError covers both JSONDecodeError and UnicodeDecodeError.
        # Config is a convenience, never a reason to fail startup.
        log.warning("config at %s is unreadable (%s); using defaults",
                    target, type(exc).__name__)
        return merged

    if not isinstance(user_values, dict):
        log.warning("config at %s is not an object; using defaults", target)
        return merged

    merged.update(user_values)
    return merged


def save(data: dict, path: Path | None = None) -> None:
    """Write the config atomically.

    Not secret, so no permission hardening — but a half-written file would be
    read back as corrupt on next start, and a fixed temp name races when the
    server and CLI save at once.
    """
    target = path or paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".config-",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def validate(data: dict) -> list[str]:
    """Human-readable problems, empty when the config is usable."""
    problems: list[str] = []

    slots = data.get("property_slots")
    slots_ok = isinstance(slots, int) and not isinstance(slots, bool) and slots >= 1
    if not slots_ok:
        problems.append("property_slots must be a positive integer")

    account_slots = data.get("account_slots")
    if account_slots is not None and (
        not isinstance(account_slots, int) or isinstance(account_slots, bool)
        or account_slots < 1
    ):
        problems.append("account_slots must be null or a positive integer")

    reserve = data.get("daily_reserve")
    if not isinstance(reserve, int) or isinstance(reserve, bool) or reserve < 0:
        problems.append("daily_reserve must be zero or a positive integer")
    elif slots_ok and reserve >= slots:
        problems.append(
            f"daily_reserve ({reserve}) must be below property_slots ({slots})"
        )

    delay = data.get("submit_delay_range")
    if (not isinstance(delay, list) or len(delay) != 2
            or not all(isinstance(v, (int, float)) for v in delay)
            or delay[0] > delay[1] or delay[0] < 0):
        problems.append(
            "submit_delay_range must be [low, high] seconds with low <= high"
        )

    concurrency = data.get("inspect_concurrency")
    if (not isinstance(concurrency, int) or isinstance(concurrency, bool)
            or not 1 <= concurrency <= 60):
        problems.append("inspect_concurrency must be between 1 and 60")

    ttl = data.get("inspection_ttl_days")
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
        problems.append("inspection_ttl_days must be a positive integer")

    cap = data.get("sync_submit_cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 5:
        problems.append("sync_submit_cap must be between 1 and 5")

    if not isinstance(data.get("stop_on_throttle"), bool):
        problems.append("stop_on_throttle must be true or false")

    # bool subclasses int, so it is excluded explicitly throughout: `true` would
    # otherwise sail through as 1 for the timeout.
    port = data.get("bridge_port")
    if (not isinstance(port, int) or isinstance(port, bool)
            or not 1024 <= port <= 65535):
        problems.append("bridge_port must be an integer from 1024 to 65535")

    authuser = data.get("authuser")
    if not isinstance(authuser, str) or not authuser.isdigit():
        problems.append(
            "authuser must be a digit string — the /u/N index Search Console "
            'uses for the signed-in account, e.g. "0"'
        )

    if not isinstance(data.get("auto_launch_browser"), bool):
        problems.append("auto_launch_browser must be true or false")

    timeout = data.get("bridge_connect_timeout")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        problems.append("bridge_connect_timeout must be a positive integer")

    return problems
