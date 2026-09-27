import io
import json

import pytest
from PIL import Image

from clockdisplay import config
from clockdisplay.device import MAX_UPLOAD, UltraDevice, parse_filelist
from clockdisplay.display import ANIM_SLOT, STILL_SLOT, Display
from clockdisplay.render import anim, canvas, frames

FILELIST = (
    "<table id='list'><tbody><tr><th>#</th><th>Name</th><th>Size(KB)</th><th></th><th></th></tr>"
    "<tr><td>1</td><td><a href='/image/cm_main.jpg'>cm_main.jpg</a></td><td>11</td><td></td></tr>"
    "<tr><td>2</td><td><a href='/image/spaceman.gif'>spaceman.gif</a></td><td>61</td><td></td></tr>"
    "</tbody></table>"
)


def test_parse_filelist():
    files = parse_filelist(FILELIST)
    assert [(f.path, f.size_kb, f.ours) for f in files] == [
        ("/image/cm_main.jpg", 11, True),
        ("/image/spaceman.gif", 61, False),
    ]


def test_delete_refuses_foreign_files():
    with pytest.raises(PermissionError):
        UltraDevice("127.0.0.1").delete("/image/spaceman.gif")


def test_upload_size_limit():
    with pytest.raises(ValueError):
        UltraDevice("127.0.0.1").upload(b"x" * (MAX_UPLOAD + 1), "cm_big.jpg")


@pytest.mark.parametrize("img", [
    frames.solid("#d97757"),
    frames.text("Hello\\nClaude"),
    frames.meter(83, "5h", "resets in 1h"),
    frames.dual_meter(42, "in 2h", 120, "Mon 9:00 AM"),
    frames.test_pattern(),
])
def test_frames_are_240_square_jpegs(img):
    assert img.size == (240, 240)
    data = canvas.to_jpeg(img)
    assert data[:2] == b"\xff\xd8" and len(data) < MAX_UPLOAD


@pytest.mark.parametrize("make", [lambda: anim.scroll("Hi"), lambda: anim.fill(50),
                                  lambda: anim.blink("!"),
                                  lambda: anim.dual_meter_fire(42, "in 2h", 76, "Mon")],
                         ids=["scroll", "fill", "blink", "fire"])
def test_animations_are_240_square_gifs(make):
    data = make()
    assert data[:3] == b"GIF" and len(data) < MAX_UPLOAD
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (240, 240) and im.n_frames > 1


def test_gif_from_file_refits(tmp_path):
    src = tmp_path / "small.gif"
    small = [Image.new("RGB", (80, 80), c) for c in ("red", "blue")]
    small[0].save(src, save_all=True, append_images=small[1:], duration=200)
    with Image.open(io.BytesIO(anim.from_file(str(src)))) as im:
        assert im.size == (240, 240) and im.n_frames == 2


class FakeDevice:
    def __init__(self, theme=1, host="10.0.0.5"):
        self._theme, self.calls, self.host = theme, [], host

    def theme(self):
        return self._theme

    def set_theme(self, t):
        self._theme = t
        self.calls.append(("theme", t))

    def album(self):
        return {"autoplay": 0}

    def set_album(self, autoplay, interval=5):
        self.calls.append(("album", autoplay))

    def upload(self, data, name, directory):
        self.calls.append(("upload", f"{directory}/{name}"))

    def show_image(self, path):
        self.calls.append(("show", path))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOCK_CONFIG_DIR", str(tmp_path / "cfg"))


def test_display_takes_over_dedups_and_restores():
    dev = FakeDevice(theme=1)
    d = Display(dev)
    img = frames.solid("red")

    assert d.show(img) is True
    assert dev.calls == [("album", False), ("theme", 3), ("upload", STILL_SLOT), ("show", STILL_SLOT)]

    dev.calls.clear()
    assert Display(dev).show(img) is False          # same content: nothing written
    assert dev.calls == []

    assert Display(dev).show(frames.solid("blue"))  # overwrite in place: no re-select
    assert dev.calls == [("upload", STILL_SLOT)]

    dev.calls.clear()
    Display(dev).show(anim.blink("x"))              # slot switch: re-select
    assert dev.calls == [("upload", ANIM_SLOT), ("show", ANIM_SLOT)]

    assert Display(dev).restore() == 1 and dev.theme() == 1
    assert "previous_theme" not in config.load_display_state(dev.host)


def test_display_state_is_per_host():
    a, b = FakeDevice(theme=1, host="10.0.0.5"), FakeDevice(theme=4, host="10.0.0.6")
    img = frames.solid("red")
    assert Display(a).show(img) and Display(b).show(img)  # same content, different displays
    assert config.load_display_state("10.0.0.5")["previous_theme"] == 1
    assert config.load_display_state("10.0.0.6")["previous_theme"] == 4


def test_flat_state_migrates_to_default_host():
    config.save_config({"host": "10.0.0.7"})
    (config.config_dir() / "state.json").write_text('{"previous_theme": 5, "showing": "x", "hash": "h"}')
    assert config.load_display_state("10.0.0.7") == {"previous_theme": 5, "showing": "x", "hash": "h"}
    assert config.load_display_state("10.0.0.8") == {}


def test_load_displays_legacy_and_normalised(monkeypatch):
    monkeypatch.delenv("CLOCK_HOST", raising=False)
    config.save_config({"host": "10.0.0.7"})
    assert config.load_displays() == [{"name": "display", "host": "10.0.0.7", "app": "claude"}]

    config.save_config({"host": "10.0.0.7", "displays": [
        {"host": "10.0.0.8"},
        {"name": "desk", "host": "10.0.0.9", "app": "off"},
        {"name": "nohost"},
        {"name": "desk", "host": "10.0.0.10"},  # duplicate name: ignored
    ]})
    assert config.load_displays() == [
        {"name": "10.0.0.8", "host": "10.0.0.8", "app": "claude"},
        {"name": "desk", "host": "10.0.0.9", "app": "off"},
    ]
    assert config.resolve_host() == "10.0.0.8"
    assert config.find_display("desk")["host"] == "10.0.0.9"
    with pytest.raises(ValueError, match="desk"):
        config.find_display("nope")


def test_rename_display_converts_legacy_config(monkeypatch):
    monkeypatch.delenv("CLOCK_HOST", raising=False)
    config.save_config({"host": "10.0.0.7", "poll_interval": 90})
    config.rename_display("display", " desk ")
    raw = json.loads(config.config_file().read_text())
    assert raw["displays"] == [{"name": "desk", "host": "10.0.0.7", "app": "claude"}]
    assert "host" not in raw and raw["poll_interval"] == 90
    assert config.resolve_host() == "10.0.0.7"


def test_rename_display_rejects_bad_names():
    config.save_config({"displays": [{"name": "a", "host": "h1"}, {"host": "h2"}]})
    config.rename_display("h2", "b")  # unnamed entries are addressed by host
    assert [d["name"] for d in config.load_displays()] == ["a", "b"]
    for old, new in [("a", "b"), ("a", "  "), ("a", "all"), ("zzz", "c")]:
        with pytest.raises(ValueError):
            config.rename_display(old, new)


def test_identify_frame():
    img = frames.identify("desk", "192.168.1.50")
    assert img.size == (240, 240)
