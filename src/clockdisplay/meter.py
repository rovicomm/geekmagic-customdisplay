"""Usage meter loop: fetch Claude usage -> render the usage card -> push to the display.

UI-agnostic so `clock watch` and the tray app share it. Settings are re-read every tick,
so edits to config.json apply without a restart.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from clockdisplay import config
from clockdisplay.claude.usage import RateLimited, Usage, fetch_usage
from clockdisplay.device import UltraDevice
from clockdisplay.display import Display
from clockdisplay.render import frames

log = logging.getLogger(__name__)

MAX_BACKOFF = 600


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

    def tick(self, push: bool | None = None, force: bool = False) -> Usage:
        """Fetch once and push if the rounded numbers changed or force_push has elapsed."""
        cfg = config.load_config()
        usage = self.fetch()
        self.last = usage
        if push is None:
            push = cfg["autopush"] and not self.paused
        if push:
            key = (round(usage.five_pct), round(usage.week_pct))
            now = self.clock()
            if force or key != self._pushed_key or now - self._pushed_at >= cfg["force_push"]:
                self.push(usage, force=force)
                self._pushed_key, self._pushed_at = key, now
        return usage

    def invalidate(self) -> None:
        """Make the next tick push regardless of whether the numbers changed."""
        self._pushed_key = None

    def push(self, usage: Usage, force: bool = False) -> bool:
        img = frames.dual_meter(usage.five_pct, usage.five_reset, usage.week_pct, usage.week_reset)
        return self.display_factory().show(img, force=force)

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
                log.info("5h %.0f%%  7d %.0f%%", usage.five_pct, usage.week_pct)
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
