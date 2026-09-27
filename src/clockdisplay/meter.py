"""Usage meter loop: fetch Claude usage -> render the usage card -> push to the display.

UI-agnostic so `clock watch` and the tray app share it. Settings are re-read every tick,
so edits to config.json apply without a restart.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

from clockdisplay import config
from clockdisplay.claude.usage import RateLimited, Usage, fetch_usage
from clockdisplay.device import UltraDevice
from clockdisplay.display import Display
from clockdisplay.render import anim, frames

log = logging.getLogger(__name__)

MAX_BACKOFF = 600
FIRE_OFF = 0.5  # hysteresis: the fire goes out once the burn rate drops below half of fire_rate


def _default_display() -> Display:
    return Display(UltraDevice(config.resolve_host()))


class Meter:
    def __init__(self, fetch: Callable[[], Usage] = fetch_usage,
                 display_factory: Callable[[], Display] = _default_display,
                 clock: Callable[[], float] = time.time):
        self.fetch, self.display_factory, self.clock = fetch, display_factory, clock
        self.paused = False
        self.last: Usage | None = None
        self._pushed_key: tuple | None = None
        self._pushed_at = 0.0
        self._samples: deque[tuple[float, float]] = deque()  # (time, 5h %)
        self.burn_rate: float | None = None  # 5h %/hour, None until there's enough history
        self.on_fire = False

    def tick(self, push: bool | None = None, force: bool = False) -> Usage:
        """Fetch once and push if the rounded numbers changed or force_push has elapsed."""
        cfg = config.load_config()
        usage = self.fetch()
        self.last = usage
        self._update_burn(usage.five_pct, cfg)
        if push is None:
            push = cfg["autopush"] and not self.paused
        if push:
            key = (round(usage.five_pct), round(usage.week_pct), self.on_fire)
            now = self.clock()
            if force or key != self._pushed_key or now - self._pushed_at >= cfg["force_push"]:
                self.push(usage, force=force)
                self._pushed_key, self._pushed_at = key, now
        return usage

    def _update_burn(self, pct: float, cfg: dict) -> None:
        """Track the 5h %/hour over the last fire_window seconds and light or put out the fire."""
        now, window = self.clock(), float(cfg["fire_window"])
        if self._samples and pct < self._samples[-1][1] - 1:
            self._samples.clear()  # the 5h window reset
        self._samples.append((now, pct))
        while now - self._samples[0][0] > window:
            self._samples.popleft()
        t0, p0 = self._samples[0]
        # Need half a window of history so one early 1% step doesn't read as a huge rate.
        self.burn_rate = (pct - p0) / (now - t0) * 3600 if now - t0 >= window / 2 else None
        threshold = float(cfg["fire_rate"])
        if threshold <= 0 or len(self._samples) == 1:  # disabled, or history just (re)started
            self.on_fire = False
        elif self.burn_rate is None:
            pass  # not enough history yet: keep the current state
        elif self.burn_rate >= threshold:
            self.on_fire = True
        elif self.burn_rate < threshold * FIRE_OFF:
            self.on_fire = False

    def invalidate(self) -> None:
        """Make the next tick push regardless of whether the numbers changed."""
        self._pushed_key = None

    def push(self, usage: Usage, force: bool = False) -> bool:
        make = anim.dual_meter_fire if self.on_fire else frames.dual_meter
        content = make(usage.five_pct, usage.five_reset, usage.week_pct, usage.week_reset)
        return self.display_factory().show(content, force=force)

    def run(self, stop: threading.Event, on_update: Callable[[Usage | Exception], None] = lambda _: None,
            wake: threading.Event | None = None) -> None:
        """Poll until `stop` is set. Setting `wake` triggers an immediate refresh."""
        wake = wake or threading.Event()
        failures = 0
        while not stop.is_set():
            interval = int(config.load_config()["poll_interval"])
            try:
                usage = self.tick()
                failures = 0
                log.info("5h %.0f%%  7d %.0f%%  burn %s%s", usage.five_pct, usage.week_pct,
                         "n/a" if self.burn_rate is None else f"{self.burn_rate:.0f}%/h",
                         "  (on fire)" if self.on_fire else "")
                on_update(usage)
                delay = interval
            except RateLimited as e:
                delay = max(e.retry_after, interval)
                log.warning("rate limited, retrying in %ss", delay)
                on_update(e)
            except Exception as e:  # keep the loop alive through network/device hiccups
                failures += 1
                delay = min(interval * 2 ** (failures - 1), MAX_BACKOFF)
                log.warning("%s: %s (retry in %ss)", type(e).__name__, e, delay)
                on_update(e)
            wake.wait(delay)
            wake.clear()
