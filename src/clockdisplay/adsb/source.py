"""Aircraft from a local ADS-B receiver's JSON API (readsb, tar1090, dump1090-fa, piaware).

All of them serve `<base>/data/aircraft.json` ({"now", "aircraft": [...]}) and
`<base>/data/receiver.json` (which has the receiver's lat/lon when it's configured).
readsb with its aircraft database also fills in registration ("r"), type code ("t"),
type description ("desc"), operator ("ownOp") and year, which the card uses.
"""
from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import dataclass

TIMEOUT = 5
STALE_POSITION = 60  # seconds; older positions are ignored
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


class SourceError(RuntimeError):
    pass


@dataclass
class Aircraft:
    hex: str
    callsign: str = ""
    registration: str = ""
    type_code: str = ""
    description: str = ""
    operator: str = ""
    year: str = ""
    altitude: int | None = None  # feet (barometric), None when unknown
    on_ground: bool = False
    speed: float | None = None   # ground speed, knots
    track: float | None = None   # degrees
    vertical_rate: int | None = None  # feet/minute
    squawk: str = ""
    lat: float | None = None
    lon: float | None = None
    seen_pos: float | None = None
    military: bool = False
    distance: float | None = None  # nautical miles from the observer
    bearing: float | None = None   # degrees from the observer to the aircraft

    @property
    def name(self) -> str:
        """Best short label: callsign, else registration, else the ICAO hex."""
        return self.callsign or self.registration or self.hex.upper()

    @property
    def direction(self) -> str:
        return "" if self.bearing is None else COMPASS[round(self.bearing / 45) % 8]


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_aircraft(raw: dict) -> Aircraft:
    alt = raw.get("alt_baro", raw.get("altitude"))
    on_ground = alt == "ground"
    rate = _num(raw.get("baro_rate", raw.get("geom_rate")))
    return Aircraft(
        hex=str(raw.get("hex", "")).lstrip("~").lower(),
        callsign=str(raw.get("flight") or "").strip(),
        registration=str(raw.get("r") or "").strip(),
        type_code=str(raw.get("t") or "").strip(),
        description=" ".join(str(raw.get("desc") or "").split()),
        operator=" ".join(str(raw.get("ownOp") or "").split()),
        year=str(raw.get("year") or "").strip(),
        altitude=0 if on_ground else (int(a) if (a := _num(alt)) is not None else None),
        on_ground=on_ground,
        speed=_num(raw.get("gs")),
        track=_num(raw.get("track", raw.get("true_heading"))),
        vertical_rate=int(rate) if rate is not None else None,
        squawk=str(raw.get("squawk") or ""),
        lat=_num(raw.get("lat")),
        lon=_num(raw.get("lon")),
        seen_pos=_num(raw.get("seen_pos")),
        military=bool(int(raw.get("dbFlags") or 0) & 1),
    )


def distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Great-circle distance (nautical miles) and initial bearing (degrees) from 1 to 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    nm = 2 * math.asin(math.sqrt(min(1.0, a))) * 3440.065
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return nm, (math.degrees(math.atan2(y, x)) + 360) % 360


def _get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as e:
        raise SourceError(f"GET {url} failed: {e}") from e


def _base(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if "://" in url else f"http://{url}"


def fetch_receiver(url: str) -> tuple[float, float] | None:
    """The receiver's configured position, if it publishes one."""
    data = _get_json(f"{_base(url)}/data/receiver.json")
    lat, lon = _num(data.get("lat")), _num(data.get("lon"))
    return (lat, lon) if lat is not None and lon is not None else None


def fetch_aircraft(url: str, location: tuple[float, float] | None = None) -> list[Aircraft]:
    """Every aircraft the receiver currently tracks, with distance/bearing from `location`
    filled in for those with a recent position."""
    data = _get_json(f"{_base(url)}/data/aircraft.json")
    out = []
    for raw in data.get("aircraft") or []:
        if not isinstance(raw, dict) or not raw.get("hex"):
            continue
        ac = parse_aircraft(raw)
        fresh = ac.seen_pos is None or ac.seen_pos <= STALE_POSITION
        if location and ac.lat is not None and ac.lon is not None and fresh:
            ac.distance, ac.bearing = distance_bearing(location[0], location[1], ac.lat, ac.lon)
        out.append(ac)
    return out


def _prefixed(value: str, prefixes: list[str]) -> bool:
    return not prefixes or any(value.upper().startswith(str(p).upper()) for p in prefixes)


def matches(ac: Aircraft, settings: dict) -> bool:
    """Whether `ac` passes the user's filters (see config.ADSB_DEFAULTS)."""
    if ac.distance is None or ac.distance > float(settings["radius"]):
        return False
    if ac.on_ground and not settings["include_ground"]:
        return False
    if not ac.on_ground:
        if ac.altitude is None:
            return False
        if ac.altitude < float(settings["min_altitude"]):
            return False
        if float(settings["max_altitude"]) > 0 and ac.altitude > float(settings["max_altitude"]):
            return False
    if settings["military_only"] and not ac.military:
        return False
    return _prefixed(ac.type_code, settings["types"]) and _prefixed(ac.callsign, settings["callsigns"])


def overhead(aircraft: list[Aircraft], settings: dict) -> list[Aircraft]:
    """Aircraft passing the filters, nearest first."""
    return sorted((ac for ac in aircraft if matches(ac, settings)), key=lambda ac: ac.distance)
