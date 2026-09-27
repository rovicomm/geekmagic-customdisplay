import datetime as dt
import io
import json
import time

import pytest

from clockdisplay import config
from clockdisplay.claude import auth, usage
from clockdisplay.claude.usage import Usage, format_reset, parse
from clockdisplay.device import DeviceError
from clockdisplay.meter import Meter

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOCK_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    store: dict = {}
    monkeypatch.setattr(auth.keyring, "get_password", lambda s, u: store.get((s, u)))
    monkeypatch.setattr(auth.keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(auth.keyring, "delete_password", lambda s, u: store.pop((s, u), None))
    return store


# --- usage parsing -------------------------------------------------------------

def test_parse_usage_response():
    u = parse({"five_hour": {"utilization": 42.0, "resets_at": "2026-09-26T14:15:00+00:00"},
               "seven_day": {"utilization": 76, "resets_at": "2026-09-28T09:00:00Z"},
               "seven_day_opus": None})
    assert (u.five_pct, u.week_pct) == (42.0, 76.0)
    assert u.five_resets_at == dt.datetime(2026, 9, 26, 14, 15, tzinfo=UTC)
    assert u.week_resets_at.tzinfo is not None


def test_parse_handles_missing_sections():
    u = parse({"five_hour": None})
    assert (u.five_pct, u.week_pct, u.five_reset) == (0.0, 0.0, "unknown")


@pytest.mark.parametrize("delta,expected", [
    (dt.timedelta(minutes=-5), "soon"),
    (dt.timedelta(seconds=20), "in 1m"),
    (dt.timedelta(minutes=45, seconds=30), "in 45m"),
    (dt.timedelta(hours=2, minutes=15), "in 2h 15m"),
    (dt.timedelta(hours=3), "in 3h"),
])
def test_format_reset_countdowns(delta, expected):
    assert format_reset(NOW + delta, now=NOW) == expected


def test_format_reset_days_uses_local_weekday_and_12h_time():
    when = NOW + dt.timedelta(days=2, hours=1)
    local = when.astimezone()
    assert format_reset(when, now=NOW) == f"{local:%a} {local.hour % 12 or 12}:{local:%M %p}"


# --- auth ------------------------------------------------------------------------

def write_creds(tmp_path, access="file-access", refresh="file-refresh", expires_in=3600):
    d = tmp_path / "claude"
    d.mkdir(exist_ok=True)
    creds = {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                               "expiresAt": int((time.time() + expires_in) * 1000),
                               "scopes": ["user:inference"], "subscriptionType": "pro"},
             "mcpOAuth": {"x": {"accessToken": "keep-me"}}}
    (d / ".credentials.json").write_text(json.dumps(creds))
    return d / ".credentials.json"


def test_cached_token_wins(tmp_path, isolated):
    write_creds(tmp_path)
    isolated[(auth.KEYRING_SERVICE, auth.KEYRING_USER)] = json.dumps(
        {"access_token": "cached", "refresh_token": "r", "expires_at": time.time() + 3600})
    assert auth.get_access_token() == "cached"


def test_expired_cache_falls_back_to_file_and_caches_it(tmp_path, isolated):
    write_creds(tmp_path)
    isolated[(auth.KEYRING_SERVICE, auth.KEYRING_USER)] = json.dumps(
        {"access_token": "stale", "refresh_token": "r", "expires_at": time.time() - 10})
    assert auth.get_access_token() == "file-access"
    assert json.loads(isolated[(auth.KEYRING_SERVICE, auth.KEYRING_USER)])["access_token"] == "file-access"


def test_expired_everything_refreshes_and_writes_back(tmp_path, isolated, monkeypatch):
    path = write_creds(tmp_path, expires_in=-100)
    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(json.loads(req.data))
        return io.BytesIO(json.dumps({"access_token": "new-access", "refresh_token": "new-refresh",
                                      "expires_in": 28800}).encode())

    monkeypatch.setattr(auth.urllib.request, "urlopen", fake_urlopen)
    assert auth.get_access_token() == "new-access"
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "file-refresh"

    creds = json.loads(path.read_text())
    oauth = creds["claudeAiOauth"]
    assert (oauth["accessToken"], oauth["refreshToken"]) == ("new-access", "new-refresh")
    assert oauth["expiresAt"] > time.time() * 1000 and oauth["subscriptionType"] == "pro"
    assert creds["mcpOAuth"] == {"x": {"accessToken": "keep-me"}}  # other fields untouched
    assert json.loads(isolated[(auth.KEYRING_SERVICE, auth.KEYRING_USER)])["refresh_token"] == "new-refresh"


def test_not_signed_in(tmp_path):
    with pytest.raises(auth.AuthError):
        auth.get_access_token()
    assert auth.status()["source"] == "not signed in"


def test_no_tokens_in_config_dir(tmp_path):
    write_creds(tmp_path)
    auth.get_access_token()
    cfg = tmp_path / "cfg"
    assert not cfg.exists() or "file-access" not in "".join(
        p.read_text() for p in cfg.rglob("*") if p.is_file())


def test_fetch_reauths_once_on_401(monkeypatch):
    tokens = iter(["old", "new"])
    monkeypatch.setattr(usage.auth, "get_access_token", lambda: next(tokens))
    invalidated = []
    monkeypatch.setattr(usage.auth, "invalidate", lambda: invalidated.append(1))

    def fake_get(token):
        if token == "old":
            raise usage.urllib.error.HTTPError(usage.USAGE_URL, 401, "no", {}, None)
        return {"five_hour": {"utilization": 5}}

    monkeypatch.setattr(usage, "_get", fake_get)
    assert usage.fetch_usage().five_pct == 5 and invalidated == [1]


def test_fetch_raises_rate_limited(monkeypatch):
    monkeypatch.setattr(usage.auth, "get_access_token", lambda: "t")

    def fake_get(token):
        raise usage.urllib.error.HTTPError(usage.USAGE_URL, 429, "slow", {"Retry-After": "120"}, None)

    monkeypatch.setattr(usage, "_get", fake_get)
    with pytest.raises(usage.RateLimited) as e:
        usage.fetch_usage()
    assert e.value.retry_after == 120


# --- config ----------------------------------------------------------------------

def test_config_dir_uses_localappdata_on_windows(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOCK_CONFIG_DIR")
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "config.json").write_text('{"host": "10.0.0.9"}')
    monkeypatch.setattr(config, "_LEGACY_DIR", legacy)
    monkeypatch.setattr(config, "_migrated", False)

    assert config.config_dir() == tmp_path / "lad" / "clockdisplay"
    assert config.resolve_host() == "10.0.0.9"  # migrated from the old location


def test_load_config_defaults():
    cfg = config.load_config()
    assert cfg["poll_interval"] == 60 and cfg["autopush"] is True and cfg["host"]


# --- meter -----------------------------------------------------------------------

class OnShow:
    """Display stand-in that calls `fn()` on every push."""
    def __init__(self, fn):
        self.fn = fn

    def show(self, content, force=False):
        self.fn()
        return True


def test_meter_pushes_on_change_or_after_force_interval():
    values = iter([40.2, 40.4, 41.0, 41.0])
    now = [0.0]
    pushes = []
    m = Meter(fetch=lambda: Usage(next(values), None, 10, None), clock=lambda: now[0],
              display_factory=lambda h: OnShow(lambda: pushes.append(m.last.five_pct)))

    m.tick()                    # first: push
    now[0] = 60; m.tick()       # rounds to the same 40%: skip
    now[0] = 120; m.tick()      # 41%: push
    now[0] = 800; m.tick()      # unchanged but force_push (600 s) elapsed: push
    assert pushes == [40.2, 41.0, 41.0]


def test_meter_paused_does_not_push():
    pushes = []
    m = Meter(fetch=lambda: Usage(50, None, 10, None),
              display_factory=lambda h: OnShow(lambda: pushes.append(1)))
    m.paused = True
    assert m.tick().five_pct == 50 and pushes == []


def test_meter_lights_and_puts_out_fire_on_burn_rate():
    pcts = iter([10, 12, 14, 16, 18, 20, 22, 24] + [25] * 10 + [1])
    now = [0.0]
    shown = []
    m = Meter(fetch=lambda: Usage(next(pcts), None, 10, None), clock=lambda: now[0],
              display_factory=lambda h: OnShow(lambda: shown.append(m.on_fire)))
    states = []
    for i in range(19):
        now[0] = i * 60.0
        m.tick()
        states.append(m.on_fire)
    # 2%/min = 120%/h lights it once 5 min of history exist. Once usage goes flat it stays
    # lit at 30%/h (between the on and off thresholds) and goes out at 18%/h.
    assert states[:5] == [False] * 5 and all(states[5:16])
    assert states[16:] == [False] * 3
    assert shown[0] is False and True in shown and shown[-1] is False


def test_meter_fire_disabled_with_zero_rate(tmp_path):
    config.save_config({"fire_rate": 0})
    pcts = iter(range(0, 100, 10))
    now = [0.0]
    m = Meter(fetch=lambda: Usage(next(pcts), None, 10, None), clock=lambda: now[0],
              display_factory=lambda h: OnShow(lambda: None))
    for i in range(8):
        now[0] = i * 60.0
        m.tick()
    assert m.burn_rate and m.burn_rate > 100 and not m.on_fire


class FakeDisplay:
    def __init__(self, log, host, fail=False):
        self.log, self.host, self.fail = log, host, fail

    def show(self, content, force=False):
        if self.fail:
            raise DeviceError(f"GET http://{self.host}/app.json failed: timed out")
        self.log.append(self.host)
        return True


def _two_displays(**second):
    config.save_config({"displays": [{"name": "a", "host": "h1"}, {"name": "b", "host": "h2", **second}]})


def test_meter_fetches_once_and_pushes_every_display():
    _two_displays()
    fetches, shown = [], []
    m = Meter(fetch=lambda: fetches.append(1) or Usage(40, None, 10, None),
              display_factory=lambda host: FakeDisplay(shown, host))
    m.tick()
    assert len(fetches) == 1 and sorted(shown) == ["h1", "h2"]
    assert {t.status for t in m.targets.values()} == {"ok"}


def test_meter_failing_display_does_not_block_others_and_retries():
    _two_displays()
    down, shown = {"h2"}, []
    now = [0.0]
    m = Meter(fetch=lambda: Usage(40, None, 10, None), clock=lambda: now[0],
              display_factory=lambda host: FakeDisplay(shown, host, fail=host in down))
    m.tick()
    assert shown == ["h1"] and m.targets["b"].status == "unreachable"
    down.clear()
    now[0] = 60; m.tick()                  # unchanged numbers: only the failed display is due
    assert shown == ["h1", "h2"] and m.targets["b"].status == "ok"


def test_meter_skips_paused_and_off_displays():
    _two_displays(app="off")
    shown = []
    m = Meter(fetch=lambda: Usage(40, None, 10, None), display_factory=lambda h: FakeDisplay(shown, h))
    m.sync_targets()
    m.targets["a"].paused = True
    m.tick()
    assert shown == [] and m.targets["b"].status == "off"


def test_meter_follows_config_changes():
    _two_displays()
    shown = []
    now = [0.0]
    m = Meter(fetch=lambda: Usage(40, None, 10, None), clock=lambda: now[0],
              display_factory=lambda h: FakeDisplay(shown, h))
    m.tick()
    m.targets["a"].paused = True
    config.save_config({"displays": [{"name": "a", "host": "h1"}, {"name": "c", "host": "h3"}]})
    now[0] = 60; m.tick()
    assert list(m.targets) == ["a", "c"] and m.targets["a"].paused  # pause survives the reload
    assert sorted(shown) == ["h1", "h2", "h3"]


def test_meter_skips_held_display_and_keeps_history_on_rename():
    _two_displays()
    shown = []
    m = Meter(fetch=lambda: Usage(40, None, 10, None), display_factory=lambda h: FakeDisplay(shown, h))
    m.sync_targets()
    m.targets["a"].hold = True  # e.g. showing its name for Identify
    m.tick()
    assert shown == ["h2"]
    m.targets["b"].paused = True
    config.rename_display("b", "shelf")
    m.rename("b", "shelf")
    m.tick()
    assert m.targets["shelf"].paused and "b" not in m.targets


# --- tray ------------------------------------------------------------------------

def test_launch_command_frozen_and_venv(monkeypatch):
    from clockdisplay import tray
    assert "-m clockdisplay.tray" in tray._launch_command()
    monkeypatch.setattr(tray.sys, "frozen", True, raising=False)
    monkeypatch.setattr(tray.sys, "executable", r"D:\Apps\ClockDisplay.exe")
    assert tray._launch_command() == r'"D:\Apps\ClockDisplay.exe"'
