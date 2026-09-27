"""Animated 240x240 GIFs. The device plays these in the Photo Album theme.

Uploading is ~0.5 s per file, so anything smoother than ~2 fps must be a GIF
rather than a stream of pushed frames.
"""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageSequence

from clockdisplay.render import canvas, frames, widgets
from clockdisplay.render.canvas import BG, SIZE, TEXT, Color


def scroll(msg: str, fg: str | Color = TEXT, bg: str | Color = BG, size: int = 96,
           speed: int = 8, frame_ms: int = 50) -> bytes:
    """Marquee: text scrolls right-to-left across the screen, looping."""
    f = canvas.font(size)
    probe = ImageDraw.Draw(canvas.new())
    l, t, r, b = probe.textbbox((0, 0), msg, font=f)
    text_w = r - l
    y = (SIZE - (b - t)) // 2 - t
    out = []
    for x in range(SIZE, -text_w - speed, -speed):
        img = canvas.new(bg)
        ImageDraw.Draw(img).text((x - l, y), msg, font=f, fill=canvas.color(fg))
        out.append(img)
    return canvas.to_gif(out, frame_ms)


def fill(pct: float, label: str = "", sub: str = "", steps: int = 20, frame_ms: int = 40,
         hold_ms: int = 3000) -> bytes:
    """Ring meter sweeping from 0 up to pct, then holding."""
    out = [frames.meter(pct * i / steps, label, sub) for i in range(steps + 1)]
    return canvas.to_gif(out, [frame_ms] * steps + [hold_ms])


def dual_meter_fire(five_pct: float, five_reset: str, week_pct: float, week_reset: str,
                    steps: int = 12, frame_ms: int = 80) -> bytes:
    """The usage card with the 5h line on fire, looping seamlessly."""
    out = [frames.dual_meter(five_pct, five_reset, week_pct, week_reset,
                             fire_phase=2 * math.pi * i / steps) for i in range(steps)]
    return canvas.to_gif(out, frame_ms)


def blink(msg: str, fg: str | Color = TEXT, bg: str | Color = BG, on_ms: int = 600,
          off_ms: int = 400) -> bytes:
    return canvas.to_gif([frames.text(msg, fg, bg), canvas.new(bg)], [on_ms, off_ms])


def from_file(path: str, mode: str = "cover") -> bytes:
    """Load any GIF; pass it through untouched if already 240x240, else refit every frame."""
    with open(path, "rb") as fh:
        raw = fh.read()
    with Image.open(path) as src:
        if src.size == (SIZE, SIZE):
            return raw
        out, durations = [], []
        for fr in ImageSequence.Iterator(src):
            img = canvas.new()
            widgets.paste_fit(img, fr.copy(), (0, 0, SIZE, SIZE), mode)
            out.append(img)
            durations.append(fr.info.get("duration", 100))
    return canvas.to_gif(out, durations)
