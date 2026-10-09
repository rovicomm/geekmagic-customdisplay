"""Install the Claude usage section into a Zebar widget pack (made for neosoft-zebar).

The pack is a compiled bundle, so rather than rebuilding it we copy claude-usage.js/.css
next to its index.html and add two tags to <head>, between marker comments so install is
idempotent and uninstall puts the file back exactly. Zebar also caches every response a
widget fetches (for the pack's caching.defaultDuration, a week in neosoft), which would
freeze the numbers, so install adds a no-cache rule for the tray's localhost URL to the
widget in zpack.json. Re-run after updating the pack.
"""
from __future__ import annotations

import hashlib
import json
import re
from importlib import resources
from pathlib import Path

FILES = ("claude-usage.js", "claude-usage.css")
START, END = "<!-- clockdisplay -->", "<!-- /clockdisplay -->"
_BLOCK = re.compile(rf"\n?[ \t]*{re.escape(START)}.*?{re.escape(END)}", re.S)


def zebar_dir() -> Path:
    return Path.home() / ".glzr" / "zebar"


def startup_pages(root: Path | None = None) -> list[Path]:
    """index.html of each widget Zebar starts (settings.json "startupConfigs")."""
    root = root or zebar_dir()
    try:
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    pages: list[Path] = []
    for sc in settings.get("startupConfigs") or []:
        pack = str(sc.get("pack", "")).split("/")[-1]
        for d in sorted(root.glob(f"*{pack}*")):
            try:
                zpack = json.loads((d / "zpack.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if zpack.get("name") != pack:
                continue
            for w in zpack.get("widgets") or []:
                if w.get("name") == sc.get("widget"):
                    page = (d / w.get("htmlPath", "index.html")).resolve()
                    if page.is_file() and page not in pages:
                        pages.append(page)
    return pages


def _local_rule(port: int) -> dict:
    return {"urlRegex": rf"^http://127\.0\.0\.1:{port}/", "duration": 0}


def _ours(rule: dict) -> bool:
    return str(rule.get("urlRegex", "")).startswith(r"^http://127\.0\.0\.1:")


def _set_cache_rule(page: Path, port: int | None) -> None:
    """Add (port) or remove (None) our no-cache rule on the widget(s) served by `page`."""
    zpack_path = next((d / "zpack.json" for d in (page.parent, *page.parents)
                       if (d / "zpack.json").is_file()), None)
    if zpack_path is None:
        return
    zpack = json.loads(zpack_path.read_text(encoding="utf-8"))
    changed = False
    for w in zpack.get("widgets") or []:
        if (zpack_path.parent / w.get("htmlPath", "index.html")).resolve() != page.resolve():
            continue
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
    _set_cache_rule(page, port)


def uninstall(page: Path) -> bool:
    """Remove our tags and files. Returns whether anything was installed."""
    html = page.read_text(encoding="utf-8")
    was = START in html
    if was:
        page.write_text(_BLOCK.sub("", html), encoding="utf-8")
    for name in FILES:
        (page.parent / name).unlink(missing_ok=True)
    _set_cache_rule(page, None)
    return was
