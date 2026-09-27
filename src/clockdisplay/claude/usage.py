"""Claude subscription usage (5h session / 7d weekly) from Anthropic's OAuth usage endpoint.

Same numbers as Claude's Settings -> Usage page. Ported from claude-meter's usage.py.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from clockdisplay.claude import auth

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA = "oauth-2025-04-20"


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Usage:
    five_pct: float
    five_resets_at: dt.datetime | None
    week_pct: float
    week_resets_at: dt.datetime | None

    @property
    def five_reset(self) -> str:
        return format_reset(self.five_resets_at)

    @property
    def week_reset(self) -> str:
        return format_reset(self.week_resets_at)


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def parse(data: dict) -> Usage:
    five = data.get("five_hour") or {}
    week = data.get("seven_day") or {}
    return Usage(float(five.get("utilization") or 0), _parse_time(five.get("resets_at")),
                 float(week.get("utilization") or 0), _parse_time(week.get("resets_at")))


def format_reset(when: dt.datetime | None, now: dt.datetime | None = None) -> str:
    """'in 45m', 'in 2h 15m', or a local weekday + time like 'Mon 9:00 AM'."""
    if when is None:
        return "unknown"
    now = now or dt.datetime.now(dt.timezone.utc)
    secs = int((when - now).total_seconds())
    if secs <= 0:
        return "soon"
    if secs < 3600:
        return f"in {max(1, secs // 60)}m"
    if secs < 86400:
        h, m = divmod(secs // 60, 60)
        return f"in {h}h {m}m" if m else f"in {h}h"
    local = when.astimezone()
    return f"{local:%a} {local.hour % 12 or 12}:{local:%M %p}"  # no %-I on Windows


def _retry_after(value: str | None) -> int:
    """Retry-After is seconds or an HTTP date; default 60 s."""
    if not value:
        return 60
    try:
        return max(1, int(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(1, int((when - dt.datetime.now(when.tzinfo)).total_seconds()))
    except (TypeError, ValueError):
        return 60


def _get(token: str) -> dict:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": BETA,
        "User-Agent": "clockdisplay"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_raw() -> dict:
    """GET the usage JSON. Re-authenticates once on 401/403; raises RateLimited on 429."""
    for attempt in (1, 2):
        try:
            return _get(auth.get_access_token())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(_retry_after(e.headers.get("Retry-After"))) from e
            if e.code in (401, 403) and attempt == 1:
                auth.invalidate()
                continue
            raise
    raise AssertionError("unreachable")


def fetch_usage() -> Usage:
    return parse(fetch_raw())
