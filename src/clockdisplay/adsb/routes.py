"""Flight routes (origin -> destination airports) by callsign, from the routeset API that
tar1090 uses (adsb.im by default).

The receiver only knows what the aircraft broadcasts, so routes come from this crowd-sourced
database. Each answer has a "plausible" flag that checks the route against the aircraft's
position; implausible routes (stale or reused flight numbers) are dropped. Only airline-style
callsigns (three letters then a digit, e.g. BAW123) are looked up. Results, including "no
route", are cached in memory for CACHE_TTL.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass

from clockdisplay.adsb.photos import USER_AGENT
from clockdisplay.adsb.source import Aircraft

log = logging.getLogger(__name__)

DEFAULT_API = "https://adsb.im/api/0/routeset"
TIMEOUT = 6
CACHE_TTL = 3600
AIRLINE_CALLSIGN = re.compile(r"^[A-Z]{3}\d")


@dataclass(frozen=True)
class Airport:
    iata: str
    icao: str
    city: str

    @property
    def code(self) -> str:
        return self.iata or self.icao


@dataclass(frozen=True)
class Route:
    origin: Airport
    destination: Airport
    stops: tuple[Airport, ...] = ()  # intermediate airports on multi-leg flight numbers

    @property
    def codes(self) -> str:
        return " → ".join(a.code for a in (self.origin, *self.stops, self.destination))

    @property
    def cities(self) -> str:
        return " – ".join(a.city for a in (self.origin, *self.stops, self.destination) if a.city)


def _post(url: str, body: dict) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def parse_route(entry: dict) -> Route | None:
    if not entry.get("plausible"):
        return None
    airports = [Airport(str(a.get("iata") or ""), str(a.get("icao") or ""), str(a.get("location") or ""))
                for a in entry.get("_airports") or [] if isinstance(a, dict)]
    airports = [a for a in airports if a.code]
    if len(airports) < 2:
        return None
    return Route(airports[0], airports[-1], tuple(airports[1:-1]))


class RouteCache:
    def __init__(self, post=_post, clock=time.time):
        self._post, self.clock = post, clock
        self._cache: dict[str, tuple[float, Route | None]] = {}
        self._lock = threading.Lock()

    def lookup(self, ac: Aircraft, api: str = DEFAULT_API) -> Route | None:
        callsign = ac.callsign.upper()
        if not AIRLINE_CALLSIGN.match(callsign) or ac.lat is None or ac.lon is None:
            return None
        now = self.clock()
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_TTL}
            if callsign in self._cache:
                return self._cache[callsign][1]
        try:
            data = json.loads(self._post(api or DEFAULT_API,
                                         {"planes": [{"callsign": callsign, "lat": ac.lat, "lng": ac.lon}]}))
        except Exception as e:  # show the card without a route, retry next time
            log.warning("route lookup for %s failed: %s: %s", callsign, type(e).__name__, e)
            return None
        route = next((parse_route(e) for e in data if isinstance(e, dict)
                      and str(e.get("callsign", "")).upper() == callsign), None) \
            if isinstance(data, list) else None
        with self._lock:
            self._cache[callsign] = (now, route)
        return route
