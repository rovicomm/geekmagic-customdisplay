"""Plane spotting loop: poll the ADS-B receiver and put overhead aircraft on the displays.

Works alongside the usage Meter and shares its display targets:
  * "adsb" displays show the nearest aircraft that passes the filters, refreshed every
    `refresh` seconds, and go back to the clock theme when the sky is empty;
  * "claude" displays get a pop-up for each aircraft that comes within `popup_radius`
    (default: `radius`), then the usage card goes back up. The pop-up lasts
    `popup_seconds`, or with `popup_seconds` 0 until the plane leaves `popup_radius`
    (refreshed every `refresh` seconds meanwhile, capped at `popup_max`). The same
    aircraft doesn't pop up again for `cooldown` seconds. A plane that arrives while a
    pop-up is still showing gets its turn afterwards if it's still in range.
Settings live in the "adsb" block of config.json and are re-read every poll.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from clockdisplay import config
from clockdisplay.adsb import card, source
from clockdisplay.adsb.photos import PhotoCache
from clockdisplay.adsb.routes import RouteCache
from clockdisplay.adsb.source import Aircraft, SourceError
from clockdisplay.device import DeviceError
from clockdisplay.meter import Meter, Target

log = logging.getLogger(__name__)

MIN_POLL = 2
MAX_BACKOFF = 300
TRY_SECONDS = 30  # "Show nearest now" when pop-ups last until the plane leaves


@dataclass
class Popup:
    """An until-it-leaves pop-up holding one display."""
    hex: str
    started: float
    shown: float


class Spotter:
    def __init__(self, meter: Meter, fetch=source.fetch_aircraft, receiver=source.fetch_receiver,
                 photos: PhotoCache | None = None, routes: RouteCache | None = None,
                 clock: Callable[[], float] = time.time):
        self.meter, self.fetch, self.fetch_receiver, self.clock = meter, fetch, receiver, clock
        self.photos = photos or PhotoCache()
        self.routes = routes or RouteCache()
        self.settings: dict = dict(config.ADSB_DEFAULTS)
        self.aircraft: list[Aircraft] = []  # everything the receiver tracks
        self.nearby: list[Aircraft] = []    # passing the filters, nearest first
        self.popped: dict[str, float] = {}  # hex -> when it last popped up
        self.popups: dict[str, Popup] = {}  # display name -> its until-it-leaves pop-up
        self.error: str | None = None
        self.wake = threading.Event()
        self._receiver: tuple[str, tuple[float, float] | None] | None = None

    @property
    def popup_radius(self) -> float:
        return float(self.settings["popup_radius"] or 0) or float(self.settings["radius"])

    @property
    def until_gone(self) -> bool:
        """Pop-ups last until the plane leaves popup_radius rather than a fixed time."""
        return float(self.settings["popup_seconds"] or 0) <= 0

    @property
    def active(self) -> bool:
        return bool(self.settings["enabled"] and self.settings["url"])

    def status(self) -> str:
        if not self.settings["url"]:
            return "No receiver URL set"
        if not self.settings["enabled"]:
            return "Off"
        if self.error:
            return self.error
        if self.nearby:
            ac = self.nearby[0]
            return f"{len(self.nearby)} overhead · nearest {ac.name} {ac.distance:.1f} nm"
        return f"None overhead ({len(self.aircraft)} tracked)"

    def location(self) -> tuple[float, float]:
        s = self.settings
        if s["location"]:
            return float(s["location"][0]), float(s["location"][1])
        if not self._receiver or self._receiver[0] != s["url"]:
            self._receiver = (s["url"], self.fetch_receiver(s["url"]))
        if self._receiver[1] is None:
            raise SourceError('receiver has no position; set "location": [lat, lon] under "adsb"')
        return self._receiver[1]

    def card(self, ac: Aircraft):
        fields = [f for f in self.settings["fields"] if f in config.ADSB_FIELDS]
        photo = self.photos.lookup(ac.hex) if "photo" in fields else None
        route = self.routes.lookup(ac, self.settings["route_api"]) if "route" in fields else None
        return card.render(ac, fields, photo, route)

    def refresh(self, ignore_enabled: bool = False) -> None:
        """Re-read settings and fetch the aircraft list (no display changes)."""
        cfg = config.load_config()
        self.settings = cfg["adsb"]
        self.meter.sync_targets(cfg)
        if not (self.active or ignore_enabled and self.settings["url"]):
            self.aircraft, self.nearby = [], []
            return
        self.aircraft = self.fetch(self.settings["url"], self.location())
        self.nearby = source.overhead(self.aircraft, self.settings)

    def tick(self, stop: threading.Event) -> None:
        self.refresh()
        now = self.clock()
        targets = [t for t in self.meter.targets.values() if not t.paused and not self.meter.paused]
        self._update_adsb_displays([t for t in targets if t.app == "adsb"], now)
        self._track_popups(now)
        if self.active and self.settings["popup"]:
            self._popup([t for t in targets if t.app == "claude" and t.popup], now, stop)

    # --- "adsb" displays -----------------------------------------------------------

    def _update_adsb_displays(self, targets: list[Target], now: float) -> None:
        ac = self.nearby[0] if self.nearby else None
        content = None
        for t in targets:
            if t.hold:
                continue
            try:
                if ac:
                    key = ("adsb", ac.hex)
                    if key == t.pushed_key and now - t.pushed_at < float(self.settings["refresh"]):
                        continue
                    content = content or self.card(ac)
                    self.meter.display_factory(t.host).show(content)
                    t.pushed_key, t.pushed_at, t.last_push = key, now, now
                elif t.pushed_key is not None:  # sky's empty: back to the clock
                    self.meter.display_factory(t.host).restore()
                    t.pushed_key = None
                t.error = None
            except Exception as e:
                self._display_failed(t, e)

    # --- pop-ups over the usage card -----------------------------------------------

    def _next_plane(self, now: float) -> Aircraft | None:
        """The nearest plane within popup_radius that hasn't popped up within the cooldown."""
        cooldown = float(self.settings["cooldown"])
        self.popped = {h: at for h, at in self.popped.items() if now - at < cooldown}
        candidates = source.overhead(self.aircraft, {**self.settings, "radius": self.popup_radius})
        return next((ac for ac in candidates if ac.hex not in self.popped), None)

    def _mark_popped(self, ac: Aircraft, now: float) -> None:
        self.popped[ac.hex] = now
        log.info("overhead: %s %s %.1f nm %s", ac.name, ac.type_code, ac.distance, card.altitude_text(ac))

    def _popup(self, targets: list[Target], now: float, stop: threading.Event) -> None:
        ready = [t for t in targets if not t.hold]
        ac = self._next_plane(now) if ready else None
        if ac is None:
            return  # a busy display means the plane waits for the next poll
        self._mark_popped(ac, now)
        if not self.until_gone:
            self.flash(ready, self.card(ac), float(self.settings["popup_seconds"]), stop)
            return
        content = self.card(ac)
        for t in ready:
            try:
                if self.meter.hold(t, content):
                    self.popups[t.name] = Popup(ac.hex, now, now)
                    t.error = None
            except Exception as e:
                self._display_failed(t, e)

    def _track_popups(self, now: float) -> None:
        """Clear until-it-leaves pop-ups whose plane has left popup_radius (or the receiver
        lost it, or popup_max ran out); refresh the card on the rest."""
        by_hex = {ac.hex: ac for ac in self.aircraft}
        swapped: dict[str, tuple[Aircraft, object]] = {}  # old hex -> the plane replacing it
        for name, p in list(self.popups.items()):
            t = self.meter.targets.get(name)
            ac = by_hex.get(p.hex)
            if t is None:
                del self.popups[name]
                continue
            if (not self.until_gone or not (self.active and self.settings["popup"] and t.popup)
                    or t.paused or self.meter.paused):
                self._release(name)
                continue
            gone = ac is None or ac.distance is None or ac.distance > self.popup_radius
            if gone or now - p.started >= float(self.settings["popup_max"]):
                # Straight on to the next plane if one is waiting, rather than flashing the
                # usage card up for a moment in between.
                if p.hex not in swapped:
                    nxt = self._next_plane(now)
                    if nxt:
                        self._mark_popped(nxt, now)
                    swapped[p.hex] = (nxt, self.card(nxt) if nxt else None)
                nxt, content = swapped[p.hex]
                if nxt is None:
                    self._release(name)
                    continue
                try:
                    self.meter.display_factory(t.host).show(content, force=True)
                    self.popups[name] = Popup(nxt.hex, now, now)
                except Exception as e:
                    self._display_failed(t, e)
                    self._release(name)
            elif now - p.shown >= float(self.settings["refresh"]):
                try:
                    self.meter.display_factory(t.host).show(self.card(ac))
                    p.shown = now
                except Exception as e:
                    self._display_failed(t, e)

    def _release(self, name: str) -> None:
        self.popups.pop(name, None)
        t = self.meter.targets.get(name)
        if t is None:
            return
        try:
            self.meter.release(t)
        except Exception as e:
            self._display_failed(t, e)

    def release_all(self) -> None:
        """Hand back every display an until-it-leaves pop-up is holding."""
        for name in list(self.popups):
            self._release(name)

    def flash(self, targets: list[Target], content, seconds: float, stop: threading.Event) -> None:
        """Show content on each display for `seconds` in the background, then give it back."""
        def run(t: Target) -> None:
            try:
                self.meter.flash(t, content, seconds, stop)
                t.error = None
            except Exception as e:
                self._display_failed(t, e)

        for t in targets:
            threading.Thread(target=run, args=(t,), name=f"popup-{t.name}", daemon=True).start()

    def show_nearest(self, stop: threading.Event) -> Aircraft | None:
        """Pop the nearest aircraft up on every active display now, ignoring the filters
        (except position) and the cooldown. For trying out the card and settings."""
        self.refresh()
        if not self.active:
            return None
        candidates = self.nearby or sorted((ac for ac in self.aircraft if ac.distance is not None),
                                           key=lambda ac: ac.distance)
        if not candidates:
            return None
        ac = candidates[0]
        targets = [t for t in self.meter.targets.values()
                   if t.app in ("claude", "adsb") and not t.paused and not t.hold]
        self.flash(targets, self.card(ac), float(self.settings["popup_seconds"]) or TRY_SECONDS, stop)
        return ac

    def _display_failed(self, t: Target, e: Exception) -> None:
        t.error = "unreachable" if isinstance(e, (DeviceError, OSError)) else f"error: {type(e).__name__}"
        log.warning("display %s (%s): %s: %s", t.name, t.host, type(e).__name__, e)

    # --- loop ------------------------------------------------------------------------

    def run(self, stop: threading.Event, on_update: Callable[[], None] = lambda: None) -> None:
        """Poll until `stop` is set. Setting `self.wake` polls immediately."""
        failures = 0
        while not stop.is_set():
            interval = max(MIN_POLL, float(self.settings["poll_interval"]))
            try:
                self.tick(stop)
                self.error, failures, delay = None, 0, interval
            except Exception as e:  # keep spotting through receiver/network hiccups
                self.release_all()  # can't tell whether those planes have gone
                failures += 1
                delay = min(interval * 2 ** (failures - 1), MAX_BACKOFF)
                self.error = "Receiver unreachable" if isinstance(e, SourceError) else f"Error: {type(e).__name__}"
                log.warning("adsb: %s: %s (retry in %.0fs)", type(e).__name__, e, delay)
            on_update()
            self.wake.wait(delay)
            self.wake.clear()
        self.release_all()
