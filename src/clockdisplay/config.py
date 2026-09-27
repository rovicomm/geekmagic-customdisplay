"""Host resolution and small persistent state.

Host lookup order: explicit argument > $CLOCK_HOST > config file "host" > DEFAULT_HOST.
Config/state live in ~/.config/clockdisplay/ (override dir with $CLOCK_CONFIG_DIR).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_HOST = "192.0.2.10"


def config_dir() -> Path:
    override = os.environ.get("CLOCK_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".config" / "clockdisplay"


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


def load_state() -> dict:
    return _read("state.json")


def save_state(state: dict) -> None:
    _write("state.json", state)
