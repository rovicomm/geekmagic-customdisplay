"""Host resolution, settings and small persistent state.

Displays come from config.json "displays": [{"name", "host", "app"}]. Configs without it
(the old single "host" key) are treated as one display. Default host lookup order:
explicit argument > $CLOCK_HOST > first display > config file "host" > DEFAULT_HOST.
Config/state live in %LOCALAPPDATA%\\clockdisplay on Windows, ~/.config/clockdisplay
elsewhere (override dir with $CLOCK_CONFIG_DIR). Secrets never go here; see claude.auth.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
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

# Plane spotting from a local ADS-B receiver (readsb/tar1090/dump1090 "aircraft.json").
ADSB_DEFAULTS = {
    "enabled": True,        # still does nothing until "url" is set
    "url": "",              # receiver base URL, e.g. http://192.168.1.20:8080
    "poll_interval": 5,     # seconds between aircraft.json fetches
    "location": None,       # [lat, lon] to measure from; default: the receiver's own position
    "radius": 5,            # nautical miles from location that counts as "overhead"
    "min_altitude": 0,      # feet
    "max_altitude": 0,      # feet; 0 = no ceiling
    "include_ground": False,
    "types": [],            # ICAO type-code prefixes to show, e.g. ["B74", "A38"]; [] = all
    "callsigns": [],        # callsign prefixes to show, e.g. ["BAW", "DAL"]; [] = all
    "military_only": False,
    "popup": True,          # pop planes up over "claude" displays
    "popup_seconds": 30,    # how long a pop-up stays before the usage card comes back
    "cooldown": 1800,       # seconds before the same aircraft can pop up again
    "refresh": 20,          # seconds between card refreshes on "adsb" displays
    "fields": ["photo", "callsign", "type", "registration", "operator",
               "altitude", "speed", "distance"],
}
ADSB_FIELDS = ("photo", "callsign", "type", "registration", "operator", "altitude",
               "speed", "distance", "squawk")

APPS = ("claude", "adsb", "off")  # what a display can run; "claude" is the usage meter
_FLAT_STATE = ("previous_theme", "showing", "hash")  # pre-multi-display state.json keys

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
    cfg = _read("config.json")
    first = next((d.get("host") for d in cfg.get("displays") or [] if isinstance(d, dict)), None)
    return explicit or os.environ.get("CLOCK_HOST") or first or cfg.get("host") or DEFAULT_HOST


def load_displays() -> list[dict]:
    """Configured displays, normalised; a legacy single-host config becomes one display."""
    raw = _read("config.json").get("displays")
    if not isinstance(raw, list):
        return [{"name": "display", "host": resolve_host(), "app": "claude"}]
    displays, names = [], set()
    for d in raw:
        if not isinstance(d, dict) or not d.get("host"):
            continue
        host = str(d["host"])
        name = str(d.get("name") or host)
        if name in names:
            continue
        names.add(name)
        displays.append({**d, "name": name, "host": host, "app": d.get("app") or "claude"})
    return displays


def find_display(name: str) -> dict:
    displays = load_displays()
    for d in displays:
        if d["name"] == name:
            return d
    known = ", ".join(d["name"] for d in displays) or "none"
    raise ValueError(f"no display named {name!r} (configured: {known})")


def rename_display(old: str, new: str) -> None:
    """Rename a display in config.json. A legacy single-host config is converted to a
    "displays" list first, so the name has somewhere to live."""
    new = new.strip()
    if not new or new == "all":
        raise ValueError(f"{new!r} can't be used as a display name")
    names = [d["name"] for d in load_displays()]
    if old not in names:
        find_display(old)  # raises with the list of known names
    if new != old and new in names:
        raise ValueError(f"there is already a display named {new!r}")
    raw = _read("config.json")
    if not isinstance(raw.get("displays"), list):
        raw["displays"] = load_displays()
        raw.pop("host", None)
    for d in raw["displays"]:
        if isinstance(d, dict) and d.get("host") and str(d.get("name") or d["host"]) == old:
            d["name"] = new
            break
    save_config(raw)


def load_config() -> dict:
    """Settings with defaults filled in; $CLOCK_HOST still wins for the host."""
    cfg = {**DEFAULTS, **_read("config.json")}
    adsb = cfg.get("adsb")
    cfg["adsb"] = {**ADSB_DEFAULTS, **(adsb if isinstance(adsb, dict) else {})}
    cfg["host"] = resolve_host()
    cfg["displays"] = load_displays()
    return cfg


def save_config(cfg: dict) -> None:
    _write("config.json", cfg)


def set_adsb(key: str, value) -> None:
    """Change one key of the "adsb" block in config.json, leaving the rest of the file alone."""
    raw = _read("config.json")
    if not isinstance(raw.get("adsb"), dict):
        raw["adsb"] = {}
    raw["adsb"][key] = value
    save_config(raw)


_state_lock = threading.Lock()


def _load_states() -> dict:
    """Whole state.json, with pre-multi-display flat keys moved under the default host."""
    state = _read("state.json")
    flat = {k: state.pop(k) for k in _FLAT_STATE if k in state}
    displays = state.setdefault("displays", {})
    if flat:
        displays.setdefault(resolve_host(), {}).update(flat)
    return state


def load_display_state(host: str) -> dict:
    with _state_lock:
        return dict(_load_states()["displays"].get(host, {}))


def save_display_state(host: str, st: dict) -> None:
    """Read-modify-write so parallel pushes to different displays don't clobber each other."""
    with _state_lock:
        state = _load_states()
        state["displays"][host] = st
        _write("state.json", state)
