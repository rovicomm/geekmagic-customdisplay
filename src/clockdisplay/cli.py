"""`clock` command line.

Rendering commands (text, color, image, meter, demo, pattern, anim, gif) push to the
device, or with --out save the render locally instead (PNG for stills, GIF for animations).
The target is --host, else --display NAME (or "all"), else the first configured display.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import urllib.error
from pathlib import Path

from PIL import Image

from clockdisplay import __version__, config
from clockdisplay.adsb.source import SourceError
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
    _for_each(args, lambda dev: "pushed" if Display(dev).show(content, force=args.force)
              else "unchanged, skipped (use --force)")


def _devices(args) -> list[tuple[str, UltraDevice]]:
    """(name, device) pairs selected by --host / --display; defaults to the first display."""
    if args.host:
        return [(args.host, UltraDevice(args.host))]
    if args.display == "all":
        return [(d["name"], UltraDevice(d["host"])) for d in config.load_displays()]
    if args.display:
        d = config.find_display(args.display)
        return [(d["name"], UltraDevice(d["host"]))]
    host = config.resolve_host()
    return [(host, UltraDevice(host))]


def _for_each(args, fn) -> None:
    """Run fn(device) -> message on every selected display. With several displays, output is
    prefixed with the name and one failing display doesn't stop the rest (exit status 1)."""
    devices = _devices(args)
    if len(devices) == 1:
        print(fn(devices[0][1]))
        return
    failed = False
    for name, dev in devices:
        try:
            print(f"{name}: {fn(dev)}")
        except (DeviceError, urllib.error.URLError, OSError) as e:
            print(f"{name}: error: {e}", file=sys.stderr)
            failed = True
    if failed:
        sys.exit(1)


def _device(args) -> UltraDevice:
    if args.display == "all" and not args.host:
        raise ValueError("this command takes a single display, not --display all")
    return _devices(args)[0][1]


def _on_off(value: str) -> bool:
    if value.lower() in ("on", "1", "true", "yes"):
        return True
    if value.lower() in ("off", "0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


# --- device commands ---------------------------------------------------------

def cmd_rename(args):
    config.rename_display(args.old, args.new)
    print(f"renamed {args.old} -> {args.new.strip()}")


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
    def restore(dev):
        previous = Display(dev).restore()
        return f"restored theme {previous}" if previous else "nothing to restore"
    _for_each(args, restore)


def cmd_clean(args):
    def clean(dev):
        removed = Display(dev).clean()
        return ", ".join(f"deleted {p}" for p in removed) or "no cm_* files on device"
    _for_each(args, clean)


def cmd_displays(args):
    for d in config.load_displays():
        try:
            info = UltraDevice(d["host"], timeout=3).info()
            status = f'{info["model"]} {info["version"]}, theme {info["theme"]} ({THEMES.get(info["theme"], "?")})'
        except (DeviceError, OSError) as e:
            status = f"unreachable ({e})" if args.verbose else "unreachable"
        print(f'{d["name"]:<12} {d["host"]:<16} {d["app"]:<8} {status}')


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
    from clockdisplay.adsb.spotter import Spotter
    from clockdisplay.meter import Meter
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    stop, meter = threading.Event(), Meter()
    spotter = Spotter(meter)
    threading.Thread(target=spotter.run, args=(stop,), name="adsb", daemon=True).start()
    try:
        meter.run(stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        spotter.wake.set()


# --- aircraft ------------------------------------------------------------------

def _spotter():
    from clockdisplay.adsb.spotter import Spotter
    from clockdisplay.meter import Meter
    sp = Spotter(Meter())
    sp.refresh(ignore_enabled=True)  # one-off commands work even with spotting switched off
    if not sp.settings["url"]:
        raise ValueError('no ADS-B receiver configured: set "adsb": {"url": "http://..."} in '
                         f"{config.config_file()}")
    return sp


def cmd_planes(args):
    from clockdisplay.adsb import source
    from clockdisplay.adsb.card import altitude_text
    sp = _spotter()
    rows = sp.nearby if not args.all else sorted(
        sp.aircraft, key=lambda ac: (ac.distance is None, ac.distance or 0))
    radius, popup = sp.settings["radius"], sp.popup_radius
    print(f"{len(sp.nearby)} of {len(sp.aircraft)} tracked aircraft within {radius} nm and your filters"
          f" (* = within the {popup:g} nm pop-up radius)")
    for ac in rows:
        dist = "" if ac.distance is None else f"{ac.distance:5.1f} nm {ac.direction:<2}"
        mark = "*" if source.matches(ac, {**sp.settings, "radius": popup}) else " "
        print(f"{mark} {ac.hex:<7} {ac.name:<9} {ac.type_code:<5} {altitude_text(ac):>10}  {dist:<11} "
              f"{ac.description}")


def cmd_plane(args):
    sp = _spotter()
    if args.query:
        q = args.query.strip().lower()
        found = [ac for ac in sp.aircraft
                 if q in (ac.hex, ac.callsign.lower(), ac.registration.lower().replace("-", ""),
                          ac.registration.lower())]
        if not found:
            raise ValueError(f"no tracked aircraft matches {args.query!r} (see `clock planes --all`)")
        ac = found[0]
    else:
        pool = sp.nearby or sorted((a for a in sp.aircraft if a.distance is not None),
                                   key=lambda a: a.distance)
        if not pool:
            raise ValueError("the receiver isn't tracking any aircraft with a position")
        ac = pool[0]
    route = sp.routes.lookup(ac, sp.settings["route_api"])
    print(f"{ac.name} ({ac.hex}) {ac.description or ac.type_code}"
          + (f", {route.codes.replace(' → ', ' -> ')} ({route.cities.replace('–', '-')})" if route else ""))
    _output(args, sp.card(ac))


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
    p.add_argument("--version", action="version", version=f"clock {__version__}")
    p.add_argument("--host", help="device IP/hostname (default $CLOCK_HOST or the first configured display)")
    p.add_argument("-d", "--display", metavar="NAME",
                   help="configured display to target by name, or 'all' (see `clock displays`)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def render_cmd(name, fn, help):
        sp = sub.add_parser(name, help=help)
        sp.add_argument("--out", help="save render to this file instead of pushing")
        sp.add_argument("--force", action="store_true", help="push even if unchanged")
        sp.set_defaults(fn=fn)
        return sp

    sp = sub.add_parser("displays", help="list configured displays and whether they respond")
    sp.add_argument("-v", "--verbose", action="store_true", help="show why a display is unreachable")
    sp.set_defaults(fn=cmd_displays)

    sp = sub.add_parser("rename", help="rename a configured display, e.g. 'rename display desk'")
    sp.add_argument("old")
    sp.add_argument("new")
    sp.set_defaults(fn=cmd_rename)

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

    sub.add_parser("watch", help="keep the displays updated with live usage and overhead aircraft "
                                 "(Ctrl+C to stop)").set_defaults(fn=cmd_watch)

    sp = sub.add_parser("planes", help="aircraft overhead right now, per the adsb settings")
    sp.add_argument("--all", action="store_true", help="every tracked aircraft, nearest first")
    sp.set_defaults(fn=cmd_planes)

    sp = render_cmd("plane", cmd_plane, "show the aircraft card for the nearest plane, or QUERY")
    sp.add_argument("query", nargs="?", help="ICAO hex, callsign or registration")

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
    except (DeviceError, SourceError, AuthError, RateLimited, urllib.error.URLError,
            PermissionError, ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
