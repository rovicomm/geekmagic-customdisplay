"""System-tray app: keeps the configured SmallTV displays showing live Claude usage.

Run with `clock-tray` (pythonw, no console) or `python -m clockdisplay.tray`.
Logs go to %LOCALAPPDATA%\\clockdisplay\\logs\\tray.log.
"""
from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

from clockdisplay import config
from clockdisplay.claude.auth import AuthError
from clockdisplay.claude.usage import RateLimited, Usage
from clockdisplay.device import UltraDevice
from clockdisplay.display import Display
from clockdisplay.meter import Meter, Target
from clockdisplay.render import canvas, frames, widgets

log = logging.getLogger("clockdisplay.tray")

APP_NAME = "ClockDisplay"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
GREY = (150, 150, 150)
TRACK = (70, 70, 70)
IDENTIFY_SECONDS = 6


# --- icon ----------------------------------------------------------------------

def render_icon(pct: float | None, active: bool = True, size: int = 64) -> Image.Image:
    """Ring gauge of the 5h percentage; grey when paused/erroring, empty when unknown."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    fill = canvas.level_color(pct) if active and pct is not None else GREY
    widgets.ring(img, (size // 2, size // 2), size // 2 - 4, size // 7, pct or 0, fill, track=TRACK)
    text = "?" if pct is None else f"{min(pct, 99):.0f}"
    widgets.text_box(img, text, (size // 4, size // 4, size * 3 // 4, size * 3 // 4),
                     fill=(255, 255, 255))
    return img


# --- autostart (HKCU Run key) --------------------------------------------------

def _launch_command() -> str:
    if getattr(sys, "frozen", False):  # portable ClockDisplay.exe
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else exe}" -m clockdisplay.tray'


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# --- text prompt ---------------------------------------------------------------

def ask_text(title: str, prompt: str, default: str = "") -> str | None:
    """Windows InputBox via PowerShell (keeps tkinter out of the exe). None if cancelled."""
    def q(v: str) -> str:
        return "'" + v.replace("'", "''") + "'"
    # Base64 the answer: PowerShell 5.1 ignores OutputEncoding when stdout is a pipe.
    script = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
              f"$r = [Microsoft.VisualBasic.Interaction]::InputBox({q(prompt)}, {q(title)}, {q(default)}); "
              "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($r))")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, timeout=600,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        log.exception("text prompt failed")
        return None
    text = base64.b64decode(out.stdout.strip() or b"").decode("utf-8", "replace").strip()
    return text or None  # InputBox returns "" on Cancel


# --- single instance -----------------------------------------------------------

_mutex = None


def _already_running() -> bool:
    """Hold a named mutex for the process lifetime; True if another instance owns it."""
    global _mutex
    if sys.platform != "win32":
        return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    _mutex = kernel32.CreateMutexW(None, False, "Local\\clockdisplay-tray")
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


# --- app -----------------------------------------------------------------------

class TrayApp:
    def __init__(self):
        self.meter = Meter()
        self.stop, self.wake = threading.Event(), threading.Event()
        self.error: str | None = None
        self.meter.sync_targets()
        self.icon = pystray.Icon(APP_NAME, render_icon(None), "Claude usage: starting…",
                                 pystray.Menu(self._items))

    def _items(self) -> list[pystray.MenuItem]:
        """Rebuilt each time the menu opens, so displays added to config.json show up."""
        item = pystray.MenuItem
        return [
            item(lambda _: self._status_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Refresh now", self._refresh, default=True),
            item("Pause all displays", self._toggle_pause, checked=lambda _: self.meter.paused),
            item("Restore all clock themes", self._restore_all),
            pystray.Menu.SEPARATOR,
            *(item(t.name, self._display_menu(t.name)) for t in self.meter.targets.values()),
            pystray.Menu.SEPARATOR,
            item("Edit settings", self._edit_settings),
            item("Open data folder", lambda: os.startfile(config.config_dir())),
            item("Start with Windows", self._toggle_autostart, checked=lambda _: autostart_enabled()),
            pystray.Menu.SEPARATOR,
            item("Quit", self._quit),
        ]

    def _display_menu(self, name: str) -> pystray.Menu:
        item = pystray.MenuItem

        def target() -> Target | None:
            return self.meter.targets.get(name)

        return pystray.Menu(
            item(lambda _: (t := target()) and f"{t.host} · {t.status}" or "removed", None, enabled=False),
            item("Identify (show name on screen)", lambda: self._identify(name)),
            item("Rename…", lambda: self._rename(name)),
            pystray.Menu.SEPARATOR,
            item("Pause updates", lambda: self._toggle_display_pause(name),
                 checked=lambda _: bool((t := target()) and t.paused)),
            item("Restore clock theme", lambda: self._restore(name)),
            item("Open web UI", lambda: (t := target()) and webbrowser.open(f"http://{t.host}")),
        )

    def _status_line(self) -> str:
        u = self.meter.last
        if self.error:
            return self.error
        if not u:
            return "Waiting for first update…"
        return f"5h {u.five_pct:.0f}% · 7d {u.week_pct:.0f}%"

    def _on_update(self, result: Usage | Exception) -> None:
        if isinstance(result, Usage):
            self.error = None
            u = result
            self.icon.icon = render_icon(u.five_pct, active=self._active())
            title = (f"Claude 5h {u.five_pct:.0f}% (resets {u.five_reset})\n"
                     f"7d {u.week_pct:.0f}% (resets {u.week_reset})")
            failing = sum(1 for t in self.meter.targets.values()
                          if t.app == "claude" and not t.paused and t.error)
            if failing:
                title += f"\n({failing} display{'s' if failing > 1 else ''} unreachable)"
            self.icon.title = title[:127]
        else:
            if isinstance(result, AuthError):
                self.error = "Sign-in needed: run `claude`"
            elif isinstance(result, RateLimited):
                self.error = f"Rate limited, retry in {result.retry_after}s"
            else:
                self.error = f"Error: {type(result).__name__}"
            last = self.meter.last
            self.icon.icon = render_icon(last.five_pct if last else None, active=False)
            self.icon.title = f"Claude usage: {self.error}"[:127]
        self.icon.update_menu()

    def _refresh(self) -> None:
        self.wake.set()

    def _active(self) -> bool:
        """False when nothing is being pushed, so the icon greys out."""
        return not self.meter.paused and any(
            t.app == "claude" and not t.paused for t in self.meter.targets.values())

    def _redraw_icon(self) -> None:
        if self.meter.last:
            self.icon.icon = render_icon(self.meter.last.five_pct, active=self._active())
        self.icon.update_menu()

    def _toggle_pause(self) -> None:
        self.meter.paused = not self.meter.paused
        self._redraw_icon()
        if not self.meter.paused:
            self.meter.invalidate()  # re-take the displays even if the numbers didn't change
            self.wake.set()

    def _toggle_display_pause(self, name: str) -> None:
        t = self.meter.targets.get(name)
        if not t:
            return
        t.paused = not t.paused
        self._redraw_icon()
        if not t.paused:
            self.meter.invalidate(name)
            self.wake.set()

    def _restore_device(self, t: Target) -> None:
        try:
            Display(UltraDevice(t.host)).restore()
        except Exception:
            log.exception("restore %s failed", t.name)

    def _restore(self, name: str) -> None:
        t = self.meter.targets.get(name)
        if t:
            t.paused = True
            self._restore_device(t)
            self._redraw_icon()

    def _restore_all(self) -> None:
        self.meter.paused = True  # "Pause all displays" unticks to resume them all
        for t in list(self.meter.targets.values()):
            self._restore_device(t)
        self._redraw_icon()

    def _identify(self, name: str) -> None:
        """Show the display's name for IDENTIFY_SECONDS, then put back what it was showing."""
        t = self.meter.targets.get(name)
        if not t or t.hold:
            return

        def run() -> None:
            t.hold = True
            try:
                Display(UltraDevice(t.host)).show(frames.identify(t.name, t.host), force=True)
                self.stop.wait(IDENTIFY_SECONDS)
            except Exception as e:
                log.warning("identify %s failed: %s", t.name, e)
                self.icon.notify(f"Couldn't reach {t.name} ({t.host})", APP_NAME)
                return
            finally:
                t.hold = False
            if t.app == "claude" and not t.paused and not self.meter.paused:
                self.meter.invalidate(t.name)  # the meter re-pushes the usage card
                self.wake.set()
            else:
                self._restore_device(t)

        threading.Thread(target=run, name="identify", daemon=True).start()

    def _rename(self, name: str) -> None:
        t = self.meter.targets.get(name)
        if not t:
            return

        def run() -> None:
            new = ask_text("Rename display", f"Name for the display at {t.host}:", name)
            if new is None or new.strip() in ("", name):
                return
            try:
                config.rename_display(name, new)
            except ValueError as e:
                self.icon.notify(str(e), APP_NAME)
                return
            self.meter.rename(name, new.strip())
            self.icon.update_menu()

        threading.Thread(target=run, name="rename", daemon=True).start()

    def _edit_settings(self) -> None:
        path = config.config_file()
        if not path.exists():
            config.save_config(config.load_config())
        os.startfile(path)

    def _toggle_autostart(self) -> None:
        set_autostart(not autostart_enabled())

    def _quit(self) -> None:
        self.stop.set()
        self.wake.set()
        self.icon.stop()

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        worker = threading.Thread(target=self.meter.run, name="meter", daemon=True,
                                  args=(self.stop, self._on_update, self.wake))
        worker.start()

    def run(self) -> None:
        self.icon.run(setup=self._setup)


def _setup_logging() -> None:
    handler = RotatingFileHandler(config.logs_dir() / "tray.log", maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def main() -> None:
    if _already_running():
        return
    if getattr(sys, "frozen", False):
        # Entry-point backend discovery is unreliable inside a PyInstaller bundle.
        import keyring
        from keyring.backends.Windows import WinVaultKeyring
        keyring.set_keyring(WinVaultKeyring())
    _setup_logging()
    log.info("starting (config dir %s)", config.config_dir())
    try:
        TrayApp().run()
    except Exception:
        log.exception("tray crashed")
        raise
    log.info("stopped")


if __name__ == "__main__":
    main()
