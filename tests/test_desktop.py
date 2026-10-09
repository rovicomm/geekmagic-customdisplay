import datetime as dt
import io
import json
import urllib.request

import pytest
from PIL import Image

from clockdisplay import config
from clockdisplay.claude.usage import Usage
from clockdisplay.desktop import server, zebar
from clockdisplay.desktop.virtual import VirtualDevice
from clockdisplay.display import ANIM_SLOT, RESET_SLOT, STILL_SLOT, Display
from clockdisplay.meter import Meter
from clockdisplay.render import anim, frames

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOCK_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("CLOCK_HOST", raising=False)


def _watched() -> tuple[VirtualDevice, list]:
    dev, seen = VirtualDevice(), []
    dev.listener = seen.append
    return dev, seen


# --- virtual device ------------------------------------------------------------------

def test_virtual_device_shows_still_then_gif_then_clock():
    dev, seen = _watched()
    d = Display(dev)
    assert d.show(frames.dual_meter(42, "in 2h", 76, "Mon"))
    assert dev.theme() == 3 and seen[-1][:2] == b"\xff\xd8" and STILL_SLOT in dev.files
    assert not d.show(frames.dual_meter(42, "in 2h", 76, "Mon"))  # unchanged: skipped
    d.show(anim.dual_meter_fire(42, "in 2h", 76, "Mon"))
    assert seen[-1][:3] == b"GIF" and dev.current() == dev.files[ANIM_SLOT]
    assert d.restore() == 1  # back to the clock theme it "had"
    assert seen[-1] is None and dev.current() is None


def test_virtual_device_overwriting_on_screen_file_refreshes():
    dev, seen = _watched()
    d = Display(dev)
    d.show(frames.solid("red"))
    n = len(seen)
    d.show(frames.solid("blue"))  # same slot: an upload alone refreshes the screen
    assert len(seen) > n and Image.open(io.BytesIO(seen[-1])).getpixel((5, 5))[2] > 200


def test_virtual_device_keeps_reset_gifs_and_refuses_foreign_deletes():
    dev, _ = _watched()
    d = Display(dev)
    path, data = RESET_SLOT.format("5h"), anim.reset_gif("5h")
    d.show_file(path, data)
    assert dev.current() == data
    assert not d.store(path, data)  # already there
    assert path in [f.path for f in dev.list_files("/image")]
    with pytest.raises(PermissionError):
        dev.delete("/image/spaceman.gif")


# --- desktop window as a display -----------------------------------------------------------

def test_window_toggle_adds_and_removes_display():
    config.save_config({"displays": [{"name": "desk", "host": "10.0.0.5"}]})
    config.set_window_enabled(True)
    config.set_window_enabled(True)  # idempotent
    assert config.window_enabled()
    assert [d["host"] for d in config.load_displays()] == ["10.0.0.5", config.WINDOW_HOST]
    config.set_window_enabled(False)
    assert [d["host"] for d in config.load_displays()] == ["10.0.0.5"]


def test_window_toggle_on_fresh_config_is_the_only_display():
    config.set_window_enabled(True)
    assert config.load_displays() == [{**config.WINDOW_DISPLAY}]


def test_window_name_avoids_clash():
    config.save_config({"displays": [{"name": "Desktop", "host": "10.0.0.5"}]})
    config.set_window_enabled(True)
    assert [d["name"] for d in config.load_displays()] == ["Desktop", "Desktop 2"]


def test_meter_pushes_to_window_through_default_factory(monkeypatch):
    from clockdisplay.desktop import virtual
    dev, seen = _watched()
    monkeypatch.setattr(virtual, "_device", dev)
    config.set_window_enabled(True)
    m = Meter(fetch=lambda: Usage(40, None, 10, None))
    m.tick()
    assert seen and seen[-1][:2] == b"\xff\xd8"
    assert m.targets["Desktop"].status == "ok"


def test_meter_on_reset_fires_even_without_displays():
    config.save_config({"displays": []})
    t = NOW
    it, now = iter([Usage(90, t, 50, None), Usage(0, t + dt.timedelta(hours=5), 50, None)]), [NOW.timestamp()]
    m = Meter(fetch=lambda: next(it), clock=lambda: now[0])
    got = []
    m.on_reset = got.append
    m.tick()
    now[0] += 60
    m.tick()
    assert got == [["5h"]]


# --- bar server --------------------------------------------------------------------------

@pytest.fixture
def bar():
    state = server.BarState()
    httpd = server.serve(state, 0)
    yield state, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_bar_server_hides_when_disabled_and_serves_usage(bar):
    state, base = bar
    state.update(Usage(42, None, 95, None), on_fire=True, burn_rate=50.0)
    with urllib.request.urlopen(base + "/usage") as r:
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        assert json.load(r) == {"enabled": False, "need_anchor": True}
    state.enabled = True
    with urllib.request.urlopen(base + "/usage") as r:
        u = json.load(r)
    assert (u["five_pct"], u["week_pct"], u["on_fire"], u["color7"]) == (42, 95, True, "#d93a3a")


def test_bar_server_refuses_a_busy_port(bar):
    _, base = bar
    with pytest.raises(OSError):
        server.serve(server.BarState(), int(base.rsplit(":", 1)[1]))


def test_bar_server_fire_png_is_animated(bar):
    _, base = bar
    with urllib.request.urlopen(base + "/fire.png") as r:
        data = r.read()
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "PNG" and im.n_frames > 1 and im.mode == "RGBA"


def test_bar_server_anchor_prefers_primary_monitor(bar):
    state, base = bar

    def post(**a):
        req = urllib.request.Request(base + "/anchor", json.dumps(a).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req).close()

    post(x=3000, y=0, w=100, h=40, primary=False)
    assert state.anchor["x"] == 3000
    post(x=2200, y=5, w=160, h=40, primary=True)
    post(x=3100, y=0, w=100, h=40, primary=False)  # another monitor's bar: ignored
    assert state.anchor == {"x": 2200, "y": 5, "w": 160, "h": 40, "primary": True}


# --- Zebar installer ----------------------------------------------------------------------

PAGE = "<!doctype html>\n<html>\n  <head>\n    <link rel=\"stylesheet\" href=\"config.css\" />\n  </head>\n  <body></body>\n</html>\n"


def _pack(root):
    pack = root / "blaiyz.neosoft-zebar@1.2.6-me"
    pack.mkdir(parents=True)
    (pack / "index.html").write_text(PAGE, encoding="utf-8")
    (pack / "zpack.json").write_text(json.dumps(
        {"name": "neosoft-zebar", "widgets": [{"name": "default", "htmlPath": "./index.html"}]}))
    (root / "settings.json").write_text(json.dumps(
        {"startupConfigs": [{"pack": "neosoft-zebar", "widget": "default", "preset": "me"}]}))
    return pack


def test_zebar_install_is_idempotent_and_uninstall_restores(tmp_path):
    pack = _pack(tmp_path)
    [page] = zebar.startup_pages(tmp_path)
    zebar.install(page, 47815)
    zebar.install(page, 47815)
    html = page.read_text(encoding="utf-8")
    assert html.count(zebar.START) == 1 and 'data-port="47815"' in html
    assert html.index(zebar.END) < html.index("</head>")
    assert (pack / "claude-usage.js").is_file() and (pack / "claude-usage.css").is_file()
    assert zebar.installed(page)
    [w] = json.loads((pack / "zpack.json").read_text())["widgets"]
    assert w["caching"]["rules"] == [{"urlRegex": r"^http://127\.0\.0\.1:47815/", "duration": 0}]
    assert zebar.uninstall(page)
    [w] = json.loads((pack / "zpack.json").read_text())["widgets"]
    assert w["caching"]["rules"] == []
    assert page.read_text(encoding="utf-8") == PAGE
    assert not (pack / "claude-usage.js").exists()
    assert not zebar.uninstall(page)


def _marketplace(root, downloads, pack_id, version, widget="default", html="index.html"):
    """A pack installed the way the Zebar marketplace does it."""
    d = downloads / f"{pack_id}@{version}"
    d.mkdir(parents=True)
    (d / html).write_text(PAGE, encoding="utf-8")
    (d / "zpack.json").write_text(json.dumps(
        {"name": pack_id.split(".", 1)[1], "widgets": [{"name": widget, "htmlPath": f"./{html}"}]}))
    (root / ".marketplace").mkdir(parents=True, exist_ok=True)
    (root / ".marketplace" / f"{pack_id}.json").write_text(json.dumps({"packId": pack_id, "version": version}))
    return d


def _startup(root, *configs):
    root.mkdir(parents=True, exist_ok=True)
    (root / "settings.json").write_text(json.dumps(
        {"startupConfigs": [{"pack": p, "widget": w, "preset": "default"} for p, w in configs]}))


def test_zebar_finds_marketplace_pack_at_its_installed_version(tmp_path):
    root, downloads = tmp_path / "glzr", tmp_path / "downloads"
    _marketplace(root, downloads, "blaiyz.neosoft-zebar", "1.2.6")
    (downloads / "blaiyz.neosoft-zebar@1.2.5").mkdir()  # an old download Zebar no longer uses
    _startup(root, ("blaiyz.neosoft-zebar", "default"))
    [t] = zebar.resolve_startup(root, downloads)
    assert t.page == (downloads / "blaiyz.neosoft-zebar@1.2.6" / "index.html").resolve()


def test_zebar_local_pack_id_is_its_name_even_beside_a_marketplace_copy(tmp_path):
    root, downloads = tmp_path / "glzr", tmp_path / "downloads"
    local = _pack(root)  # id "neosoft-zebar"
    _marketplace(root, downloads, "blaiyz.neosoft-zebar", "1.2.6")
    _startup(root, ("neosoft-zebar", "default"))
    assert zebar.startup_pages(root, downloads) == [(local / "index.html").resolve()]


def test_zebar_explains_what_it_could_not_find(tmp_path):
    root, downloads = tmp_path / "glzr", tmp_path / "downloads"
    _marketplace(root, downloads, "glzr-io.starter", "0.0.0", widget="with-glazewm", html="with-glazewm.html")
    _startup(root, ("someone.missing", "bar"), ("glzr-io.starter", "vanilla"), ("glzr-io.starter", "with-glazewm"))
    missing, no_widget, ok = zebar.resolve_startup(root, downloads)
    assert missing.page is None and "someone.missing" in missing.reason and "glzr-io.starter" in missing.reason
    assert no_widget.page is None and "vanilla" in no_widget.reason
    assert ok.page.name == "with-glazewm.html" and not ok.neosoft


def test_zebar_install_round_trips_on_marketplace_pack(tmp_path):
    root, downloads = tmp_path / "glzr", tmp_path / "downloads"
    d = _marketplace(root, downloads, "blaiyz.neosoft-zebar", "1.2.6")
    _startup(root, ("blaiyz.neosoft-zebar", "default"))
    [page] = zebar.startup_pages(root, downloads)
    zebar.install(page, 5000)
    assert zebar.installed(page) and (d / "claude-usage.js").is_file()
    [w] = json.loads((d / "zpack.json").read_text())["widgets"]
    assert w["caching"]["rules"][0]["urlRegex"].endswith(":5000/")
    assert zebar.uninstall(page) and page.read_text(encoding="utf-8") == PAGE


def test_zebar_without_settings_says_so(tmp_path):
    [t] = zebar.resolve_startup(tmp_path / "nowhere", tmp_path / "downloads")
    assert t.page is None and "settings.json" in t.reason


def test_zebar_install_makes_widget_serve_our_files(tmp_path):
    root, downloads = tmp_path / "glzr", tmp_path / "downloads"
    d = _marketplace(root, downloads, "glzr-io.starter", "0.0.0")
    zpack = json.loads((d / "zpack.json").read_text())
    zpack["widgets"][0]["includeFiles"] = ["*.html", "*.css"]  # what the starter pack ships
    (d / "zpack.json").write_text(json.dumps(zpack))
    _startup(root, ("glzr-io.starter", "default"))
    [page] = zebar.startup_pages(root, downloads)

    def include():
        return json.loads((d / "zpack.json").read_text())["widgets"][0]["includeFiles"]

    zebar.install(page, 47815)
    zebar.install(page, 47815)
    assert include() == ["*.html", "*.css", "claude-usage.js"]  # the .css was already served
    zebar.uninstall(page)
    assert include() == ["*.html", "*.css"]
