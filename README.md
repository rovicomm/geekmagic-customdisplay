# clock-display

Drive a GeekMagic **SmallTV-Ultra** (240×240) over the LAN: push rendered frames, animated
GIFs and device settings. This is the groundwork for a Claude usage meter. The device API is
documented in [docs/ultra-api.md](docs/ultra-api.md).

## Setup
```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```
Default host is `192.0.2.10`. Override it with `--host`, `$env:CLOCK_HOST`, or
`config.json` in `%LOCALAPPDATA%\clockdisplay` (`{"host": "..."}`; `~/.config/clockdisplay` off Windows).

## CLI
```powershell
clock info                         # model, firmware, theme, brightness, free space
clock theme                        # list themes (* = active); `clock theme 1` to switch
clock brightness 40
clock night on --start 22 --end 7 --brightness 5
clock cycle 1 4 --interval 30      # auto-rotate themes; `clock cycle --off`
clock clockstyle --colors "#ffffff" "#d97757" "#ffffff" --12h on

clock text "Hello\nClaude" --fg "#d97757"
clock color orange
clock image photo.jpg --mode contain
clock gif party.gif
clock meter 83 --label "5h session" --sub "resets in 1h 4m"
clock demo --five 42 --week 76     # mock Claude usage card (--fire: heavy-burn animation)
clock pattern                      # colour/geometry test pattern
clock anim scroll "Claude Code"    # also: anim fill 64, anim blink "!"

clock restore                      # go back to the theme active before we took over
clock files                        # list device files (* = ours)
clock clean                        # delete only our cm_* files
```
Add `--out file.png` (or `.gif` for animations) to any render command to preview it locally
without touching the device.

## Claude usage
Live 5h-session / 7d-weekly usage (the same numbers as Claude → Settings → Usage) comes from
Anthropic's OAuth usage endpoint, authenticated with your existing **Claude Code** sign-in, so
install Claude Code and run `claude` once to sign in first.
```powershell
clock auth                         # where the token comes from and when it expires
clock usage                        # fetch once, print and push the usage card (--no-push, --out)
clock watch                        # keep the display updated in the foreground
```
Tokens: the credentials in `%USERPROFILE%\.claude\.credentials.json` are the source of truth.
A cached copy lives in **Windows Credential Manager** (`clockdisplay` / `claude-oauth`). When a
token expires the app refreshes it and writes the rotated pair back to Claude Code's file so
the `claude` CLI stays signed in. `clock auth --logout` clears the cached copy.

### Tray app
```powershell
.venv\Scripts\clock-tray.exe       # or: pythonw -m clockdisplay.tray
```
The icon shows the 5h % and the tooltip shows both windows with their reset times. The menu has
Refresh now, Pause display updates, Restore clock theme, Edit settings, Open data folder,
**Start with Windows** (an HKCU `Run` entry) and Quit. Only one instance runs at a time.

### Portable exe
```powershell
.venv\Scripts\pip install -e ".[build]"
.venv\Scripts\python scripts\build_exe.py     # -> dist\ClockDisplay.exe (~19 MB)
```
`ClockDisplay.exe` is the tray app as a single file with no Python needed, so it can be copied
anywhere. It still uses the machine's Claude Code sign-in, Credential Manager and
`%LOCALAPPDATA%\clockdisplay`. "Start with Windows" points at wherever the exe lives, so toggle
it off and on again after moving it. One-file exes unpack to `%TEMP%` on launch (a second or two),
and SmartScreen may warn the first time because the exe is unsigned.

Files live in `%LOCALAPPDATA%\clockdisplay`: `config.json`, `state.json` and `logs\tray.log`.
`config.json` keys (re-read every poll):

| key | default | |
| --- | --- | --- |
| `host` | `192.0.2.10` | display IP |
| `poll_interval` | `60` | seconds between fetches; below ~30 s triggers rate limiting |
| `force_push` | `600` | re-push unchanged numbers so the countdowns stay fresh |
| `autopush` | `true` | `false` = tray only, leave the display alone |
| `fire_rate` | `40` | 5h burn rate (%/hour) that sets the 5h line on fire; `0` turns it off |
| `fire_window` | `600` | seconds of history the burn rate is measured over |

An even pace uses up the 5h window at 20 %/h. When the rate over the last `fire_window` reaches
`fire_rate`, the card becomes a looping GIF with flames on the 5h line. It goes back to the still
card once the rate falls below half of `fire_rate`, or when the window resets. Preview it with
`clock demo --fire --out fire.gif`.

## How pushes work
Showing content switches to the Photo Album theme with autoplay off and remembers the previous
theme for `clock restore`. Stills overwrite `/image/cm_main.jpg`, animations `/image/cm_anim.gif`.
Overwriting the on-screen file refreshes it without re-selecting it. Identical content is
skipped (`--force` to override) to save flash writes. The tool never deletes files it didn't
create.

## Library
```python
from clockdisplay import UltraDevice, Display
from clockdisplay.render import frames, anim, widgets, canvas

dev = UltraDevice("192.0.2.10")
Display(dev).show(frames.dual_meter(42, "in 2h 15m", 76, "Mon 9:00 AM"))
Display(dev).show(anim.fill(64, label="7d"))   # GIF bytes work too
```
`widgets` has `text_box`, `bar`, `ring`, `sparkline` and `paste_fit` for building custom 240×240 layouts.

## Tests
```powershell
.venv\Scripts\python -m pytest
```
