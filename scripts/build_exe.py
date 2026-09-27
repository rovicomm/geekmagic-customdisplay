r"""Build the portable tray app: dist\ClockDisplay.exe (PyInstaller, one file, no console).

    .venv\Scripts\pip install -e ".[build]"
    .venv\Scripts\python scripts\build_exe.py
"""
from __future__ import annotations

from pathlib import Path

import PyInstaller.__main__

from clockdisplay.tray import render_icon

ROOT = Path(__file__).resolve().parent.parent


def make_icon() -> Path:
    ico = ROOT / "build" / "clockdisplay.ico"
    ico.parent.mkdir(exist_ok=True)
    img = render_icon(60, size=256)
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico


def main() -> None:
    make_icon()
    PyInstaller.__main__.run([
        str(ROOT / "packaging" / "ClockDisplay.spec"), "--noconfirm", "--clean",
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build" / "pyinstaller"),
    ])
    exe = ROOT / "dist" / "ClockDisplay.exe"
    print(f"\nbuilt {exe} ({exe.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
