"""Canvas basics: size, palette, fonts, colour parsing, encoding."""
from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageColor, ImageFont

SIZE = 240
Color = tuple[int, int, int]

# Palette (bar colours / thresholds adapted from claude-meter's renderers).
BG     = (0, 0, 0)
TEXT   = (235, 235, 235)
DIM    = (140, 140, 140)
TRACK  = (40, 40, 40)
GREEN  = (26, 166, 75)
YELLOW = (228, 184, 26)
RED    = (217, 58, 58)
CLAUDE = (217, 119, 87)


def level_color(pct: float) -> Color:
    if pct >= 90:
        return RED
    if pct >= 70:
        return YELLOW
    return GREEN


def color(value: str | Color) -> Color:
    """Accept '#rrggbb', CSS names ('orange') or an RGB tuple."""
    if isinstance(value, tuple):
        return value
    return ImageColor.getrgb(value)[:3]


_FONTS = {
    "regular": ["C:/Windows/Fonts/segoeui.ttf", "/System/Library/Fonts/SFNS.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "bold":    ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "mono":    ["C:/Windows/Fonts/consolab.ttf", "/System/Library/Fonts/Menlo.ttc",
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"],
}


@lru_cache(maxsize=128)
def font(size: int, style: str = "bold") -> ImageFont.FreeTypeFont:
    for path in _FONTS[style]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def new(bg: str | Color = BG) -> Image.Image:
    return Image.new("RGB", (SIZE, SIZE), color(bg))


def to_jpeg(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def to_gif(frames: list[Image.Image], duration_ms: int | list[int] = 100, loop: int = 0) -> bytes:
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:],
                   duration=duration_ms, loop=loop, optimize=True, disposal=1)
    return buf.getvalue()
