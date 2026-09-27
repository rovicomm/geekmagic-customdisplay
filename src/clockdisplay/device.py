"""HTTP client for the GeekMagic SmallTV-Ultra stock firmware (tested on Ultra-V9.0.45).

The API was reverse-engineered from the device's own web UI; see docs/ultra-api.md.

Firmware quirk: upload responses carry two `Content-Length` headers with
different values, which urllib3/requests reject outright. Uploads therefore
go through http.client, which tolerates it. GET endpoints are well-behaved.

Safety: this client only deletes files whose name starts with OWN_PREFIX, and
does not wrap factory reset / reboot / clear-directory at all.
"""
from __future__ import annotations

import html
import http.client
import json
import re
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

OWN_PREFIX = "cm_"
MAX_UPLOAD = 1024 * 1024  # the web UI's own limit

THEMES = {
    1: "Weather Clock Today",
    2: "Weather Forecast",
    3: "Photo Album",
    4: "Time Style 1",
    5: "Time Style 2",
    6: "Time Style 3",
    7: "Simple Weather Clock",
}
PHOTO_ALBUM = 3


class DeviceError(RuntimeError):
    pass


@dataclass
class RemoteFile:
    path: str
    size_kb: int

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def ours(self) -> bool:
        return self.name.startswith(OWN_PREFIX)


class UltraDevice:
    def __init__(self, host: str, timeout: float = 10.0):
        self.host = host.removeprefix("http://").removeprefix("https://").rstrip("/")
        self.timeout = timeout

    # --- transport -------------------------------------------------------

    def _get(self, path: str, **params) -> str:
        url = f"http://{self.host}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        last: Exception | None = None
        for _ in range(2):  # one retry: the ESP web server occasionally drops a connection
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except (ConnectionError, http.client.RemoteDisconnected) as e:
                last = e
            except OSError as e:
                raise DeviceError(f"GET {url} failed: {e}") from e
        raise DeviceError(f"GET {url} failed: {last}")

    def _json(self, path: str) -> dict:
        text = self._get(path)
        try:
            return json.loads(text)
        except ValueError as e:
            raise DeviceError(f"{path} returned non-JSON: {text[:80]!r}") from e

    def _set(self, **params) -> None:
        text = self._get("/set", **params)
        if text.strip() != "OK":
            raise DeviceError(f"/set {params} returned {text[:80]!r}")

    # --- info ------------------------------------------------------------

    def info(self) -> dict:
        v = self._json("/v.json")
        return {
            "model": v.get("m"),
            "version": v.get("v"),
            "theme": self.theme(),
            "brightness": self.brightness(),
            **self.space(),
        }

    def space(self) -> dict:
        s = self._json("/space.json")
        return {"total": int(s["total"]), "free": int(s["free"])}

    # --- theme / brightness / album -------------------------------------

    def theme(self) -> int:
        return int(self._json("/app.json")["theme"])

    def set_theme(self, theme: int) -> None:
        if theme not in THEMES:
            raise ValueError(f"theme must be one of {sorted(THEMES)}")
        self._set(theme=theme)

    def theme_cycle(self) -> dict:
        return self._json("/theme_list.json")

    def set_theme_cycle(self, themes: list[int], enabled: bool, interval: int) -> None:
        flags = ",".join("1" if t in themes else "0" for t in sorted(THEMES))
        self._set(theme_list=flags, sw_en=int(enabled), theme_interval=interval)

    def brightness(self) -> int:
        return int(self._json("/brt.json")["brt"])

    def set_brightness(self, value: int) -> None:
        if not 0 <= value <= 100:
            raise ValueError("brightness must be 0-100")
        self._set(brt=value)

    def set_night_mode(self, enabled: bool, start_hour: int = 22, end_hour: int = 7,
                       brightness: int = 10) -> None:
        self._set(t1=start_hour, t2=end_hour, b1=50, b2=brightness, en=int(enabled))

    def album(self) -> dict:
        return self._json("/album.json")

    def set_album(self, autoplay: bool, interval: int = 5) -> None:
        self._set(i_i=interval, autoplay=int(autoplay))

    # --- clock appearance -------------------------------------------------

    def set_clock_colors(self, hour: str, minute: str, second: str) -> None:
        self._set(hc=hour, mc=minute, sc=second)

    def set_12h(self, enabled: bool) -> None:
        self._set(hour=int(enabled))

    def set_clock_font(self, font: int) -> None:
        """1 = default big font, 2 = digital font."""
        self._set(font=font)

    def set_colon_blink(self, enabled: bool) -> None:
        self._set(colon=int(enabled))

    def set_date_format(self, fmt: int) -> None:
        """1 DD/MM/YYYY, 2 YYYY/MM/DD, 3 MM/DD/YYYY, 4 MM/DD, 5 DD/MM."""
        self._set(day=fmt)

    # --- files -----------------------------------------------------------

    def list_files(self, directory: str = "/image") -> list[RemoteFile]:
        return parse_filelist(self._get("/filelist", dir=directory))

    def upload(self, data: bytes, name: str, directory: str = "/image/") -> None:
        """Upload (or overwrite) a file. Overwriting the file currently on
        screen refreshes the display without another show_image() call."""
        if len(data) > MAX_UPLOAD:
            raise ValueError(f"{name}: {len(data)} bytes exceeds the 1 MB device limit")
        directory = "/" + directory.strip("/") + "/"
        ctype = "image/gif" if name.lower().endswith(".gif") else "image/jpeg"
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

        conn = http.client.HTTPConnection(self.host, timeout=max(self.timeout, 20))
        try:
            conn.request("POST", "/doUpload?" + urllib.parse.urlencode({"dir": directory}), body,
                         {"Content-Type": f"multipart/form-data; boundary={boundary}"})
            resp = conn.getresponse()
            status = resp.status
        except OSError as e:
            raise DeviceError(f"upload {name} failed: {e}") from e
        finally:
            conn.close()
        if status != 200:
            raise DeviceError(f"upload {name} returned HTTP {status}")

    def show_image(self, path: str) -> None:
        """Display a /image/... JPEG or GIF (only visible in the Photo Album theme)."""
        self._set(img=path)

    def set_weather_gif(self, path: str) -> None:
        """Set the 80x80 GIF shown in the weather-clock theme (/gif/...)."""
        self._set(gif=path)

    def delete(self, path: str) -> None:
        if not path.rsplit("/", 1)[-1].startswith(OWN_PREFIX):
            raise PermissionError(f"refusing to delete {path}: not a {OWN_PREFIX}* file")
        self._get("/delete", file=path)


_ROW = re.compile(r"<a href='([^']+)'>[^<]*</a></td><td>(\d+)</td>")


def parse_filelist(markup: str) -> list[RemoteFile]:
    return [RemoteFile(html.unescape(p), int(kb)) for p, kb in _ROW.findall(markup)]
