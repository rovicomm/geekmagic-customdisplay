"""Composable drawing primitives for a 240x240 canvas."""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageOps

from clockdisplay.render.canvas import DIM, TRACK, Color, color, font


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, max_h: int,
             style: str = "bold", start: int = 120, min_size: int = 8):
    """Largest font of `style` where `text` (may contain newlines) fits the box."""
    for size in range(start, min_size - 1, -2):
        f = font(size, style)
        l, t, r, b = draw.multiline_textbbox((0, 0), text, font=f, align="center")
        if r - l <= max_w and b - t <= max_h:
            return f
    return font(min_size, style)


def text_box(img: Image.Image, text: str, box: tuple[int, int, int, int],
             fill: str | Color = (255, 255, 255), style: str = "bold",
             size: int | None = None) -> None:
    """Draw text centred in box (x0, y0, x1, y1), auto-sized unless `size` given."""
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    f = font(size, style) if size else fit_font(draw, text, x1 - x0, y1 - y0, style)
    draw.multiline_text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=f, fill=color(fill),
                        anchor="mm", align="center")


def bar(img: Image.Image, box: tuple[int, int, int, int], pct: float,
        fill: str | Color, track: str | Color = TRACK, radius: int = 0) -> None:
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=radius, fill=color(track))
    filled = int((x1 - x0) * max(0.0, min(pct, 100.0)) / 100)
    if filled > 0:
        draw.rounded_rectangle((x0, y0, x0 + filled, y1), radius=radius, fill=color(fill))


def ring(img: Image.Image, center: tuple[int, int], radius: int, width: int, pct: float,
         fill: str | Color, track: str | Color = TRACK, start_deg: float = -90) -> None:
    """Circular gauge starting at 12 o'clock, clockwise."""
    draw = ImageDraw.Draw(img)
    cx, cy = center
    bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
    draw.ellipse(bbox, outline=color(track), width=width)
    sweep = 360 * max(0.0, min(pct, 100.0)) / 100
    if sweep > 0:
        draw.arc(bbox, start_deg, start_deg + sweep, fill=color(fill), width=width)


def sparkline(img: Image.Image, box: tuple[int, int, int, int], values: list[float],
              fill: str | Color, lo: float | None = None, hi: float | None = None,
              width: int = 2, baseline: str | Color = DIM) -> None:
    if len(values) < 2:
        return
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = (hi - lo) or 1
    step = (x1 - x0) / (len(values) - 1)
    pts = [(x0 + i * step, y1 - (v - lo) / span * (y1 - y0)) for i, v in enumerate(values)]
    draw.line((x0, y1, x1, y1), fill=color(baseline), width=1)
    draw.line(pts, fill=color(fill), width=width, joint="curve")


def paste_fit(img: Image.Image, src: Image.Image, box: tuple[int, int, int, int],
              mode: str = "cover") -> None:
    """Paste `src` into box, either cropping to fill ('cover') or letterboxing ('contain')."""
    x0, y0, x1, y1 = box
    size = (x1 - x0, y1 - y0)
    src = ImageOps.exif_transpose(src).convert("RGBA")
    fitted = ImageOps.fit(src, size) if mode == "cover" else ImageOps.contain(src, size)
    ox = x0 + (size[0] - fitted.width) // 2
    oy = y0 + (size[1] - fitted.height) // 2
    img.paste(fitted, (ox, oy), fitted)


FIRE = [(150, 20, 10), (230, 70, 20), (255, 150, 30), (255, 225, 110)]  # outer -> core


def flames(img: Image.Image, box: tuple[int, int, int, int], phase: float) -> None:
    """Flames rising from the bottom edge of box, one column per pixel. `phase` is in
    radians; every term uses an integer multiple of it, so phase 0..2pi loops seamlessly."""
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    w, max_h = x1 - x0, y1 - y0
    for i in range(w):
        taper = min(1.0, (i + 1) / 10, (w - i) / 10)  # narrow the fire at both ends
        v = (0.55 + 0.25 * math.sin(i * 0.31 - phase * 2) + 0.15 * math.sin(i * 0.11 + phase * 3)
             + 0.10 * math.sin(i * 0.83 - phase * 5))
        h = max_h * taper * max(0.0, min(v, 1.0))
        for layer, c in enumerate(FIRE):
            lh = h * (1 - layer * 0.26)
            if lh >= 1:
                draw.line((x0 + i, y1, x0 + i, y1 - lh), fill=c)
    for k in range(max(1, w // 24)):  # sparks drifting up; offsets are fixed per spark
        t = (phase / (2 * math.pi) + k * 0.37) % 1
        sx = x0 + (k * 53 + int(6 * math.sin(phase + k))) % max(1, w)
        sy = y1 - max_h * (0.5 + 0.7 * t)
        draw.rectangle((sx, sy, sx + 1, sy + 1), fill=FIRE[2 if t < 0.6 else 1])
