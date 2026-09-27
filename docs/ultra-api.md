# SmallTV-Ultra HTTP API (firmware Ultra-V9.0.45)

Reverse-engineered from the device's own web UI (`/settings.html`, `/image.html`,
`/weather.html`, `/time.html`, all served gzipped). Every `/set?...` call is a plain GET that
returns the body `OK`.

> The reference project `reference/claude-meter` targets the **SmallTV v2** firmware
> (`POST /upload`, `gif.jpg`/`file1.jpg`, custom GIF container, fixed JPEG qtables). **None of
> that applies to the Ultra.**

## Read-only state
| Endpoint | Example |
|---|---|
| `/v.json` | `{"m":"SmallTV-Ultra","v":"Ultra-V9.0.45"}` |
| `/app.json` | `{"theme":1}` |
| `/brt.json` | `{"brt":"16"}` |
| `/album.json` | `{"autoplay":0,"i_i":5}` |
| `/theme_list.json` | `{"list":"0,0,0,0,0,0,0","sw_en":"0","sw_i":"10"}` |
| `/space.json` | `{"total":3121152,"free":1252876}` |
| `/city.json` | weather city settings |
| `/filelist?dir=/image` | **HTML** `<table>` of files (name + size KB) |

## Control
| Call | Effect |
|---|---|
| `/set?theme=N` | 1 Weather Clock Today, 2 Weather Forecast, 3 Photo Album, 4–6 Time Style 1–3, 7 Simple Weather Clock |
| `/set?theme_list=a,b,c,d,e,f,g&sw_en=0/1&theme_interval=S` | auto-rotate themes (flags per theme) |
| `/set?brt=0..100` | brightness |
| `/set?t1=H&t2=H&b1=50&b2=B&en=0/1` | night mode: hours start/end, night brightness |
| `/set?i_i=S&autoplay=0/1` | Photo Album slideshow |
| `/set?img=/image/x.jpg` | show a file in Photo Album (JPEG or GIF) |
| `/set?gif=/gif/x.gif` | 80×80 GIF in the weather-clock theme |
| `/set?hc=#RRGGBB&mc=..&sc=..` | clock hour/minute/second colours |
| `/set?hour=0/1` | 12-hour clock |
| `/set?font=1/2` | big / digital clock font |
| `/set?colon=0/1` | blinking colon |
| `/set?day=1..5` | date format |
| `/delete?file=/image/x.jpg` | delete one file |
| `POST /doUpload?dir=/image/` | multipart upload; the part's filename is the stored name |

Destructive, deliberately **not** wrapped: `/set?reset=1` (factory reset), `/set?reboot=1`,
`/set?clear=image`, `/set?clear=gif`, `/wifisave`, `/update` (firmware).

## Behaviour observed (2026-09-26)
- **Plain Pillow JPEG (quality 90) displays correctly.** No custom qtables/APP0 needed.
- **Overwriting the file currently on screen refreshes it** with no extra `/set?img`.
- **Animated 240×240 GIFs play** in Photo Album at the encoded frame timing.
- Upload of a ~10 KB JPEG takes **0.4–0.8 s**; `/set?img` for a new file takes ~0.3–2.5 s.
  Practical live-update ceiling is ~1–2 frames/s; anything smoother should be a GIF.
- **Upload responses send two `Content-Length` headers** with different values
  (e.g. `3579, 22`). `requests`/urllib3 raise `InvalidHeader`; `http.client` is fine.
  The upload still succeeds either way.
- ~3 MB flash, ~1.2 MB free. The web UI caps uploads at 1 MB.
