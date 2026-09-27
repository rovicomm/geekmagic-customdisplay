import io

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
                                  lambda: anim.blink("!")], ids=["scroll", "fill", "blink"])
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
    def __init__(self, theme=1):
        self._theme, self.calls = theme, []

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
    assert "previous_theme" not in config.load_state()
