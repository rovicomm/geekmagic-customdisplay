# geekmagic-customdisplay

Drive a GeekMagic **SmallTV-Ultra** (240×240) over the LAN: push rendered frames, animated
GIFs and device settings. This is the groundwork for a Claude usage meter. The device API is
documented in [docs/ultra-api.md](docs/ultra-api.md).

<p align="center"><img src="docs/images/geekmagic.jpg" alt="SmallTV-Ultra showing the Claude usage card" width="360"></p>

The idea comes from [claude-meter](https://github.com/shavindraSN/claude-meter) by
[@shavindraSN](https://github.com/shavindraSN).

## Setup
```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```
Displays are listed in `config.json` in `%LOCALAPPDATA%\clockdisplay` (`~/.config/clockdisplay`
off Windows):
```json
{
  "displays": [
    {"name": "desk",  "host": "192.168.1.50", "app": "claude"},
    {"name": "shelf", "host": "192.168.1.51", "app": "off"}
  ]
}
```
`app` says what the display runs: `claude` (the usage meter, the default), `adsb` (aircraft
overhead, see [Aircraft overhead](#aircraft-overhead-ads-b)) or `off` (leave it alone). An older config with a single `"host"` still works and counts as one display.
Give each display a name you'll recognise. Use **Rename…** in the tray, `clock rename display desk`,
or the file itself. **Identify** in the tray shows the name and IP on that screen for a few
seconds, so you can tell which physical display is which.

The CLI targets the first display by default. Use `-d NAME` for another display, `-d all` for every
display (render commands, `restore`, `clean`), or `--host IP` / `$env:CLOCK_HOST` for an
unconfigured one. Without any config the host defaults to `192.0.2.10`.

## CLI
```powershell
clock displays                     # configured displays, their app, and whether they respond
clock rename display desk          # rename a configured display
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
clock -d all demo                  # push to every configured display; -d shelf for one
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
clock watch                        # keep every `claude` display updated in the foreground
```
Tokens: the credentials in `%USERPROFILE%\.claude\.credentials.json` are the source of truth.
A cached copy lives in **Windows Credential Manager** (`clockdisplay` / `claude-oauth`). When a
token expires the app refreshes it and writes the rotated pair back to Claude Code's file so
the `claude` CLI stays signed in. `clock auth --logout` clears the cached copy.

### Tray app
```powershell
.venv\Scripts\clock-tray.exe       # or: pythonw -m clockdisplay.tray
```
The icon shows the 5h % and the tooltip shows both windows with their reset times, plus how many
displays are unreachable. Usage is fetched once per poll and pushed to every `claude` display in
parallel, so one display that is offline doesn't hold up the others. The menu has Refresh now,
Pause all displays, Restore all clock themes, a submenu for each display (status, Identify, Rename…,
Pause updates, Restore clock theme, Open web UI), Edit settings, Open data folder, **Start with Windows** (an
HKCU `Run` entry) and Quit. Displays added to or removed from `config.json` show up on the next
poll. Only one instance runs at a time.

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
| `displays` | one entry from `host` | list of `{name, host, app}`; see Setup |
| `host` | `192.0.2.10` | legacy single-display IP, used when `displays` is absent |
| `poll_interval` | `60` | seconds between fetches; below ~30 s triggers rate limiting |
| `force_push` | `600` | re-push unchanged numbers so the countdowns stay fresh |
| `autopush` | `true` | `false` = tray only, leave the display alone |
| `fire_rate` | `40` | 5h burn rate (%/hour) that sets the 5h line on fire; `0` turns it off |
| `fire_window` | `600` | seconds of history the burn rate is measured over |

An even pace uses up the 5h window at 20 %/h. When the rate over the last `fire_window` reaches
`fire_rate`, the card becomes a looping GIF with flames on the 5h line. It goes back to the still
card once the rate falls below half of `fire_rate`, or when the window resets. Preview it with
`clock demo --fire --out fire.gif`, or watch it on the device in
[docs/images/fire.mp4](docs/images/fire.mp4).

## Aircraft overhead (ADS-B)
With a local ADS-B receiver running readsb, tar1090, dump1090-fa or piaware, the displays can
show the planes flying over you. Each one gets a card with a photo (from
[planespotters.net](https://www.planespotters.net), credited on screen), callsign, altitude with
a climb/descent arrow, type, registration, operator, speed, and distance/direction. Point the
app at the receiver's web address, the same one that shows its map:
```json
{
  "displays": [{"name": "desk", "host": "192.168.1.50", "app": "claude"}],
  "adsb": {"url": "http://192.168.1.20:8080", "radius": 5, "popup_seconds": 30}
}
```
* On a **`claude`** display, a plane that comes within range **pops up** over the usage card for
  `popup_seconds`, then the usage card comes back. The same plane won't pop up again for
  `cooldown` seconds. Set `"adsb_popup": false` on a display to keep pop-ups off it.
* An **`adsb`** display shows the nearest plane in range all the time and goes back to its
  clock theme when none is in range.

The tray's **Aircraft** menu shows what's overhead. From it you can switch spotting and pop-ups on
and off, choose the pop-up length and radius, pick which fields the card shows, set the receiver
URL, open the receiver's map, and pop up the nearest plane now to try things out. The other
settings live in the `adsb` block of `config.json` and are re-read every poll:

| key | default | |
| --- | --- | --- |
| `url` | `""` | receiver base URL; nothing happens until it's set |
| `enabled` | `true` | master switch |
| `radius` | `5` | nautical miles from `location` that count as overhead |
| `location` | receiver's | `[lat, lon]` to measure from, if not the receiver's own position |
| `min_altitude` / `max_altitude` | `0` / `0` | feet; a `max_altitude` of `0` means no ceiling |
| `include_ground` | `false` | also show aircraft on the ground |
| `types` | `[]` | only these ICAO type-code prefixes, e.g. `["B74", "A38"]` |
| `callsigns` | `[]` | only these callsign prefixes, e.g. `["BAW", "DAL"]` |
| `military_only` | `false` | only aircraft flagged military in the receiver's database |
| `popup` | `true` | pop planes up over `claude` displays |
| `popup_seconds` | `30` | how long a pop-up stays |
| `cooldown` | `1800` | seconds before the same plane can pop up again |
| `refresh` | `20` | seconds between card refreshes on `adsb` displays |
| `poll_interval` | `5` | seconds between receiver polls |
| `fields` | all but `squawk` | card contents: `photo`, `callsign`, `altitude`, `type`, `registration`, `operator`, `speed`, `distance`, `squawk` |

```powershell
clock planes                       # what's overhead now (--all: everything tracked)
clock plane                        # push the nearest plane's card (or: clock plane BAW117 --out card.png)
```
`clock watch` runs the spotter alongside the usage meter. Type, registration and operator come
from readsb's aircraft database, so they're blank on receivers that don't have it.

## How pushes work
Showing content switches to the Photo Album theme with autoplay off and remembers the previous
theme for `clock restore`. This state is kept per display, keyed by host, in `state.json`. Stills overwrite `/image/cm_main.jpg`, animations `/image/cm_anim.gif`.
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
