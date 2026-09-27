r"""Render docs/images/adsb.png: the usage card, and an aircraft pop-up with and without a photo.

    .venv\Scripts\python scripts\readme_images.py

The flights are made up (registrations in formats that can't be issued), and the "photo"
is drawn here: real photos come from planespotters.net at runtime and aren't ours to
redistribute.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from clockdisplay.adsb import card
from clockdisplay.adsb.photos import Photo
from clockdisplay.adsb.routes import Airport, Route
from clockdisplay.adsb.source import Aircraft
from clockdisplay.render import canvas, frames

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["photo", "callsign", "route", "type", "registration", "operator", "altitude", "speed", "distance"]


def illustration(w: int = 420, h: int = 280) -> Image.Image:
    """A simple airliner against a sky gradient, standing in for a planespotters photo."""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line((0, y, w, y), fill=(int(70 + 110 * t), int(130 + 90 * t), int(200 + 45 * t)))
    body, shade, tail = (245, 245, 248), (200, 205, 215), (27, 60, 130)
    d.polygon([(150, 150), (250, 150), (180, 215), (150, 215)], fill=shade)   # far wing
    d.rounded_rectangle((60, 128, 360, 162), radius=17, fill=body)            # fuselage
    d.polygon([(352, 132), (395, 138), (352, 158)], fill=body)                # nose
    d.polygon([(60, 130), (95, 130), (58, 72), (38, 72)], fill=tail)          # fin
    d.polygon([(52, 140), (100, 140), (70, 118), (46, 118)], fill=shade)      # tailplane
    d.polygon([(170, 152), (250, 152), (200, 95), (178, 95)], fill=body)      # near wing
    d.rounded_rectangle((196, 110, 236, 126), radius=8, fill=shade)           # engine
    for x in range(120, 345, 14):
        d.rectangle((x, 137, x + 6, 142), fill=(90, 110, 150))                # windows
    d.line((70, 152, 350, 152), fill=tail, width=4)                           # cheatline
    return img


def main() -> None:
    usage = frames.dual_meter(42, "in 2h 15m", 76, "Mon 9:00 AM")
    jet = Aircraft(hex="000001", callsign="BAW117", registration="G-0DMO", type_code="B77W",
                   description="BOEING 777-300ER", operator="EXAMPLE AIRWAYS", altitude=11200,
                   vertical_rate=-1400, speed=292, distance=1.8, bearing=315)
    route = Route(Airport("LHR", "EGLL", "London"), Airport("JFK", "KJFK", "New York"))
    popup = card.render(jet, FIELDS, Photo(illustration(), "photographer"), route)
    small = Aircraft(hex="000002", callsign="N0DEMO", registration="N0DEMO", type_code="C172",
                     description="CESSNA 172 Skyhawk", altitude=2500, vertical_rate=500,
                     speed=104, distance=0.9, bearing=45)
    no_photo = card.render(small, FIELDS, None)

    panels = [(usage, "Claude usage"), (popup, "a plane comes overhead"), (no_photo, "no photo on file")]
    pad, gap, caption = 16, 20, 30
    sheet = Image.new("RGB", (pad * 2 + 240 * 3 + gap * 2, pad * 2 + 240 + caption), (24, 24, 27))
    d = ImageDraw.Draw(sheet)
    f = canvas.font(15, "regular")
    for i, (img, label) in enumerate(panels):
        x = pad + i * (240 + gap)
        sheet.paste(img, (x, pad))
        d.rectangle((x - 1, pad - 1, x + 240, pad + 240), outline=(70, 70, 76))
        d.text((x + 120, pad + 240 + caption // 2 + 2), label, font=f, fill=(170, 170, 176), anchor="mm")
    out = ROOT / "docs" / "images" / "adsb.png"
    sheet.save(out, optimize=True)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
