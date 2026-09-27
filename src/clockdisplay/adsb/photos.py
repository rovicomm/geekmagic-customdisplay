"""Aircraft photos from planespotters.net's public API, looked up by ICAO hex.

Their terms ask for the photographer to be credited wherever a photo is shown, so the
card prints it, and for server clients to send a User-Agent with a contact URL.

Photos are cached on disk in <config dir>/photos: `<hex>.jpg` plus `<hex>.json` holding
the photographer, or just a `<hex>.json` saying there's no photo. A found photo is kept
until the cache grows past MAX_PHOTOS (oldest used go first); "no photo" is re-checked
after MISS_TTL, since someone may have photographed the aircraft since. Network errors
aren't cached at all. A small in-memory LRU sits in front of the disk cache.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from clockdisplay import __version__, config

log = logging.getLogger(__name__)

API = "https://api.planespotters.net/pub/photos/hex/{hex}"
USER_AGENT = f"clockdisplay/{__version__} (+https://github.com/rovicomm/geekmagic-customdisplay)"
TIMEOUT = 8
MEMORY_SIZE = 50
MAX_PHOTOS = 2000             # ~30 KB each
MISS_TTL = 7 * 24 * 3600      # seconds before "no photo" is looked up again


@dataclass(frozen=True)
class Photo:
    image: Image.Image
    photographer: str


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def _decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


class PhotoCache:
    def __init__(self, get=_get, directory: Path | None = None, clock=time.time):
        self._get, self._dir, self.clock = get, directory, clock
        self._memory: OrderedDict[str, Photo | None] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        return self._dir or config.config_dir() / "photos"

    def lookup(self, hex_code: str) -> Photo | None:
        hex_code = hex_code.lower()
        with self._lock:
            if hex_code in self._memory:
                self._memory.move_to_end(hex_code)
                return self._memory[hex_code]
        found, photo = self._load(hex_code)
        if not found:
            try:
                photo, jpeg = self._fetch(hex_code)
            except Exception as e:  # network trouble: show the card without a photo, retry next time
                log.warning("photo lookup for %s failed: %s: %s", hex_code, type(e).__name__, e)
                return None
            self._store(hex_code, photo, jpeg)
        with self._lock:
            self._memory[hex_code] = photo
            while len(self._memory) > MEMORY_SIZE:
                self._memory.popitem(last=False)
        return photo

    # --- disk ------------------------------------------------------------------------

    def _load(self, hex_code: str) -> tuple[bool, Photo | None]:
        """(found in cache, photo). A stale or unreadable entry counts as not found."""
        meta_path, jpg = self.directory / f"{hex_code}.json", self.directory / f"{hex_code}.jpg"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not meta.get("photo"):
                return self.clock() - float(meta.get("checked", 0)) < MISS_TTL, None
            photo = Photo(_decode(jpg.read_bytes()), str(meta.get("photographer") or ""))
            os.utime(jpg)  # mark as recently used for pruning
            return True, photo
        except (OSError, ValueError):
            return False, None

    def _store(self, hex_code: str, photo: Photo | None, jpeg: bytes | None) -> None:
        d = self.directory
        try:
            d.mkdir(parents=True, exist_ok=True)
            if photo and jpeg:
                (d / f"{hex_code}.jpg").write_bytes(jpeg)
                meta = {"photo": True, "photographer": photo.photographer}
            else:
                meta = {"photo": False}
            meta["checked"] = self.clock()
            (d / f"{hex_code}.json").write_text(json.dumps(meta), encoding="utf-8")
            if photo:
                self._prune()
        except OSError as e:
            log.warning("couldn't cache photo for %s: %s", hex_code, e)

    def _prune(self) -> None:
        photos = list(self.directory.glob("*.jpg"))
        if len(photos) <= MAX_PHOTOS:
            return
        photos.sort(key=lambda p: p.stat().st_mtime)
        for p in photos[:len(photos) - MAX_PHOTOS]:
            p.unlink(missing_ok=True)
            p.with_suffix(".json").unlink(missing_ok=True)

    # --- network ---------------------------------------------------------------------

    def _fetch(self, hex_code: str) -> tuple[Photo | None, bytes | None]:
        data = json.loads(self._get(API.format(hex=hex_code)))
        for p in data.get("photos") or []:
            src = (p.get("thumbnail_large") or p.get("thumbnail") or {}).get("src")
            if src:
                jpeg = self._get(src)
                return Photo(_decode(jpeg), str(p.get("photographer") or "").strip()), jpeg
        return None, None
