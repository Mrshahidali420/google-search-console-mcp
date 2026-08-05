"""User-tunable settings.

Deliberately small. Sites come from the Search Console API, and which browsers
EXIST comes from the detector — neither belongs in a config file a human has
to write. What remains is genuinely a preference, and which of the detected
browsers to drive turns out to be one of them: the detector can rank the
profiles it finds, but it cannot know that the operator keeps their Search
Console work in Brave and their personal mail in Chrome. That choice is
recorded here by gsc_use_browser rather than in the server's memory, because
an MCP server is respawned at every session start and a preference that dies
with the process is not a preference.
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
    "submit_retries": 1,         # re-sends allowed ONLY before the click

    # URL Inspection
    "inspect_concurrency": 20,   # api.MAX_WORKERS; the 600/min gate is upstream
    "inspection_ttl_days": 7,

    # Behaviour
    "stop_on_throttle": True,
    "sync_submit_cap": 5,

    # Bridge
    "bridge_port": 8765,         # localhost only; the extension defaults to this
    "authuser": "0",             # the /u/N index of the Google account in GSC
    "auto_launch_browser": True,
    "bridge_connect_timeout": 60,  # seconds to wait for the extension to appear

    # Browser choice — null means "let the detector rank them", which is the
    # right answer for almost everyone. Set by gsc_use_browser, never by hand
    # if it can be helped: the tool validates the pair against the profiles
    # that actually exist, and a typo here is a browser that cannot be found.
    "browser": None,          # a brand KEY, e.g. "brave" — not the label
    "browser_profile": None,  # a profile DIRECTORY, e.g. "Default"
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

    # Capped low on purpose. A retry only ever re-sends a URL that failed
    # before the click, but each one costs another trip through the browser
    # UI — a live failure took 2m18s to arrive — so a large number turns one
    # broken URL into a stalled run.
    retries = data.get("submit_retries")
    if (not isinstance(retries, int) or isinstance(retries, bool)
            or not 0 <= retries <= 3):
        problems.append("submit_retries must be between 0 and 3")

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

    problems.extend(_browser_problems(data))
    return problems


def _browser_problems(data: dict) -> list[str]:
    """The pinned browser, if there is one — shape only, not existence.

    Whether the pinned profile is still on the machine is a question for the
    detector, and asking it here would make validate() read six browsers'
    state files. This checks the two things that can be known from the text:
    that each value is a non-empty string when present, and that a profile is
    never pinned without a browser. A bare profile is not a near-miss to be
    guessed at — "Default" names a directory in every brand installed, so
    resolving it alone would pick one at random.
    """
    problems: list[str] = []
    browser = data.get("browser")
    profile = data.get("browser_profile")

    for key, value in (("browser", browser), ("browser_profile", profile)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            problems.append(f"{key} must be null or a non-empty string")

    if profile is not None and browser is None:
        problems.append(
            "browser_profile is set but browser is not — pin both with "
            "gsc_use_browser, or neither"
        )
    return problems
