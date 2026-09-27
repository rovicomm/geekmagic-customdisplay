"""Plane spotting loop: poll the ADS-B receiver and put overhead aircraft on the displays.

Works alongside the usage Meter and shares its display targets:
  * "adsb" displays show the nearest aircraft that passes the filters, refreshed every
    `refresh` seconds, and go back to the clock theme when the sky is empty;
  * "claude" displays get a pop-up for each newly arrived aircraft that lasts
    `popup_seconds`, then the meter re-pushes the usage card. The same aircraft doesn't
    pop up again for `cooldown` seconds. A plane that arrives while a pop-up is still
    showing gets its turn afterwards if it's still overhead.
Settings live in the "adsb" block of config.json and are re-read every poll.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from clockdisplay import config
from clockdisplay.adsb import card, source
from clockdisplay.adsb.photos import PhotoCache
from clockdisplay.adsb.source import Aircraft, SourceError
from clockdisplay.device import DeviceError
from clockdisplay.meter import Meter, Target

log = logging.getLogger(__name__)

MIN_POLL = 2
MAX_BACKOFF = 300


class Spotter:
    def __init__(self, meter: Meter, fetch=source.fetch_aircraft, receiver=source.fetch_receiver,
                 photos: PhotoCache | None = None, clock: Callable[[], float] = time.time):
        self.meter, self.fetch, self.fetch_receiver, self.clock = meter, fetch, receiver, clock
        self.photos = photos or PhotoCache()
        self.settings: dict = dict(config.ADSB_DEFAULTS)
        self.aircraft: list[Aircraft] = []  # everything the receiver tracks
        self.nearby: list[Aircraft] = []    # passing the filters, nearest first
        self.popped: dict[str, float] = {}  # hex -> when it last popped up
        self.error: str | None = None
        self.wake = threading.Event()
        self._receiver: tuple[str, tuple[float, float] | None] | None = None

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
        return card.render(ac, fields, photo)

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

    def _popup(self, targets: list[Target], now: float, stop: threading.Event) -> None:
        cooldown = float(self.settings["cooldown"])
        self.popped = {h: at for h, at in self.popped.items() if now - at < cooldown}
        fresh = [ac for ac in self.nearby if ac.hex not in self.popped]
        ready = [t for t in targets if not t.hold]
        if not fresh or not ready:
            return  # a busy display means the plane waits for the next poll
        ac = fresh[0]
        self.popped[ac.hex] = now
        log.info("overhead: %s %s %.1f nm %s", ac.name, ac.type_code, ac.distance, card.altitude_text(ac))
        self.flash(ready, self.card(ac), float(self.settings["popup_seconds"]), stop)

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
        self.flash(targets, self.card(ac), float(self.settings["popup_seconds"]), stop)
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
                failures += 1
                delay = min(interval * 2 ** (failures - 1), MAX_BACKOFF)
                self.error = "Receiver unreachable" if isinstance(e, SourceError) else f"Error: {type(e).__name__}"
                log.warning("adsb: %s: %s (retry in %.0fs)", type(e).__name__, e, delay)
            on_update()
            self.wake.wait(delay)
            self.wake.clear()
