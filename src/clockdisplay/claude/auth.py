"""OAuth access token for the Claude usage endpoint, borrowed from Claude Code's sign-in.

Claude Code keeps its tokens in ~/.claude/.credentials.json (`claudeAiOauth`). That file is
the source of truth; we keep a copy of the current token pair in the OS credential store
(Windows Credential Manager via `keyring`) so a valid token survives restarts without
re-reading the file, and never write tokens to our own config/state files.

Refresh tokens rotate, so after refreshing we write the new pair back to the credentials
file too; otherwise the `claude` CLI would be left holding a dead refresh token.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's public OAuth client
KEYRING_SERVICE = "clockdisplay"
KEYRING_USER = "claude-oauth"
EXPIRY_MARGIN = 60  # seconds


class AuthError(RuntimeError):
    pass


def credentials_path() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(override) if override else Path.home() / ".claude") / ".credentials.json"


# --- keyring cache -------------------------------------------------------------

def _load_cached() -> dict | None:
    try:
        raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        return json.loads(raw) if raw else None
    except (KeyringError, ValueError):
        return None


def _save_cached(token: dict) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USER, json.dumps(token))
    except KeyringError:
        pass  # the credentials file still works; the cache is an optimisation


def invalidate() -> None:
    """Forget the cached token; the next call re-reads Claude Code's credentials."""
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except (KeyringError, PasswordDeleteError):
        pass


def _valid(token: dict | None) -> bool:
    return bool(token and token.get("access_token")
                and time.time() < float(token.get("expires_at", 0)) - EXPIRY_MARGIN)


# --- Claude Code credentials file ----------------------------------------------

def _read_file() -> tuple[dict, dict | None]:
    """(whole credentials dict, our token view of claudeAiOauth or None)."""
    path = credentials_path()
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        raise AuthError(f"could not read {path}: {e}") from e
    oauth = creds.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        return creds, None
    return creds, {
        "access_token": oauth["accessToken"],
        "refresh_token": oauth.get("refreshToken", ""),
        "expires_at": int(oauth.get("expiresAt") or 0) / 1000,
    }


def _write_back(creds: dict, token: dict) -> None:
    """Atomically store a refreshed pair in Claude Code's file, keeping every other field."""
    path = credentials_path()
    oauth = creds.setdefault("claudeAiOauth", {})
    oauth.update(accessToken=token["access_token"], refreshToken=token["refresh_token"],
                 expiresAt=int(token["expires_at"] * 1000))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(creds, f)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _refresh(refresh_token: str) -> dict:
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token,
                       "client_id": CLIENT_ID}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise AuthError(f"token refresh failed (HTTP {e.code}); run `claude` and sign in again") from e
    if not result.get("access_token"):
        raise AuthError("token refresh returned no access token; run `claude` and sign in again")
    return {
        "access_token": result["access_token"],
        "refresh_token": result.get("refresh_token") or refresh_token,
        "expires_at": time.time() + int(result.get("expires_in") or 28800),
    }


# --- public ----------------------------------------------------------------------

def get_access_token() -> str:
    """A currently valid access token: keyring cache > Claude Code's file > refresh."""
    cached = _load_cached()
    if _valid(cached):
        return cached["access_token"]

    creds, from_file = _read_file()
    if _valid(from_file):
        _save_cached(from_file)
        return from_file["access_token"]

    # Prefer the file's refresh token (Claude Code may have rotated it since we cached ours).
    refresh = (from_file or {}).get("refresh_token") or (cached or {}).get("refresh_token")
    if not refresh:
        raise AuthError(f"no Claude sign-in found in {credentials_path()}; "
                        "install Claude Code and run `claude` to sign in")
    token = _refresh(refresh)
    _save_cached(token)
    if creds:
        _write_back(creds, token)
    return token["access_token"]


def status() -> dict:
    """Where the token would come from and when it expires (no secrets)."""
    cached = _load_cached()
    _, from_file = _read_file()
    if _valid(cached):
        source, exp = "credential manager", cached["expires_at"]
    elif _valid(from_file):
        source, exp = f"{credentials_path()}", from_file["expires_at"]
    elif from_file or cached:
        source, exp = "expired (will refresh on next use)", None
    else:
        source, exp = "not signed in", None
    return {"source": source, "expires_in": int(exp - time.time()) if exp else None}
