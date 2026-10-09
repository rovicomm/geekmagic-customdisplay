"""Tiny localhost HTTP server feeding the Zebar bar section (zebar/claude-usage.js).

  GET  /usage     latest numbers as JSON ({"enabled": false} hides the bar section)
  GET  /fire.png  transparent animated flames (APNG) shown while the 5h line is on fire
  POST /anchor    {"x", "y", "w", "h", "primary"}: the section's screen rect in physical
                  pixels, so the reset pop-up can hang right underneath it. With a bar on
                  every monitor, the one on the primary monitor wins.
"""
from __future__ import annotations

import io
import json
import logging
import math
import socket
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from clockdisplay.claude.usage import Usage
from clockdisplay.render import canvas, widgets

log = logging.getLogger(__name__)

DEFAULT_PORT = 47815


@lru_cache(maxsize=1)
def fire_png(w: int = 72, h: int = 22, steps: int = 12, frame_ms: int = 80) -> bytes:
    """The device's flames (same seamless phase loop as anim.dual_meter_fire), on transparent."""
    out = []
    for i in range(steps):
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        widgets.flames(img, (0, 0, w, h), 2 * math.pi * i / steps)
        out.append(img)
    buf = io.BytesIO()
    out[0].save(buf, "PNG", save_all=True, append_images=out[1:], duration=frame_ms, loop=0,
                disposal=1)
    return buf.getvalue()


def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % c


class BarState:
    """What the bar shows. Written by the tray thread, read by request threads."""

    def __init__(self):
        self.enabled = False
        self.anchor: dict | None = None
        self._payload: dict = {}
        self._lock = threading.Lock()

    def update(self, usage: Usage | None, on_fire: bool = False, burn_rate: float | None = None,
               error: str | None = None) -> None:
        payload: dict = {"error": error, "on_fire": on_fire, "burn_rate": burn_rate}
        if usage is not None:
            payload.update(
                five_pct=round(usage.five_pct, 1), five_reset=usage.five_reset,
                week_pct=round(usage.week_pct, 1), week_reset=usage.week_reset,
                color5=_hex(canvas.level_color(usage.five_pct)),
                color7=_hex(canvas.level_color(usage.week_pct)))
        with self._lock:
            self._payload = payload

    def snapshot(self) -> dict:
        with self._lock:
            return {"enabled": self.enabled, "need_anchor": self.anchor is None,
                    **(self._payload if self.enabled else {})}


def _handler(state: BarState):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Cache-Control", "no-store" if ctype == "application/json" else "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):  # CORS preflight for the JSON POST
            self._send(204, b"", "text/plain")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/usage":
                self._send(200, json.dumps(state.snapshot()).encode(), "application/json")
            elif path == "/fire.png":
                self._send(200, fire_png(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/anchor":
                self._send(404, b"not found", "text/plain")
                return
            try:
                raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096))
                data = json.loads(raw)
                anchor = {k: int(data[k]) for k in ("x", "y", "w", "h")}
                anchor["primary"] = bool(data.get("primary"))
            except (ValueError, KeyError, TypeError):
                self._send(400, b"bad anchor", "text/plain")
                return
            if anchor["primary"] or not (state.anchor and state.anchor["primary"]):
                state.anchor = anchor
            self._send(200, b"{}", "application/json")

        def log_message(self, fmt, *args):  # no per-request noise in tray.log
            pass

    return Handler


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that's in use (and steal
    # its traffic), so ask for the port exclusively: a busy port fails instead.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(state: BarState, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Start serving on 127.0.0.1:port in a daemon thread; returns the server (shutdown() it).
    Raises OSError if the port is taken."""
    httpd = _Server(("127.0.0.1", port), _handler(state))
    threading.Thread(target=httpd.serve_forever, name="bar-server", daemon=True).start()
    log.info("bar server on http://127.0.0.1:%s", httpd.server_address[1])
    return httpd
