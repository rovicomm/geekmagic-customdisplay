"""Usage meter loop: fetch Claude usage -> render the usage card -> push to every display.

UI-agnostic so `clock watch` and the tray app share it. Settings (including the display
list) are re-read every tick, so edits to config.json apply without a restart. Usage is
fetched once per tick however many displays there are; pushes run in parallel so one
unreachable display doesn't hold up the rest.
"""
from __future__ import annotations

import datetime as dt
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
from clockdisplay.display import RESET_SLOT, Display
from clockdisplay.render import anim, frames

log = logging.getLogger(__name__)

MAX_BACKOFF = 600
FIRE_OFF = 0.5  # hysteresis: the fire goes out once the burn rate drops below half of fire_rate
RESET_SLACK = dt.timedelta(seconds=60)  # a window may reset this early (clock skew)
RESET_JUMP = dt.timedelta(hours=1)      # resets_at must move this far to count, not just jitter
RESET_WINDOWS = ("5h", "7d")


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
    popup: bool = True  # ADS-B planes may pop up over the usage card

    @property
    def status(self) -> str:
        if self.app not in config.APPS:
            return f"unknown app {self.app!r}"
        if self.app == "off":
            return "off"
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
        self.wake = threading.Event()  # set to make run() tick now
        self.stop = threading.Event()  # run()'s stop; cuts reset celebrations short
        self._stored: set[str] = set()  # hosts the reset GIFs have been sent to this session

    def sync_targets(self, cfg: dict | None = None) -> None:
        """Match self.targets to the configured displays, keeping pause state and push history."""
        cfg = cfg or config.load_config()
        targets = {}
        for d in cfg["displays"]:
            t = self.targets.get(d["name"]) or Target(d["name"], d["host"])
            if t.host != d["host"]:
                t.host, t.pushed_key, t.error, t.last_push = d["host"], None, None, None
            t.app = d["app"]
            t.popup = d.get("adsb_popup", True) is not False
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
        prev, usage = self.last, self.fetch()
        self.last = usage
        self._update_burn(usage.five_pct, cfg)
        if push is None:
            push = cfg["autopush"] and not self.paused
        resets = self._resets(prev, usage) if push and float(cfg["reset_seconds"]) > 0 else []
        if resets:
            log.info("usage reset: %s", ", ".join(resets))
            self.celebrate(resets, float(cfg["reset_seconds"]))  # the card follows on release
        elif push:
            key, now = self._key(usage), self.clock()
            due = [t for t in self.targets.values()
                   if t.app == "claude" and not t.paused and not t.hold
                   and (force or key != t.pushed_key or now - t.pushed_at >= cfg["force_push"])]
            if due:
                self._push_all(due, self.render(usage), force, key, now)
        if push and float(cfg["reset_seconds"]) > 0:
            self._store_reset_gifs()
        return usage

    def _store_reset_gifs(self) -> None:
        """Once per display per session, upload the reset GIFs in the background (if the
        device doesn't have them already) so a reset plays them without waiting on an upload."""
        for t in self.targets.values():
            if t.app != "claude" or t.paused or t.host in self._stored:
                continue
            self._stored.add(t.host)

            def run(host: str = t.host, name: str = t.name) -> None:
                try:
                    d = self.display_factory(host)
                    for w in RESET_WINDOWS:
                        if d.store(RESET_SLOT.format(w), anim.reset_gif(w)):
                            log.info("display %s: stored %s", name, RESET_SLOT.format(w))
                except Exception as e:  # tried again on the next reset, and next session
                    log.warning("display %s (%s): storing reset GIFs: %s: %s",
                                name, host, type(e).__name__, e)

            threading.Thread(target=run, name=f"store-{t.name}", daemon=True).start()

    def _key(self, usage: Usage) -> tuple:
        """What's on the card, as far as deciding whether it needs re-pushing goes."""
        return round(usage.five_pct), round(usage.week_pct), self.on_fire

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

    def _resets(self, prev: Usage | None, usage: Usage) -> list[str]:
        """Windows ("7d", "5h") whose known reset time has passed and moved on since `prev`."""
        if prev is None:
            return []
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
        out = []
        for window, old, new in (("7d", prev.week_resets_at, usage.week_resets_at),
                                 ("5h", prev.five_resets_at, usage.five_resets_at)):
            if old is not None and now >= old - RESET_SLACK and (new is None or new >= old + RESET_JUMP):
                out.append(window)
        return out

    def celebrate(self, windows: list[str], seconds: float) -> list[threading.Thread]:
        """Play each window's reset GIF for `seconds` on every active meter display in the
        background, then hand the display back (which puts the usage card up)."""
        # Kept on the device under their own names, so only the first reset ever uploads them.
        gifs = [(RESET_SLOT.format(w), anim.reset_gif(w)) for w in windows]

        def run(t: Target) -> None:
            try:
                if not self.hold(t, show=lambda d: d.show_file(*gifs[0])):
                    return
            except Exception as e:
                self._failed(t, e)
                return
            try:
                for gif in gifs[1:]:
                    self.stop.wait(seconds)
                    self.display_factory(t.host).show_file(*gif)
                self.stop.wait(seconds)
                t.error = None
            except Exception as e:
                self._failed(t, e)
            finally:
                try:
                    self.release(t)
                except Exception as e:
                    self._failed(t, e)

        threads = [threading.Thread(target=run, args=(t,), name=f"reset-{t.name}", daemon=True)
                   for t in self.targets.values() if t.app == "claude" and not t.paused and not t.hold]
        for th in threads:
            th.start()
        return threads

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

    def flash(self, t: Target, content, seconds: float, stop: threading.Event) -> None:
        """hold() for `seconds` (blocking), then release(). Raises if the display can't be
        reached. No-op if it's already held."""
        if not self.hold(t, content):
            return
        try:
            stop.wait(seconds)
        finally:
            self.release(t)

    def hold(self, t: Target, content=None, show: Callable[[Display], object] | None = None) -> bool:
        """Put `content` (or whatever `show(display)` puts up) on a display and keep the meter
        off it until release(). False if something else already holds it. Raises (and doesn't
        hold) if it can't be reached."""
        if t.hold:
            return False
        t.hold = True
        try:
            d = self.display_factory(t.host)
            if show:
                show(d)
            else:
                d.show(content, force=True)
        except BaseException:
            t.hold = False
            raise
        return True

    def release(self, t: Target) -> None:
        """Hand a held display back: the last usage card goes straight back up (no waiting
        for a fetch, which may be rate limited), or the clock theme is restored for other
        displays (an "adsb" display then shows its next plane)."""
        t.hold = False
        self.invalidate(t.name)
        if t.app == "claude" and not t.paused and not self.paused:
            if self.last is not None:
                self._push_all([t], self.render(self.last), True, self._key(self.last), self.clock())
                return
            self.wake.set()  # no usage yet: show the clock until the meter has some
        self.display_factory(t.host).restore()

    def _push_all(self, targets: list[Target], content, force: bool,
                  key: tuple | None = None, now: float | None = None) -> None:
        def one(t: Target) -> None:
            try:
                self.display_factory(t.host).show(content, force=force)
            except Exception as e:  # one bad display must not stop the others
                self._failed(t, e)
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

    def _failed(self, t: Target, e: Exception) -> None:
        t.error = "unreachable" if isinstance(e, (DeviceError, OSError)) else f"error: {type(e).__name__}"
        log.warning("display %s (%s): %s: %s", t.name, t.host, type(e).__name__, e)

    def run(self, stop: threading.Event, on_update: Callable[[Usage | Exception], None] = lambda _: None,
            wake: threading.Event | None = None) -> None:
        """Poll until `stop` is set. Setting `wake` triggers an immediate refresh."""
        wake = self.wake = wake or self.wake
        self.stop = stop
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
