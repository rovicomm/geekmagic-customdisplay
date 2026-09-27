"""Usage meter loop: fetch Claude usage -> render the usage card -> push to every display.

UI-agnostic so `clock watch` and the tray app share it. Settings (including the display
list) are re-read every tick, so edits to config.json apply without a restart. Usage is
fetched once per tick however many displays there are; pushes run in parallel so one
unreachable display doesn't hold up the rest.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from clockdisplay import config
from clockdisplay.claude.usage import RateLimited, Usage, fetch_usage
from clockdisplay.device import DeviceError, UltraDevice
from clockdisplay.display import Display
from clockdisplay.render import anim, frames

log = logging.getLogger(__name__)

MAX_BACKOFF = 600
FIRE_OFF = 0.5  # hysteresis: the fire goes out once the burn rate drops below half of fire_rate


def _default_display(host: str) -> Display:
    return Display(UltraDevice(host))


@dataclass
class Target:
    """One configured display and its push bookkeeping."""
    name: str
    host: str
    app: str = "claude"
    paused: bool = False
    hold: bool = False  # something else (e.g. Identify) is on screen; don't push over it
    error: str | None = None
    last_push: float | None = None
    pushed_key: tuple | None = None
    pushed_at: float = 0.0

    @property
    def status(self) -> str:
        if self.app != "claude":
            return self.app if self.app in config.APPS else f"unknown app {self.app!r}"
        if self.paused:
            return "paused"
        return self.error or ("ok" if self.last_push is not None else "waiting")


class Meter:
    def __init__(self, fetch: Callable[[], Usage] = fetch_usage,
                 display_factory: Callable[[str], Display] = _default_display,
                 clock: Callable[[], float] = time.time):
        self.fetch, self.display_factory, self.clock = fetch, display_factory, clock
        self.paused = False
        self.last: Usage | None = None
        self.targets: dict[str, Target] = {}
        self._samples: deque[tuple[float, float]] = deque()  # (time, 5h %)
        self.burn_rate: float | None = None  # 5h %/hour, None until there's enough history
        self.on_fire = False
        self._warned_apps: set[str] = set()

    def sync_targets(self, cfg: dict | None = None) -> None:
        """Match self.targets to the configured displays, keeping pause state and push history."""
        cfg = cfg or config.load_config()
        targets = {}
        for d in cfg["displays"]:
            t = self.targets.get(d["name"]) or Target(d["name"], d["host"])
            if t.host != d["host"]:
                t.host, t.pushed_key, t.error, t.last_push = d["host"], None, None, None
            t.app = d["app"]
            if t.app not in config.APPS and t.app not in self._warned_apps:
                self._warned_apps.add(t.app)
                log.warning("display %s: unknown app %r, leaving it alone", t.name, t.app)
            targets[t.name] = t
        self.targets = targets

    def tick(self, push: bool | None = None, force: bool = False) -> Usage:
        """Fetch once and push to each display whose rounded numbers changed or whose
        force_push interval has elapsed."""
        cfg = config.load_config()
        self.sync_targets(cfg)
        usage = self.fetch()
        self.last = usage
        self._update_burn(usage.five_pct, cfg)
        if push is None:
            push = cfg["autopush"] and not self.paused
        if push:
            key = (round(usage.five_pct), round(usage.week_pct), self.on_fire)
            now = self.clock()
            due = [t for t in self.targets.values()
                   if t.app == "claude" and not t.paused and not t.hold
                   and (force or key != t.pushed_key or now - t.pushed_at >= cfg["force_push"])]
            if due:
                self._push_all(due, self.render(usage), force, key, now)
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

    def rename(self, old: str, new: str) -> None:
        """Carry a display's pause state and push history over to its new name."""
        if old in self.targets and new not in self.targets:
            t = self.targets.pop(old)
            t.name = new
            self.targets[new] = t

    def invalidate(self, name: str | None = None) -> None:
        """Make the next tick push to `name` (or every display) even if the numbers didn't change."""
        for t in self.targets.values():
            if name is None or t.name == name:
                t.pushed_key = None

    def render(self, usage: Usage) -> object:
        make = anim.dual_meter_fire if self.on_fire else frames.dual_meter
        return make(usage.five_pct, usage.five_reset, usage.week_pct, usage.week_reset)

    def push(self, usage: Usage, force: bool = False) -> None:
        """Push the usage card to every active meter display now."""
        targets = [t for t in self.targets.values() if t.app == "claude" and not t.paused and not t.hold]
        self._push_all(targets, self.render(usage), force)

    def _push_all(self, targets: list[Target], content, force: bool,
                  key: tuple | None = None, now: float | None = None) -> None:
        def one(t: Target) -> None:
            try:
                self.display_factory(t.host).show(content, force=force)
            except Exception as e:  # one bad display must not stop the others
                t.error = "unreachable" if isinstance(e, (DeviceError, OSError)) \
                    else f"error: {type(e).__name__}"
                log.warning("display %s (%s): %s: %s", t.name, t.host, type(e).__name__, e)
                return
            t.error, t.last_push = None, self.clock()
            if key is not None:
                t.pushed_key, t.pushed_at = key, now

        if len(targets) <= 1:
            for t in targets:
                one(t)
            return
        with ThreadPoolExecutor(max_workers=min(len(targets), 8), thread_name_prefix="push") as pool:
            list(pool.map(one, targets))

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
