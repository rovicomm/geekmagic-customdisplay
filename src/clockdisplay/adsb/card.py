"""The 240x240 aircraft card: optional photo on top, the chosen fields stacked underneath."""
from __future__ import annotations

from PIL import Image, ImageDraw

from clockdisplay.adsb.photos import Photo
from clockdisplay.adsb.source import Aircraft
from clockdisplay.render import canvas, widgets
from clockdisplay.render.canvas import CLAUDE, DIM, RED, SIZE, TEXT

PHOTO_H = 136
MARGIN = 10
EMERGENCY = {"7500", "7600", "7700"}


def _clip(draw: ImageDraw.ImageDraw, text: str, font, max_w: float) -> str:
    """Shorten `text` with an ellipsis until it fits max_w pixels."""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _fit(draw: ImageDraw.ImageDraw, text: str, max_w: float, start: int, smallest: int):
    """Largest bold font from `start` down to `smallest` that fits `text` in max_w."""
    for size in range(start, smallest, -2):
        f = canvas.font(size)
        if draw.textlength(text, font=f) <= max_w:
            return f
    return canvas.font(smallest)


def altitude_text(ac: Aircraft) -> str:
    if ac.on_ground:
        return "ground"
    return "" if ac.altitude is None else f"{ac.altitude:,} ft"


CLIMB_RATE = 256  # ft/min; slower than this counts as level flight


def _climb_arrow(draw: ImageDraw.ImageDraw, x: float, y_mid: float, rate: int) -> None:
    """Small up/down triangle for climbing/descending."""
    s = 6
    pts = [(x, y_mid + s), (x + 2 * s, y_mid + s), (x + s, y_mid - s)] if rate > 0 else \
          [(x, y_mid - s), (x + 2 * s, y_mid - s), (x + s, y_mid + s)]
    draw.polygon(pts, fill=CLAUDE)


def render(ac: Aircraft, fields: list[str] | tuple[str, ...], photo: Photo | None = None) -> Image.Image:
    img = canvas.new()
    draw = ImageDraw.Draw(img)
    width = SIZE - 2 * MARGIN
    show = set(fields)
    has_photo = "photo" in show and photo is not None

    if has_photo:
        widgets.paste_fit(img, photo.image, (0, 0, SIZE, PHOTO_H), "cover")
        if photo.photographer:
            f = canvas.font(11, "regular")
            credit = _clip(draw, f"© {photo.photographer} / planespotters.net", f, SIZE - 8)
            w = draw.textlength(credit, font=f)
            draw.rectangle((SIZE - w - 8, PHOTO_H - 15, SIZE, PHOTO_H), fill=(0, 0, 0))
            draw.text((SIZE - w - 4, PHOTO_H - 14), credit, font=f, fill=DIM)
        y = PHOTO_H + 4
        f_mid, f_small = canvas.font(17, "regular"), canvas.font(14, "regular")
    else:
        y = 12
        f_mid, f_small = canvas.font(22, "regular"), canvas.font(18, "regular")

    head = ac.name if "callsign" in show else ""
    alt = altitude_text(ac) if "altitude" in show else ""
    climbing = bool(alt) and ac.vertical_rate is not None and abs(ac.vertical_rate) >= CLIMB_RATE
    arrow_w = 16 if climbing else 0

    def draw_alt(x: float, baseline: float, f) -> None:
        if climbing:
            _climb_arrow(draw, x - arrow_w, baseline - f.size * 0.35, ac.vertical_rate)
        draw.text((x, baseline), alt, font=f, fill=CLAUDE, anchor="ls")

    if has_photo:  # one headline: callsign left, altitude (with climb/descent arrow) right
        if head or alt:
            f_alt = canvas.font(20)
            alt_w = draw.textlength(alt, font=f_alt) + arrow_w + 8 if alt else 0
            baseline = y + 26
            if head:
                f_head = _fit(draw, head, width - alt_w, 26, 18)
                draw.text((MARGIN, baseline), _clip(draw, head, f_head, width - alt_w),
                          font=f_head, fill=TEXT, anchor="ls")
            if alt:
                draw_alt(SIZE - MARGIN - draw.textlength(alt, font=f_alt), baseline, f_alt)
            y = baseline + 8
    else:  # room for the callsign and altitude on lines of their own
        if head:
            f_head = _fit(draw, head, width, 46, 24)
            draw.text((MARGIN, y + f_head.size), _clip(draw, head, f_head, width),
                      font=f_head, fill=TEXT, anchor="ls")
            y += f_head.size + 10
        if alt:
            f_alt = canvas.font(28)
            draw_alt(MARGIN + arrow_w, y + 26, f_alt)
            y += 38

    lines: list[tuple[str, object, tuple]] = []
    if "type" in show and (ac.description or ac.type_code):
        lines.append((ac.description or ac.type_code, f_mid, TEXT))
    ident = [s for s, key in ((ac.registration, "registration"), (ac.operator, "operator")) if s and key in show]
    if ident:
        lines.append((" · ".join(ident), f_small, DIM))
    motion = []
    if "speed" in show and ac.speed is not None:
        motion.append(f"{ac.speed:.0f} kt")
    if "distance" in show and ac.distance is not None:
        motion.append(f"{ac.distance:.1f} nm {ac.direction}".rstrip())
    if motion:
        lines.append((" · ".join(motion), f_small, DIM))
    if "squawk" in show and ac.squawk:
        lines.append((f"squawk {ac.squawk}", f_small, RED if ac.squawk in EMERGENCY else DIM))

    for text, f, fill in lines:
        if y + f.size > SIZE - 4:
            break
        draw.text((MARGIN, y), _clip(draw, text, f, width), font=f, fill=fill)
        y += f.size + (5 if has_photo else 10)
    return img
