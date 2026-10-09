"""An in-memory stand-in for UltraDevice, so a desktop window can be one more display.

Display, Meter and Spotter drive it exactly like a SmallTV: files are "uploaded" into a
dict, and whatever the device would show (an image in Photo Album, or the clock theme) is
handed to a listener, which the desktop window draws.
"""
from __future__ import annotations

import threading
from typing import Callable

from clockdisplay.config import WINDOW_HOST
from clockdisplay.device import OWN_PREFIX, PHOTO_ALBUM, THEMES, RemoteFile

CLOCK_THEME = 1
SPACE = 64 * 1024 * 1024

Listener = Callable[[bytes | None], None]  # image bytes, or None for the clock theme


class VirtualDevice:
    def __init__(self, host: str = WINDOW_HOST):
        self.host = host
        self.files: dict[str, bytes] = {}
        self.listener: Listener = lambda _: None
        self._theme = CLOCK_THEME
        self._autoplay = False
        self._showing: str | None = None
        self._lock = threading.Lock()

    def current(self) -> bytes | None:
        """What the screen shows now: image bytes, or None for the clock."""
        with self._lock:
            if self._theme != PHOTO_ALBUM or self._showing is None:
                return None
            return self.files.get(self._showing)

    def _changed(self) -> None:
        self.listener(self.current())

    # --- the UltraDevice subset Display uses ---------------------------------

    def space(self) -> dict:
        used = sum(len(d) for d in self.files.values())
        return {"total": SPACE, "free": SPACE - used}

    def theme(self) -> int:
        return self._theme

    def set_theme(self, theme: int) -> None:
        if theme not in THEMES:
            raise ValueError(f"theme must be one of {sorted(THEMES)}")
        self._theme = theme
        self._changed()

    def album(self) -> dict:
        return {"autoplay": self._autoplay}

    def set_album(self, autoplay: bool, interval: int = 5) -> None:
        self._autoplay = autoplay

    def list_files(self, directory: str = "/image") -> list[RemoteFile]:
        prefix = "/" + directory.strip("/") + "/"
        return [RemoteFile(p, (len(d) + 1023) // 1024) for p, d in self.files.items()
                if p.startswith(prefix)]

    def upload(self, data: bytes, name: str, directory: str = "/image/") -> None:
        path = "/" + directory.strip("/") + "/" + name
        with self._lock:
            self.files[path] = data
            on_screen = path == self._showing
        if on_screen:  # like the device: overwriting the file on screen refreshes it
            self._changed()

    def show_image(self, path: str) -> None:
        with self._lock:
            self._showing = path
        self._changed()

    def delete(self, path: str) -> None:
        if not path.rsplit("/", 1)[-1].startswith(OWN_PREFIX):
            raise PermissionError(f"refusing to delete {path}: not a {OWN_PREFIX}* file")
        with self._lock:
            self.files.pop(path, None)


_device: VirtualDevice | None = None
_device_lock = threading.Lock()


def window_device() -> VirtualDevice:
    """The one VirtualDevice behind the desktop window, shared by every Display for it."""
    global _device
    with _device_lock:
        if _device is None:
            _device = VirtualDevice()
        return _device
