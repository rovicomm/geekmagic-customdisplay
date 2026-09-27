import datetime as dt
import io
import json
import time

import pytest

from clockdisplay import config
from clockdisplay.claude import auth, usage
from clockdisplay.claude.usage import Usage, format_reset, parse
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

def test_meter_pushes_on_change_or_after_force_interval():
    values = iter([40.2, 40.4, 41.0, 41.0])
    now = [0.0]
    pushes = []
    m = Meter(fetch=lambda: Usage(next(values), None, 10, None), display_factory=None,
              clock=lambda: now[0])
    m.push = lambda u, force=False: pushes.append(u.five_pct)

    m.tick()                    # first: push
    now[0] = 60; m.tick()       # rounds to the same 40%: skip
    now[0] = 120; m.tick()      # 41%: push
    now[0] = 800; m.tick()      # unchanged but force_push (600 s) elapsed: push
    assert pushes == [40.2, 41.0, 41.0]


def test_meter_paused_does_not_push():
    pushes = []
    m = Meter(fetch=lambda: Usage(50, None, 10, None), display_factory=None)
    m.push = lambda u, force=False: pushes.append(u)
    m.paused = True
    assert m.tick().five_pct == 50 and pushes == []
