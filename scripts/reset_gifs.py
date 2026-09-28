r"""Render src/clockdisplay/assets/reset_{5h,7d}.gif from the originals in docs/images.

    .venv\Scripts\python scripts\reset_gifs.py

The originals are too big for the device and not 240x240, so each is fitted to the screen,
then frames are dropped and the palette cut until it fits BUDGET. Both GIFs are kept on the
display (~1.2 MB free on a typical one), so they're held well under the 1 MB upload limit.
"""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageSequence

from clockdisplay.render import canvas, widgets
from clockdisplay.render.canvas import SIZE

ROOT = Path(__file__).resolve().parent.parent
# window -> (source, fit). One "s" per unit of the window. "top" crops a square off the top of a
# portrait GIF (the face is up there); the rest go through widgets.paste_fit.
SOURCES = {"5h": ("yesssss.gif", "top"), "7d": ("yesssssss.gif", "contain")}
BUDGET = 400 * 1024
MIN_STEP = 2  # keep every other frame (same loop length) so both GIFs fit on the display


def fit(frame: Image.Image, mode: str) -> Image.Image:
    if mode == "top":
        w, h = frame.size
        side = min(w, h)
        x0 = (w - side) // 2
        return frame.convert("RGB").crop((x0, 0, x0 + side, side)).resize((SIZE, SIZE), Image.LANCZOS)
    img = canvas.new()
    widgets.paste_fit(img, frame, (0, 0, SIZE, SIZE), mode)
    return img


def encode(frames: list[Image.Image], durations: list[int], colors: int) -> bytes:
    quantized = [f.quantize(colors, method=Image.Quantize.MEDIANCUT) for f in frames]
    buf = io.BytesIO()
    quantized[0].save(buf, "GIF", save_all=True, append_images=quantized[1:],
                      duration=durations, loop=0, optimize=True)
    return buf.getvalue()


def shrink(path: Path, mode: str) -> bytes:
    frames, durations = [], []
    with Image.open(path) as src:
        for fr in ImageSequence.Iterator(src):
            frames.append(fit(fr.copy(), mode))
            durations.append(fr.info.get("duration", 100))
    step = MIN_STEP
    while True:
        # Drop frames but keep the loop the same length.
        kept = frames[::step]
        kept_ms = [sum(durations[i:i + step]) for i in range(0, len(frames), step)]
        for colors in (128, 64, 32):
            data = encode(kept, kept_ms, colors)
            if len(data) <= BUDGET:
                print(f"{path.name}: {len(kept)} frames, {colors} colours, {len(data) / 1024:.0f} KB")
                return data
        step += 1


def main() -> None:
    out = ROOT / "src" / "clockdisplay" / "assets"
    out.mkdir(exist_ok=True)
    for window, (name, mode) in SOURCES.items():
        (out / f"reset_{window}.gif").write_bytes(shrink(ROOT / "docs" / "images" / name, mode))


if __name__ == "__main__":
    main()
