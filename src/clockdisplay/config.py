"""Host resolution, settings and small persistent state.

Host lookup order: explicit argument > $CLOCK_HOST > config file "host" > DEFAULT_HOST.
Config/state live in %LOCALAPPDATA%\\clockdisplay on Windows, ~/.config/clockdisplay
elsewhere (override dir with $CLOCK_CONFIG_DIR). Secrets never go here; see claude.auth.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

DEFAULT_HOST = "192.0.2.10"

DEFAULTS = {
    "host": DEFAULT_HOST,
    "poll_interval": 60,   # seconds between usage fetches (<30 s trips Anthropic's rate limiter)
    "force_push": 600,     # re-push unchanged numbers after this long so countdowns stay fresh
    "autopush": True,      # push to the display (False: tray only)
    "fire_rate": 40,       # 5h burn in %/hour that sets the 5h line on fire (0 = off; 20 = even pace)
    "fire_window": 600,    # seconds of history the burn rate is measured over
}

_LEGACY_DIR = Path.home() / ".config" / "clockdisplay"
_migrated = False


def config_dir() -> Path:
    override = os.environ.get("CLOCK_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        d = Path(os.environ["LOCALAPPDATA"]) / "clockdisplay"
        _migrate(d)
        return d
    return _LEGACY_DIR


def _migrate(new: Path) -> None:
    """One-time copy of config/state from ~/.config/clockdisplay to the new location."""
    global _migrated
    if _migrated:
        return
    _migrated = True
    if new.exists() or not _LEGACY_DIR.is_dir():
        return
    new.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "state.json"):
        if (_LEGACY_DIR / name).is_file():
            shutil.copy2(_LEGACY_DIR / name, new / name)


def logs_dir() -> Path:
    d = config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_file() -> Path:
    return config_dir() / "config.json"


def _read(name: str) -> dict:
    try:
        return json.loads((config_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict) -> None:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(data, indent=2), encoding="utf-8")


def resolve_host(explicit: str | None = None) -> str:
    return explicit or os.environ.get("CLOCK_HOST") or _read("config.json").get("host") or DEFAULT_HOST


def load_config() -> dict:
    """Settings with defaults filled in; $CLOCK_HOST still wins for the host."""
    cfg = {**DEFAULTS, **_read("config.json")}
    cfg["host"] = resolve_host()
    return cfg


def save_config(cfg: dict) -> None:
    _write("config.json", cfg)


def load_state() -> dict:
    return _read("state.json")


def save_state(state: dict) -> None:
    _write("state.json", state)
