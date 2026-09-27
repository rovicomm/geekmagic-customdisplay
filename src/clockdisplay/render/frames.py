"""Ready-made full-screen 240x240 frames."""
from __future__ import annotations

from PIL import Image, ImageDraw

from clockdisplay.render import canvas, widgets
from clockdisplay.render.canvas import BG, CLAUDE, DIM, SIZE, TEXT, Color, level_color


def solid(fill: str | Color) -> Image.Image:
    return canvas.new(fill)


def text(msg: str, fg: str | Color = TEXT, bg: str | Color = BG, style: str = "bold",
         margin: int = 12) -> Image.Image:
    """Auto-sized, centred text. Use '\\n' for multiple lines."""
    img = canvas.new(bg)
    widgets.text_box(img, msg.replace("\\n", "\n"), (margin, margin, SIZE - margin, SIZE - margin),
                     fill=fg, style=style)
    return img


def image(path: str, mode: str = "cover", bg: str | Color = BG) -> Image.Image:
    img = canvas.new(bg)
    with Image.open(path) as src:
        widgets.paste_fit(img, src, (0, 0, SIZE, SIZE), mode)
    return img


def meter(pct: float, label: str = "", sub: str = "", fill: str | Color | None = None) -> Image.Image:
    """Single ring gauge with a big percentage in the middle."""
    img = canvas.new()
    c = canvas.color(fill) if fill else level_color(pct)
    widgets.ring(img, (120, 120), 104, 16, pct, c)
    widgets.text_box(img, f"{pct:.0f}%", (50, 78, 190, 150), fill=c)
    if label:
        widgets.text_box(img, label, (60, 48, 180, 76), fill=DIM, style="regular", size=20)
    if sub:
        widgets.text_box(img, sub, (60, 152, 180, 176), fill=DIM, style="regular", size=16)
    return img


def dual_meter(five_pct: float, five_reset: str, week_pct: float, week_reset: str,
               title: str = "Claude usage") -> Image.Image:
    """Two-bar usage card (layout from claude-meter's photo240 renderer)."""
    img = canvas.new()
    draw = ImageDraw.Draw(img)
    f_title, f_pct, f_small = canvas.font(20), canvas.font(34), canvas.font(14, "regular")
    draw.text((12, 8), title, font=f_title, fill=CLAUDE)

    def section(y: int, label: str, pct: float, reset: str) -> None:
        c = level_color(pct)
        draw.text((12, y), label, font=f_small, fill=DIM)
        pct_text = f"{max(0.0, pct):.0f}%"
        draw.text((228 - f_pct.getlength(pct_text), y - 4), pct_text, font=f_pct, fill=c)
        widgets.bar(img, (12, y + 38, 228, y + 52), pct, c, radius=4)
        draw.text((12, y + 56), f"resets {reset}", font=f_small, fill=DIM)

    section(48, "5h session", five_pct, five_reset)
    section(142, "7d weekly", week_pct, week_reset)
    return img


def test_pattern() -> Image.Image:
    """Colour bars, gradient, border and centre cross for checking geometry and colour."""
    img = canvas.new()
    draw = ImageDraw.Draw(img)
    bars = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
            (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    w = SIZE / len(bars)
    for i, c in enumerate(bars):
        draw.rectangle((round(i * w), 0, round((i + 1) * w) - 1, 139), fill=c)
    for x in range(SIZE):
        v = round(x * 255 / (SIZE - 1))
        draw.line((x, 140, x, 179), fill=(v, v, v))
    for i in range(8):
        draw.rectangle((i * 30, 180, i * 30 + 29, 239), fill=CLAUDE if i % 2 else (30, 30, 30))
    draw.rectangle((0, 0, SIZE - 1, SIZE - 1), outline=(255, 0, 0))
    draw.line((120, 100, 120, 140), fill=(255, 0, 0))
    draw.line((100, 120, 140, 120), fill=(255, 0, 0))
    widgets.text_box(img, "240x240", (60, 190, 180, 230), fill=TEXT, size=22)
    return img
