"""Push pipeline: put a frame or GIF on screen with as few device writes as possible.

Behaviour confirmed on Ultra-V9.0.45:
  * plain Pillow JPEGs display fine (no quantisation-table tricks needed);
  * overwriting the file currently on screen refreshes it without another /set?img;
  * animated 240x240 GIFs play in the Photo Album theme;
  * one upload takes ~0.4-0.8 s.
"""
from __future__ import annotations

import hashlib

from PIL import Image

from clockdisplay import config
from clockdisplay.device import OWN_PREFIX, PHOTO_ALBUM, UltraDevice
from clockdisplay.render.canvas import to_jpeg

STILL_SLOT = f"/image/{OWN_PREFIX}main.jpg"
ANIM_SLOT  = f"/image/{OWN_PREFIX}anim.gif"


class Display:
    def __init__(self, device: UltraDevice):
        self.device = device
        self.state = config.load_display_state(device.host)

    def show(self, content: Image.Image | bytes, force: bool = False) -> bool:
        """Show a PIL image (sent as JPEG) or raw GIF/JPEG bytes. Returns False if
        skipped because the same content is already on screen."""
        if isinstance(content, Image.Image):
            data, slot = to_jpeg(content), STILL_SLOT
        else:
            data = content
            slot = ANIM_SLOT if data[:3] == b"GIF" else STILL_SLOT

        self._take_over()
        digest = hashlib.sha1(data).hexdigest()
        if not force and self.state.get("showing") == slot and self.state.get("hash") == digest:
            return False

        directory, name = slot.rsplit("/", 1)
        self.device.upload(data, name, directory)
        if self.state.get("showing") != slot or force:
            self.device.show_image(slot)
        self.state.update(showing=slot, hash=digest)
        config.save_display_state(self.device.host, self.state)
        return True

    def _take_over(self) -> None:
        """Switch to Photo Album with autoplay off, remembering the theme to restore."""
        current = self.device.theme()
        if current != PHOTO_ALBUM:
            self.state.setdefault("previous_theme", current)
            self.device.set_album(autoplay=False)
            self.device.set_theme(PHOTO_ALBUM)
            self.state.pop("showing", None)  # force /set?img after a theme switch
        elif self.device.album().get("autoplay"):
            self.device.set_album(autoplay=False)

    def restore(self) -> int | None:
        """Return to the theme that was active before we took over."""
        previous = self.state.pop("previous_theme", None)
        if previous is not None:
            self.device.set_theme(previous)
        self.state.pop("showing", None)
        config.save_display_state(self.device.host, self.state)
        return previous

    def clean(self) -> list[str]:
        """Delete our own cm_* files from the device (never anyone else's)."""
        removed = []
        for directory in ("/image", "/gif"):
            for f in self.device.list_files(directory):
                if f.ours:
                    self.device.delete(f.path)
                    removed.append(f.path)
        self.state.pop("showing", None)
        config.save_display_state(self.device.host, self.state)
        return removed
