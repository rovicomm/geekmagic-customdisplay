import io
import json
import threading

import pytest
from PIL import Image

from clockdisplay import config
from clockdisplay.adsb import card, source
from clockdisplay.adsb.photos import PhotoCache
from clockdisplay.adsb.source import Aircraft
from clockdisplay.adsb.spotter import Spotter
from clockdisplay.claude.usage import Usage
from clockdisplay.device import DeviceError
from clockdisplay.meter import Meter

HOME = (51.470, -0.454)  # any fixed point; a public airport


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOCK_CONFIG_DIR", str(tmp_path / "cfg"))


def plane(hex_code="abc123", distance=1.0, altitude=3000, **kw) -> Aircraft:
    return Aircraft(hex=hex_code, callsign=kw.pop("callsign", "BAW123"), altitude=altitude,
                    distance=distance, bearing=kw.pop("bearing", 90.0), **kw)


def settings(**kw) -> dict:
    return {**config.ADSB_DEFAULTS, "url": "http://rx", **kw}


# --- source ------------------------------------------------------------------------

def test_parse_aircraft_readsb_record():
    ac = source.parse_aircraft({
        "hex": "~400ABC", "flight": "BAW117 ", "r": "G-0DMO", "t": "A321",
        "desc": "AIRBUS  A-321", "ownOp": "EXAMPLE AIRWAYS", "alt_baro": 7575, "gs": 262.4,
        "baro_rate": -1472, "squawk": "4051", "lat": 51.51, "lon": -0.35, "dbFlags": 1})
    assert (ac.hex, ac.callsign, ac.registration, ac.type_code) == ("400abc", "BAW117", "G-0DMO", "A321")
    assert ac.description == "AIRBUS A-321" and ac.altitude == 7575 and ac.vertical_rate == -1472
    assert ac.military and not ac.on_ground


def test_parse_aircraft_on_ground_and_sparse():
    ac = source.parse_aircraft({"hex": "a1b2c3", "alt_baro": "ground"})
    assert ac.on_ground and ac.altitude == 0 and ac.name == "A1B2C3"
    assert source.parse_aircraft({"hex": "a1b2c3", "r": "N1"}).name == "N1"


def test_distance_bearing():
    nm, brg = source.distance_bearing(51.0, -1.0, 52.0, -1.0)  # one degree of latitude due north
    assert nm == pytest.approx(60.0, abs=0.2) and brg == pytest.approx(0.0, abs=0.01)
    _, east = source.distance_bearing(51.0, -1.0, 51.0, 0.0)
    assert east == pytest.approx(90.0, abs=0.5)


def test_fetch_aircraft_measures_from_location_and_drops_stale(monkeypatch):
    data = {"aircraft": [
        {"hex": "aaa111", "lat": HOME[0] + 0.05, "lon": HOME[1], "seen_pos": 1},
        {"hex": "bbb222", "lat": HOME[0], "lon": HOME[1], "seen_pos": 300},  # stale position
        {"hex": "ccc333"},                                                   # no position
        {"flight": "NOHEX"},
    ]}
    monkeypatch.setattr(source, "_get_json", lambda url: data)
    out = {ac.hex: ac for ac in source.fetch_aircraft("rx:8080", HOME)}
    assert set(out) == {"aaa111", "bbb222", "ccc333"}
    assert out["aaa111"].distance == pytest.approx(3.0, abs=0.05) and out["aaa111"].direction == "N"
    assert out["bbb222"].distance is None and out["ccc333"].distance is None


@pytest.mark.parametrize("ac,s,ok", [
    (plane(distance=4.9), {}, True),
    (plane(distance=5.1), {}, False),
    (plane(distance=None), {}, False),
    (plane(altitude=None), {}, False),
    (plane(altitude=12000), {"max_altitude": 10000}, False),
    (plane(altitude=12000), {"max_altitude": 0}, True),
    (plane(altitude=500), {"min_altitude": 1000}, False),
    (plane(on_ground=True, altitude=0), {}, False),
    (plane(on_ground=True, altitude=0), {"include_ground": True}, True),
    (plane(type_code="B744"), {"types": ["b74", "A38"]}, True),
    (plane(type_code="C172"), {"types": ["B74"]}, False),
    (plane(callsign="DAL12"), {"callsigns": ["BAW"]}, False),
    (plane(), {"military_only": True}, False),
])
def test_matches_filters(ac, s, ok):
    assert source.matches(ac, settings(**s)) is ok


def test_overhead_sorts_nearest_first():
    far, near, out = plane("far", 4.0), plane("near", 0.5), plane("out", 9.0)
    assert [ac.hex for ac in source.overhead([far, out, near], settings())] == ["near", "far"]


# --- photos and card ----------------------------------------------------------------

def _jpeg(size=(420, 280)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 90, 200)).save(buf, "JPEG")
    return buf.getvalue()


def fake_api(calls):
    def get(url):
        calls.append(url)
        if url.endswith("/hex/abc123"):
            return json.dumps({"photos": [{"thumbnail_large": {"src": "https://img/1.jpg"},
                                           "photographer": "A. Spotter"}]}).encode()
        if url.endswith("/hex/000000"):
            return b'{"photos": []}'
        return _jpeg()
    return get


def test_photo_cache_parses_and_caches_hits_and_misses(tmp_path):
    calls = []
    pc = PhotoCache(fake_api(calls), tmp_path)
    p = pc.lookup("ABC123")
    assert p.photographer == "A. Spotter" and p.image.size == (420, 280)
    assert pc.lookup("abc123") is p and pc.lookup("000000") is None and pc.lookup("000000") is None
    assert len(calls) == 3  # api + image for the hit, api once for the miss


def test_photo_cache_survives_restart_on_disk(tmp_path):
    calls = []
    PhotoCache(fake_api(calls), tmp_path).lookup("abc123")
    PhotoCache(fake_api(calls), tmp_path).lookup("000000")
    assert (tmp_path / "abc123.jpg").is_file() and len(calls) == 3
    fresh = PhotoCache(fake_api(calls), tmp_path)  # a new process: memory cache is empty
    p = fresh.lookup("abc123")
    assert p.photographer == "A. Spotter" and p.image.size == (420, 280)
    assert fresh.lookup("000000") is None and len(calls) == 3  # both answered from disk


def test_photo_cache_rechecks_misses_after_ttl(tmp_path):
    from clockdisplay.adsb import photos
    calls, now = [], [0.0]
    PhotoCache(fake_api(calls), tmp_path, clock=lambda: now[0]).lookup("000000")
    now[0] = photos.MISS_TTL - 1
    PhotoCache(fake_api(calls), tmp_path, clock=lambda: now[0]).lookup("000000")
    assert len(calls) == 1
    now[0] = photos.MISS_TTL + 1
    PhotoCache(fake_api(calls), tmp_path, clock=lambda: now[0]).lookup("000000")
    assert len(calls) == 2


def test_photo_cache_prunes_least_recently_used(tmp_path, monkeypatch):
    import os
    from clockdisplay.adsb import photos
    monkeypatch.setattr(photos, "MAX_PHOTOS", 2)
    for i, name in enumerate(("old", "mid")):
        (tmp_path / f"{name}.jpg").write_bytes(_jpeg())
        (tmp_path / f"{name}.json").write_text('{"photo": true}')
        os.utime(tmp_path / f"{name}.jpg", (i, i))
    PhotoCache(fake_api([]), tmp_path).lookup("abc123")
    assert sorted(p.name for p in tmp_path.glob("*.jpg")) == ["abc123.jpg", "mid.jpg"]
    assert not (tmp_path / "old.json").exists()


def test_photo_cache_does_not_cache_failures(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        raise OSError("offline")

    pc = PhotoCache(get, tmp_path)
    assert pc.lookup("abc123") is None and pc.lookup("abc123") is None and len(calls) == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fields,photo", [
    (config.ADSB_DEFAULTS["fields"], True),
    (config.ADSB_DEFAULTS["fields"], False),
    (config.ADSB_FIELDS, True),
    (["callsign"], False),
    ([], False),
])
def test_card_renders_240_square(fields, photo):
    from clockdisplay.adsb.photos import Photo
    ac = plane(registration="G-0DMO", type_code="A321", description="AIRBUS A-321 with a very long name",
               operator="SOME VERY LONG OPERATOR NAME LIMITED", speed=250.0, vertical_rate=-1500,
               squawk="7700")
    img = card.render(ac, fields, Photo(Image.open(io.BytesIO(_jpeg())), "A. Spotter") if photo else None)
    assert img.size == (240, 240) and img.mode == "RGB"


def test_altitude_text():
    assert card.altitude_text(plane(altitude=36000)) == "36,000 ft"
    assert card.altitude_text(plane(on_ground=True, altitude=0)) == "ground"


# --- spotter ------------------------------------------------------------------------

class FakeDisplay:
    def __init__(self, log, host, fail=False):
        self.log, self.host, self.fail = log, host, fail

    def show(self, content, force=False):
        if self.fail:
            raise DeviceError("timed out")
        self.log.append(("show", self.host))
        return True

    def restore(self):
        self.log.append(("restore", self.host))


class NoPhotos:
    def lookup(self, hex_code):
        return None


def _join_popups():
    for th in threading.enumerate():
        if th.name.startswith("popup-"):
            th.join(2)


def make_spotter(sky, displays, down=(), **adsb):
    config.save_config({"displays": displays,
                        "adsb": {"url": "http://rx", "location": list(HOME), "popup_seconds": 0, **adsb}})
    log, now = [], [0.0]
    meter = Meter(display_factory=lambda h: FakeDisplay(log, h, fail=h in down), clock=lambda: now[0])
    sp = Spotter(meter, fetch=lambda url, loc: list(sky), receiver=lambda url: None,
                 photos=NoPhotos(), clock=lambda: now[0])
    return sp, log, now


def test_popup_over_claude_displays_once_per_cooldown():
    sky = [plane("aaa111", 2.0)]
    sp, log, now = make_spotter(sky, [{"name": "desk", "host": "h1"},
                                      {"name": "shelf", "host": "h2", "adsb_popup": False},
                                      {"name": "spare", "host": "h3", "app": "off"}])
    sp.meter.last = Usage(40, None, 10, None)
    stop = threading.Event()
    sp.tick(stop); _join_popups()
    # only the claude display that allows pop-ups: the plane, then the last usage card straight back
    assert log == [("show", "h1")] * 2
    assert sp.meter.targets["desk"].pushed_key == (40, 10, False)
    now[0] = 60; sp.tick(stop); _join_popups()
    assert len(log) == 2                        # same plane: cooldown
    now[0] = 2000; sp.tick(stop); _join_popups()
    assert log == [("show", "h1")] * 4          # cooldown over


def test_popup_before_any_usage_falls_back_to_clock():
    sp, log, _ = make_spotter([plane()], [{"name": "desk", "host": "h1"}])
    sp.tick(threading.Event()); _join_popups()
    assert log == [("show", "h1"), ("restore", "h1")]
    assert sp.meter.targets["desk"].pushed_key is None and sp.meter.wake.is_set()


def test_popup_waits_for_held_display_and_takes_planes_in_turn():
    sky = [plane("aaa111", 1.0), plane("bbb222", 3.0)]
    sp, log, now = make_spotter(sky, [{"name": "desk", "host": "h1"}])
    stop = threading.Event()
    sp.meter.sync_targets()
    sp.meter.targets["desk"].hold = True        # e.g. Identify is showing
    sp.tick(stop)
    assert log == [] and sp.popped == {}
    sp.meter.targets["desk"].hold = False
    sp.tick(stop); _join_popups()
    sp.tick(stop); _join_popups()
    sp.tick(stop); _join_popups()
    assert log == [("show", "h1"), ("restore", "h1")] * 2 and set(sp.popped) == {"aaa111", "bbb222"}


def test_popup_respects_pause_and_popup_setting():
    sp, log, _ = make_spotter([plane()], [{"name": "desk", "host": "h1"}], popup=False)
    sp.tick(threading.Event()); _join_popups()
    sp.meter.paused = True
    config.set_adsb("popup", True)
    sp.tick(threading.Event()); _join_popups()
    assert log == [] and sp.nearby


def test_adsb_display_follows_nearest_and_restores_when_empty():
    sky = [plane("aaa111", 2.0)]
    sp, log, now = make_spotter(sky, [{"name": "sky", "host": "h1", "app": "adsb"}], refresh=20)
    stop = threading.Event()
    sp.tick(stop)
    now[0] = 5; sp.tick(stop)                   # same plane, within refresh: no push
    now[0] = 25; sp.tick(stop)                  # refresh elapsed
    sky[:] = [plane("bbb222", 1.0), plane("aaa111", 2.0)]
    now[0] = 30; sp.tick(stop)                  # nearer plane: push at once
    sky.clear()
    now[0] = 35; sp.tick(stop)
    now[0] = 40; sp.tick(stop)
    assert log == [("show", "h1")] * 3 + [("restore", "h1")]
    assert sp.meter.targets["sky"].status == "ok"


def test_unreachable_display_is_reported():
    sp, _, _ = make_spotter([plane()], [{"name": "sky", "host": "h1", "app": "adsb"}], down={"h1"})
    sp.tick(threading.Event())
    assert sp.meter.targets["sky"].status == "unreachable"


def test_disabled_or_unconfigured_does_nothing():
    sp, log, _ = make_spotter([plane()], [{"name": "desk", "host": "h1"}], enabled=False)
    sp.tick(threading.Event())
    assert sp.nearby == [] and log == [] and sp.status() == "Off"
    config.save_config({"adsb": {"url": ""}})
    sp.tick(threading.Event())
    assert sp.status() == "No receiver URL set"


def test_receiver_position_is_used_and_required():
    config.save_config({"adsb": {"url": "http://rx"}})
    seen = []
    meter = Meter(display_factory=lambda h: FakeDisplay([], h))
    sp = Spotter(meter, fetch=lambda url, loc: seen.append(loc) or [], receiver=lambda url: HOME,
                 photos=NoPhotos())
    sp.refresh()
    assert seen == [HOME]
    sp2 = Spotter(meter, fetch=lambda url, loc: [], receiver=lambda url: None, photos=NoPhotos())
    with pytest.raises(source.SourceError, match="location"):
        sp2.refresh()


def test_show_nearest_ignores_filters_and_cooldown():
    far = plane("far", 40.0)
    sp, log, _ = make_spotter([far], [{"name": "desk", "host": "h1"},
                                      {"name": "sky", "host": "h2", "app": "adsb"}])
    assert sp.show_nearest(threading.Event()) is far
    _join_popups()
    assert sorted(log) == [("restore", "h1"), ("restore", "h2"), ("show", "h1"), ("show", "h2")]


def test_set_adsb_keeps_other_settings():
    config.save_config({"displays": [{"name": "desk", "host": "h1"}], "adsb": {"url": "http://rx"}})
    config.set_adsb("radius", 2)
    cfg = config.load_config()
    assert cfg["adsb"]["radius"] == 2 and cfg["adsb"]["url"] == "http://rx"
    assert cfg["adsb"]["popup_seconds"] == 30 and cfg["displays"][0]["name"] == "desk"
