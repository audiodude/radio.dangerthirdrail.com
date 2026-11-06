# YouTube Station design (2026-11-05)

Replace the daily 12–18 PT live broadcast with a **YouTube Station**: a 24/7
"live" stream YouTube plays from one playlist of ordinary VODs. The box stops
streaming; it bakes a new set of VODs once a week, uploads them, and swaps
them into the station's playlist. Decided in a grilling session; every
decision below was made explicitly.

## What a set is

One **set** is one pass through the whole library, cut into **stretch VODs**.
A set is baked weekly and runs as the station's playlist, in a fixed order,
looping, for a week.

- **Seed** = the date of the Friday the set swaps in (`20261113`). The first
  set, launched by hand, uses the launch date. Plans are deterministic.
- **Songs**: the library (minus `song_denylist.txt`) in a seeded shuffle, each
  song exactly once per set, cut into stretches of **2 or 3 songs** (drawn at
  random; the tail is split so no stretch is ever 1 or 4 songs). ~168 songs →
  ~67 VODs, ~6.5 h per loop.
- **One ident opens every VOD**, drawn by catalog `weight` (today the text
  filler is 6 of 9 shares). Never the same ident on two consecutive VODs,
  **except the filler**, which may repeat (as in the live bake). The rule
  wraps around the loop: the last VOD's ident vs the first VOD's.
  - An ident **with audio** is pre-roll: it plays alone with its sound, then
    song 1 starts.
  - A **silent** ident (the filler) plays over the first seconds of song 1;
    music starts at 0.
- **Visuals**: each stretch is drawn **uncut** (75%, one piece covers every
  song) or **cut** (a new piece at each song boundary). Uncut needs the
  covered span to be ≤ `MAX_SEG` (600 s) and a piece at least that long;
  otherwise it is cut. A cut segment longer than 600 s also cuts mid-song.
  Pieces rotate least-recently-used across the whole set and never repeat
  across any boundary, including the loop seam. In-points are random.
- The burned-in `♪ Song Title` stays (fades in at each song, as today).

## VOD metadata

- **Title** (≤100 chars): `Song A / Song B (Render 1, Render 2)`. Every song
  title is always kept; the render list is best effort and is truncated with
  `...` (`(Supernova Or...)`), or dropped if not even that fits. If the songs
  alone exceed 100 chars, the last song is truncated with `...`.
- **Render names**: optional catalog sidecar `title`, else the model name in
  Title Case (`supernova_orbit` → `Supernova Orbit`); in order of appearance,
  deduplicated.
- **Description**: the station blurb (the monitor's old
  `BROADCAST_DESCRIPTION`, kept in `/etc/radio-blurb.txt`), then the songs
  with links (`https://songs.travisbriggs.com/<slug>/`), then render credits.
- Unlisted, category Music (10), not made for kids, no tags, no subscriber
  notification. Custom thumbnail `/etc/radio-thumbnail.jpg` (the Pink Fluffy
  Cloud card, `assets/station-thumbnail.jpg`) set on each VOD once processed.

## Encode

1080p60 H.264, x264 `-preset slow -crf 18` with a **20 Mbps peak cap**
(`-maxrate 20M -bufsize 40M`), GOP 30, AAC 384 kbps 48 kHz, MP4 with
`+faststart`. Measured on the box: ~2.4× slower than realtime at 1080p60, so
a set encodes in ~10–16 h. The cap bounds a set to ~58 GB (an uncapped
`supernova_orbit` hit 39 Mbps), so two sets fit on the box.

The catalog is re-rendered at native 1080p (`render_piece.sh` no longer
downscales to 720p), plus four new 600 s pieces so the 600 s pool is 8:
`chromatic_cascade/fresco`, `matter_cycle/tides`, `radial_burst/pulsar`,
`metro_rise/century` (spec `scripts/render/specs/station-1080.json`). The
hand-made 720p idents are upscaled by the bake's scale filter. Launch waits
for the re-render.

## Week on the box (all times Pacific)

| When | What |
|---|---|
| **Wed 03:00** | `radio-station-bake.timer`: pick the target Friday, sync S3, plan, encode every VOD, validate, publish to S3, register the set, kick the station job |
| **hourly** | `radio-station.timer` → `station.py run`: upload, check processing, swap, delete, health check, alert |
| **Thu 18:00** | Telegram alert if the set isn't fully uploaded and processed |
| **Fri 06:00** | Swap: append the new VODs to the playlist in order, then remove the old items. If the set isn't ready, last week's keeps playing and the swap happens on the first hourly run after it is |
| **day after the swap** | Delete last week's videos |
| **daily** | Health check on the live set: playlist = the set, in order; every video processed and unlisted |

**Target selection** (`station.py target`): the coming Friday. Skip if a set
is already pending, or if the live set swapped in fewer than 4 days before
that Friday (so a set launched Tue–Thu gets a full first week). `--now`
(launch): seed = today, swap immediately.

## YouTube

- **Credentials**: the monitor's OAuth client + refresh token, already in
  `/etc/radio.env` (`youtube.force-ssl`). A probe upload from the box landed
  unlisted and stayed unlisted after processing (unaudited-project lock does
  not apply).
- **Playlist**: one long-lived playlist, created by the box on first run
  (public, titled `Danger Third Rail radio // Shocking Visuals!`, blurb as
  description); its ID lives in the station state. The station is bound to
  it once, by hand, in Studio. The box owns the playlist: anything in it that
  isn't the live set is removed at the swap.
- **Quota** (10,000/day default bucket; uploads are a separate 100/day
  bucket): swap ≈ 67 inserts + 67 removals ≈ 6,700 units on Friday; deleting
  the old videos ≈ 3,350 on Saturday. `quotaExceeded` defers to the next
  Pacific midnight.
- **Upload cap** (per channel, unpublished): on `uploadLimitExceeded`, pause
  uploads 24 h and resume. Uploads are resumable; the session URL is kept in
  state so a crash mid-upload resumes instead of duplicating.
- **Processing**: `failed`/`rejected` videos are deleted and re-uploaded
  (alert after 3 attempts).

## State and retention

`/data/station/state.json` (atomic writes, `flock`):

```json
{
  "playlist_id": "PL…",
  "sets": {
    "20261113": {"plan": "/data/station/set-20261113/plan.json",
                 "swap_at": "2026-11-13T06:00:00-08:00", "status": "pending",
                 "swapped_on": null,
                 "vods": [{"file": "vod-000.mp4", "video_id": null,
                           "upload_url": null, "processed": false, "attempts": 0}]}
  },
  "upload_until": null, "quota_until": null,
  "alerts": {}, "last_health": null
}
```

Set status: `pending` → `live` (at swap; the previous `live` becomes `old`) →
removed from state once its videos are deleted. Each set lives in
`/data/station/set-<seed>/` (VODs + `plan.json`) and is published to
`$S3_SHOWS_URI/set-<seed>/`. At each swap, local and S3 copies of every set
other than `live` and `pending` are pruned, so at most two sets exist. The
bake refuses to start with less than `BAKE_MIN_FREE_GB` (70) free.

## Alerts

Telegram (the monitor's bot and chat), edge-triggered by key in state: bake
failure, Thursday-18:00 not ready, swap overdue, upload cap hit, quota hit,
repeated upload/processing failure, health check failed / recovered, any
unexpected error (once per message until a clean run), and one message per
successful swap.

## Cutover (hard) and teardown

1. Re-render finishes → upload `catalog_1080` to the S3 catalog.
2. Ship the station code to the box, install the units, disable the nightly
   live `radio-bake.timer` (playout keeps replaying `LATEST`), bake the first
   set with `--now`.
3. The first set uploads and fills the new playlist. **Manual:** Start Station
   on it in Studio.
4. Same day: stop and disable `radio-playout`, stop the Railway monitor, and
   point `radio.dangerthirdrail.com` at the station with one fixed 302 rule.
   The next swap is the first Friday ≥4 days out.
5. Once the station is verified: delete the live code (`scripts/broadcast/`,
   `scripts/bake/opening.py`, `monitor/`, the title card), the old units
   (`radio-bake`, `radio-playout`, `radio-health`), `/data/shows` and old S3
   shows, the Railway service, and the `radio-sys` tunnel.

## Unknowns accepted

- How the station reacts to playlist edits mid-play (append-then-remove is
  the gentlest order). Watch the first Friday swap.
- The gap between videos (lands on the ident/pre-roll by design).
- Whether the station needs a public playlist (created public to be safe).
