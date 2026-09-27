"""`clock` command line.

Rendering commands (text, color, image, meter, demo, pattern, anim, gif) push to the
device, or with --out save the render locally instead (PNG for stills, GIF for animations).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
from pathlib import Path

from PIL import Image

from clockdisplay import config
from clockdisplay.claude import auth
from clockdisplay.claude.auth import AuthError
from clockdisplay.claude.usage import RateLimited, fetch_usage
from clockdisplay.device import THEMES, DeviceError, UltraDevice
from clockdisplay.display import Display
from clockdisplay.render import anim, frames


def _output(args, content: Image.Image | bytes) -> None:
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Image.Image):
            content.save(out)
        else:
            out.write_bytes(content)
        print(f"saved {out}")
        return
    pushed = Display(_device(args)).show(content, force=args.force)
    print("pushed" if pushed else "unchanged, skipped (use --force)")


def _device(args) -> UltraDevice:
    return UltraDevice(config.resolve_host(args.host))


def _on_off(value: str) -> bool:
    if value.lower() in ("on", "1", "true", "yes"):
        return True
    if value.lower() in ("off", "0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


# --- device commands ---------------------------------------------------------

def cmd_info(args):
    info = _device(args).info()
    info["theme"] = f'{info["theme"]} ({THEMES.get(info["theme"], "?")})'
    for k, v in info.items():
        print(f"{k:>10}: {v}")


def cmd_theme(args):
    dev = _device(args)
    if args.number is None:
        for n, name in THEMES.items():
            print(f"{'*' if n == dev.theme() else ' '} {n} {name}")
    else:
        dev.set_theme(args.number)


def cmd_cycle(args):
    dev = _device(args)
    if args.off:
        dev.set_theme_cycle([], enabled=False, interval=args.interval)
    elif args.themes:
        dev.set_theme_cycle(args.themes, enabled=True, interval=args.interval)
    else:
        print(json.dumps(dev.theme_cycle()))


def cmd_brightness(args):
    dev = _device(args)
    if args.value is None:
        print(dev.brightness())
    else:
        dev.set_brightness(args.value)


def cmd_night(args):
    _device(args).set_night_mode(args.enabled, args.start, args.end, args.brightness)


def cmd_clockstyle(args):
    dev = _device(args)
    if args.colors:
        dev.set_clock_colors(*args.colors)
    if args.font is not None:
        dev.set_clock_font(args.font)
    if args.h12 is not None:
        dev.set_12h(args.h12)
    if args.colon is not None:
        dev.set_colon_blink(args.colon)
    if args.date is not None:
        dev.set_date_format(args.date)


def cmd_files(args):
    dev = _device(args)
    for f in dev.list_files("/" + args.dir):
        print(f"{'*' if f.ours else ' '} {f.size_kb:>5} KB  {f.path}")
    s = dev.space()
    print(f"free {s['free'] // 1024} KB of {s['total'] // 1024} KB   (* = ours)")


def cmd_restore(args):
    previous = Display(_device(args)).restore()
    print(f"restored theme {previous}" if previous else "nothing to restore")


def cmd_clean(args):
    removed = Display(_device(args)).clean()
    print("\n".join(f"deleted {p}" for p in removed) or "no cm_* files on device")


# --- Claude usage ------------------------------------------------------------

def cmd_usage(args):
    u = fetch_usage()
    print(f"5h session {u.five_pct:5.1f}%  resets {u.five_reset}")
    print(f"7d weekly  {u.week_pct:5.1f}%  resets {u.week_reset}")
    if not args.no_push:
        _output(args, frames.dual_meter(u.five_pct, u.five_reset, u.week_pct, u.week_reset))


def cmd_auth(args):
    if args.logout:
        auth.invalidate()
        print("cleared cached token from the credential store")
        return
    s = auth.status()
    print(f"credentials: {auth.credentials_path()}")
    print(f"     source: {s['source']}")
    if s["expires_in"] is not None:
        h, m = divmod(s["expires_in"] // 60, 60)
        print(f" expires in: {h}h {m}m")


def cmd_watch(args):
    import logging
    from clockdisplay.meter import Meter
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        Meter().run(threading.Event())
    except KeyboardInterrupt:
        pass


# --- render commands ---------------------------------------------------------

def cmd_text(args):
    _output(args, frames.text(args.message, args.fg, args.bg, args.style))


def cmd_color(args):
    _output(args, frames.solid(args.color))


def cmd_image(args):
    _output(args, frames.image(args.path, args.mode))


def cmd_gif(args):
    _output(args, anim.from_file(args.path, args.mode))


def cmd_meter(args):
    _output(args, frames.meter(args.pct, args.label, args.sub, args.color))


def cmd_demo(args):
    make = anim.dual_meter_fire if args.fire else frames.dual_meter
    _output(args, make(args.five, args.five_reset, args.week, args.week_reset))


def cmd_pattern(args):
    _output(args, frames.test_pattern())


def cmd_anim(args):
    if args.kind == "scroll":
        data = anim.scroll(args.value, args.fg, args.bg, speed=args.speed)
    elif args.kind == "fill":
        data = anim.fill(float(args.value), args.label)
    else:
        data = anim.blink(args.value, args.fg, args.bg)
    _output(args, data)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clock", description="Control a SmallTV-Ultra display.")
    p.add_argument("--host", help=f"device IP/hostname (default $CLOCK_HOST or {config.DEFAULT_HOST})")
    sub = p.add_subparsers(dest="cmd", required=True)

    def render_cmd(name, fn, help):
        sp = sub.add_parser(name, help=help)
        sp.add_argument("--out", help="save render to this file instead of pushing")
        sp.add_argument("--force", action="store_true", help="push even if unchanged")
        sp.set_defaults(fn=fn)
        return sp

    sub.add_parser("info", help="model, firmware, theme, brightness, storage").set_defaults(fn=cmd_info)

    sp = sub.add_parser("theme", help="list themes or switch to one")
    sp.add_argument("number", type=int, nargs="?", choices=sorted(THEMES))
    sp.set_defaults(fn=cmd_theme)

    sp = sub.add_parser("cycle", help="auto-rotate between themes, e.g. 'cycle 1 4 --interval 30'")
    sp.add_argument("themes", type=int, nargs="*", choices=sorted(THEMES))
    sp.add_argument("--interval", type=int, default=10, help="seconds per theme")
    sp.add_argument("--off", action="store_true", help="disable auto-rotation")
    sp.set_defaults(fn=cmd_cycle)

    sp = sub.add_parser("brightness", help="get or set brightness 0-100")
    sp.add_argument("value", type=int, nargs="?")
    sp.set_defaults(fn=cmd_brightness)

    sp = sub.add_parser("night", help="night-mode dimming schedule")
    sp.add_argument("enabled", type=_on_off)
    sp.add_argument("--start", type=int, default=22, help="hour 0-23")
    sp.add_argument("--end", type=int, default=7, help="hour 0-23")
    sp.add_argument("--brightness", type=int, default=10)
    sp.set_defaults(fn=cmd_night)

    sp = sub.add_parser("clockstyle", help="appearance of the built-in clock themes")
    sp.add_argument("--colors", nargs=3, metavar=("HOUR", "MIN", "SEC"), help="e.g. '#ffffff' '#feba01' '#ffffff'")
    sp.add_argument("--font", type=int, choices=[1, 2], help="1 big, 2 digital")
    sp.add_argument("--12h", dest="h12", type=_on_off)
    sp.add_argument("--colon", type=_on_off, help="blinking colon")
    sp.add_argument("--date", type=int, choices=[1, 2, 3, 4, 5],
                    help="1 DD/MM/YYYY 2 YYYY/MM/DD 3 MM/DD/YYYY 4 MM/DD 5 DD/MM")
    sp.set_defaults(fn=cmd_clockstyle)

    sp = sub.add_parser("files", help="list files on the device")
    sp.add_argument("dir", nargs="?", default="image", choices=["image", "gif"])
    sp.set_defaults(fn=cmd_files)

    sub.add_parser("restore", help="switch back to the theme active before we took over").set_defaults(fn=cmd_restore)
    sub.add_parser("clean", help="delete our cm_* files from the device").set_defaults(fn=cmd_clean)

    sp = render_cmd("usage", cmd_usage, "fetch live Claude usage (5h / 7d) and show it")
    sp.add_argument("--no-push", action="store_true", help="just print the numbers")

    sp = sub.add_parser("auth", help="show where the Claude token comes from")
    sp.add_argument("--logout", action="store_true", help="clear the cached token")
    sp.set_defaults(fn=cmd_auth)

    sub.add_parser("watch", help="keep the display updated with live usage (Ctrl+C to stop)").set_defaults(fn=cmd_watch)

    sp = render_cmd("text", cmd_text, "full-screen auto-sized text ('\\n' for new lines)")
    sp.add_argument("message")
    sp.add_argument("--fg", default="#ebebeb")
    sp.add_argument("--bg", default="#000000")
    sp.add_argument("--style", default="bold", choices=["bold", "regular", "mono"])

    sp = render_cmd("color", cmd_color, "fill the screen with one colour")
    sp.add_argument("color", help="'#d97757' or a CSS name")

    sp = render_cmd("image", cmd_image, "show a local image file, fitted to 240x240")
    sp.add_argument("path")
    sp.add_argument("--mode", default="cover", choices=["cover", "contain"])

    sp = render_cmd("gif", cmd_gif, "show a local animated GIF, fitted to 240x240")
    sp.add_argument("path")
    sp.add_argument("--mode", default="cover", choices=["cover", "contain"])

    sp = render_cmd("meter", cmd_meter, "ring gauge with a percentage")
    sp.add_argument("pct", type=float)
    sp.add_argument("--label", default="")
    sp.add_argument("--sub", default="")
    sp.add_argument("--color", help="override the green/yellow/red level colour")

    sp = render_cmd("demo", cmd_demo, "mock Claude usage card (5h + 7d bars)")
    sp.add_argument("--five", type=float, default=42)
    sp.add_argument("--five-reset", default="in 2h 15m")
    sp.add_argument("--week", type=float, default=76)
    sp.add_argument("--week-reset", default="Mon 9:00 AM")
    sp.add_argument("--fire", action="store_true", help="5h line on fire (heavy burn animation)")

    render_cmd("pattern", cmd_pattern, "colour/geometry test pattern")

    sp = render_cmd("anim", cmd_anim, "animated GIF: scroll TEXT | fill PCT | blink TEXT")
    sp.add_argument("kind", choices=["scroll", "fill", "blink"])
    sp.add_argument("value")
    sp.add_argument("--fg", default="#d97757")
    sp.add_argument("--bg", default="#000000")
    sp.add_argument("--label", default="")
    sp.add_argument("--speed", type=int, default=8, help="scroll pixels per frame")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except (DeviceError, AuthError, RateLimited, urllib.error.URLError,
            PermissionError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
