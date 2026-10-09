"""Install the Claude usage section into a Zebar widget pack (made for neosoft-zebar).

The pack is a compiled bundle, so rather than rebuilding it we copy claude-usage.js/.css
next to its index.html and add two tags to <head>, between marker comments so install is
idempotent and uninstall puts the file back exactly. Zebar also caches every response a
widget fetches (for the pack's caching.defaultDuration, a week in neosoft), which would
freeze the numbers, so install adds a no-cache rule for the tray's localhost URL to the
widget in zpack.json, and adds our files to its "includeFiles" when the widget wouldn't
serve them otherwise. Re-run after updating the pack (a marketplace update replaces it).

Packs are looked up from settings.json "startupConfigs" (what Zebar starts) in both places
Zebar keeps them: ~/.glzr/zebar for local packs, %APPDATA%\\zebar\\downloads for ones
installed from the marketplace.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath

FILES = ("claude-usage.js", "claude-usage.css")
START, END = "<!-- clockdisplay -->", "<!-- /clockdisplay -->"
_BLOCK = re.compile(rf"\n?[ \t]*{re.escape(START)}.*?{re.escape(END)}", re.S)


def zebar_dir() -> Path:
    """Zebar's settings, and packs you made or copied yourself."""
    return Path.home() / ".glzr" / "zebar"


def downloads_dir() -> Path:
    """Packs installed from the Zebar marketplace, as <author>.<name>@<version>."""
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "zebar" / "downloads"


@dataclass
class Target:
    """One widget from settings.json "startupConfigs" and where its page was found."""
    pack: str
    widget: str
    page: Path | None = None
    reason: str = ""

    @property
    def neosoft(self) -> bool:
        """Whether the page has neosoft's layout (else the section uses its fallback spot)."""
        try:
            return "tabler-icon-point-filled" in self.page.read_text(encoding="utf-8")
        except (AttributeError, OSError):
            return False


def _zpack(d: Path) -> dict | None:
    try:
        return json.loads((d / "zpack.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def installed_packs(root: Path | None = None, downloads: Path | None = None) -> dict[str, Path]:
    """Pack id -> folder, found the way Zebar finds them: every <root>/*/zpack.json is a
    custom pack whose id is its "name"; each <root>/.marketplace/<id>.json names a
    marketplace pack at <downloads>/<packId>@<version>. A marketplace pack wins a clash."""
    root, downloads = root or zebar_dir(), downloads or downloads_dir()
    packs: dict[str, Path] = {}
    if root.is_dir():
        for d in sorted(root.iterdir()):
            z = _zpack(d) if d.is_dir() else None
            if z and z.get("name"):
                packs[str(z["name"])] = d
    meta_dir = root / ".marketplace"
    if meta_dir.is_dir():
        for f in sorted(meta_dir.glob("*.json")):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                d = downloads / f"{meta['packId']}@{meta['version']}"
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if _zpack(d) is not None:
                packs[str(meta["packId"])] = d
    return packs


def resolve_startup(root: Path | None = None, downloads: Path | None = None) -> list[Target]:
    """Every widget Zebar starts, with the page to install into or why none was found."""
    root, downloads = root or zebar_dir(), downloads or downloads_dir()
    try:
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [Target("?", "?", reason=f"can't read {root / 'settings.json'}: {e}")]
    packs = installed_packs(root, downloads)
    targets: list[Target] = []
    for sc in settings.get("startupConfigs") or []:
        t = Target(str(sc.get("pack", "")), str(sc.get("widget", "")))
        targets.append(t)
        d = packs.get(t.pack)
        if d is None:
            t.reason = f"pack {t.pack!r} not found (installed: {', '.join(sorted(packs)) or 'none'})"
            continue
        w = next((w for w in (_zpack(d) or {}).get("widgets") or [] if w.get("name") == t.widget), None)
        if w is None:
            t.reason = f"{d.name} has no widget {t.widget!r}"
            continue
        page = (d / w.get("htmlPath", "index.html")).resolve()
        if page.is_file():
            t.page = page
        else:
            t.reason = f"{page} is missing"
    if not targets:
        targets.append(Target("?", "?", reason=f"no startupConfigs in {root / 'settings.json'}"))
    return targets


def startup_pages(root: Path | None = None, downloads: Path | None = None) -> list[Path]:
    """index.html of each widget Zebar starts that could be found."""
    pages: list[Path] = []
    for t in resolve_startup(root, downloads):
        if t.page and t.page not in pages:
            pages.append(t.page)
    return pages


def _local_rule(port: int) -> dict:
    return {"urlRegex": rf"^http://127\.0\.0\.1:{port}/", "duration": 0}


def _ours(rule: dict) -> bool:
    return str(rule.get("urlRegex", "")).startswith(r"^http://127\.0\.0\.1:")


def _served(rel: str, patterns: list) -> bool:
    return any(p in ("**", "**/*") or PurePosixPath(rel).match(str(p)) for p in patterns)


def _patch_zpack(page: Path, port: int | None) -> None:
    """For the widget(s) served by `page`: with a port, make Zebar serve our two files (a
    widget only serves what its "includeFiles" globs match) and add the no-cache rule for
    the tray's URL; with None, take both back out."""
    zpack_path = next((d / "zpack.json" for d in (page.parent, *page.parents)
                       if (d / "zpack.json").is_file()), None)
    if zpack_path is None:
        return
    zpack = json.loads(zpack_path.read_text(encoding="utf-8"))
    changed = False
    for w in zpack.get("widgets") or []:
        if (zpack_path.parent / w.get("htmlPath", "index.html")).resolve() != page.resolve():
            continue
        ours = [(page.parent / f).relative_to(zpack_path.parent).as_posix() for f in FILES]
        include = [f for f in w.get("includeFiles") or [] if f not in ours]
        if port is not None:
            include += [f for f in ours if not _served(f, include)]
        if include != (w.get("includeFiles") or []):
            w["includeFiles"], changed = include, True
        caching = w.setdefault("caching", {})
        rules = [r for r in caching.get("rules") or [] if not _ours(r)]
        if port is not None:
            rules.insert(0, _local_rule(port))
        if rules != caching.get("rules"):
            caching["rules"], changed = rules, True
    if changed:
        zpack_path.write_text(json.dumps(zpack, indent=2) + "\n", encoding="utf-8")


def installed(page: Path) -> bool:
    return START in page.read_text(encoding="utf-8")


def install(page: Path, port: int) -> None:
    digest = hashlib.sha1()
    for name in FILES:
        data = (resources.files("clockdisplay") / "assets" / "zebar" / name).read_bytes()
        (page.parent / name).write_bytes(data)
        digest.update(data)
    html = page.read_text(encoding="utf-8")
    backup = page.with_name(page.name + ".bak")
    if START not in html and not backup.exists():
        backup.write_text(html, encoding="utf-8")
    html = _BLOCK.sub("", html)
    v = digest.hexdigest()[:10]  # the pack caches files for a week: a new URL per change
    block = (f"    {START}\n"
             f'    <link rel="stylesheet" href="claude-usage.css?v={v}" />\n'
             f'    <script src="claude-usage.js?v={v}" data-port="{port}" defer></script>\n'
             f"    {END}\n")
    if "</head>" not in html:
        raise ValueError(f"{page}: no </head> to add the script to")
    head = html.index("</head>")
    line = html.rfind("\n", 0, head) + 1  # our lines go above the </head> line, indent intact
    page.write_text(html[:line] + block + html[line:], encoding="utf-8")
    _patch_zpack(page, port)


def uninstall(page: Path) -> bool:
    """Remove our tags and files. Returns whether anything was installed."""
    html = page.read_text(encoding="utf-8")
    was = START in html
    if was:
        page.write_text(_BLOCK.sub("", html), encoding="utf-8")
    for name in FILES:
        (page.parent / name).unlink(missing_ok=True)
    _patch_zpack(page, None)
    return was
