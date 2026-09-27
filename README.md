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
`~/.config/clockdisplay/config.json` (`{"host": "..."}`).

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
clock demo --five 42 --week 76     # mock Claude usage card
clock pattern                      # colour/geometry test pattern
clock anim scroll "Claude Code"    # also: anim fill 64, anim blink "!"

clock restore                      # go back to the theme active before we took over
clock files                        # list device files (* = ours)
clock clean                        # delete only our cm_* files
```
Add `--out file.png` (or `.gif` for animations) to any render command to preview it locally
without touching the device.

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
