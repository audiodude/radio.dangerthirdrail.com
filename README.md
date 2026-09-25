# Galton Board Simulator

A Godot 4.x app that simulates a Galton board (bean machine). Balls drop through pegs and collect in bins, forming a bell curve. Designed to run 24/7 and be streamed to YouTube.

## Features

- Balls spawn continuously from a funnel at the top
- 12 rows of pegs with concave curved walls
- 2-3 randomly-selected colors per round
- Bins show blended colors from all balls that landed in them
- Histogram overlay scales to full bin height
- Pegs flash subtly when touched
- Auto-resets after 472-512 balls per round
- Ball/cycle counter
- Live chat integration (welcomes, gift alerts)
- Background music from S3 with on-screen song title display

## Requirements

- Godot 4.4+
- Python 3 (for chat services)

## Running

```bash
godot --path . --main-scene main.tscn
```

Press ESC to quit.

## Chat Event IPC

Godot and the chat service communicate via a shared JSON file (`/tmp/chat_events.json` by default, configurable via `CHAT_EVENTS_FILE` env var).

### Protocol

The chat service writes a JSON array of event objects to the file. Godot reads and deletes the file each second. Write atomically (write to `.tmp`, then rename) to avoid partial reads.

### Event format

Each event is a JSON object with a `type` field:

```json
[
  {"type": "join", "name": "UserName", "time": 1712345678.9},
  {"type": "message", "name": "UserName", "text": "hello!", "time": 1712345678.9},
  {"type": "gift", "name": "UserName", "amount": "$10", "time": 1712345678.9}
]
```

| Field    | Type   | Description                          |
|----------|--------|--------------------------------------|
| `type`   | string | `"join"`, `"message"`, or `"gift"`   |
| `name`   | string | Display name of the user             |
| `text`   | string | Chat message (message events only)   |
| `amount` | string | Gift amount (gift events only)       |
| `time`   | float  | Unix timestamp                       |

### Chat services

**Mock (testing):**
```bash
python3 scripts/mock_youtube.py                    # all event types
python3 scripts/mock_youtube.py --joins-only       # only joins
python3 scripts/mock_youtube.py --gifts-only       # only gifts
python3 scripts/mock_youtube.py --interval 3       # every 3 seconds
python3 scripts/mock_youtube.py --burst 5          # up to 5 events per batch
```

**Live YouTube:**
```bash
YOUTUBE_API_KEY=... YOUTUBE_VIDEO_ID=... python3 scripts/chat_poller.py
```

## Music

The stream plays background music from MP3 files stored in S3. A Python music player decodes tracks and pipes raw audio to FFmpeg via a named pipe.

### How it works

1. On startup, `start.sh` syncs MP3s from S3 to `/data/mp3` (a persistent volume)
2. `scripts/music_player.py` shuffles and loops through all tracks
3. Each track is decoded to raw PCM (s16le, 44.1kHz stereo) and written to `/tmp/audio_pipe`
4. FFmpeg reads from the pipe and muxes the audio with the video capture
5. The current song title is written to `/tmp/current_song.txt`
6. `scripts/song_display.gd` polls that file every 2s and shows "♪ Song Title" in the bottom-left corner

### Song title display

When a new song starts, the title appears at full opacity for 5 seconds, then fades to 0.4 over 10 seconds and holds there until the next track.

### Environment variables

| Variable              | Description                           |
|-----------------------|---------------------------------------|
| `S3_MUSIC_BUCKET`       | S3 URI for music files (e.g. `s3://bucket/path/`) |
| `AWS_ACCESS_KEY_ID`     | AWS credentials for S3 music sync     |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials for S3 music sync     |

The S3 bucket path is configured in `start.sh`. Music files are cached on a persistent volume at `/data/mp3` so they only sync once.

## Video catalog: rendering (scripts/render/)

Pre-rendered generative video pieces come from the [vxstory](https://github.com/audiodude/vxstory)
models, rendered by composable helpers in `scripts/render/` (run on a machine with a
GPU + the vxstory checkout; `VXSTORY_DIR` defaults to `../vxstory`):

```bash
# one piece: model, preset, seed, duration, out dir (+ optional --kind ident)
scripts/render/render_piece.sh supernova_orbit odyssey 101 1200 ./catalog
# a whole catalog from a committable spec (see scripts/render/specs/)
scripts/render/render_catalog.sh scripts/render/specs/test-night.json ./catalog
# push to S3 (mp4s first, manifest last, so the manifest never references missing files)
scripts/render/upload_catalog.sh ./catalog s3://bucket/catalog/
```

Each piece renders at the models' native 1920×1080@60 (Movie Maker captures the
project viewport; shrinking it would crop the composition, not scale it), then
downscales to 1280×720 in the mp4 encode (lanczos — doubles as supersampling AA),
gets a sidecar JSON, and
`make_manifest.py` collects sidecars into `catalog.json`:
`{"pieces": [{id, kind: piece|ident, model, preset, seed, duration_sec, file, rendered_at}]}`.
Renders are serialized (one GPU, no contention), idempotent (existing mp4s are
skipped; writes are atomic), and ~3× realtime under xvfb on a 3080 Ti.

## Video playout (scripts/playout/)

Mirrors the music architecture: `video_player.py` decodes catalog pieces to raw frames
in a named pipe (`/tmp/video_pipe`), and `playout.sh` runs the master ffmpeg muxing
video pipe + audio pipe + song-title overlay (drawtext, `reload=1`) to FLV — a local
file by default, RTMP to YouTube when `YOUTUBE_STREAM_KEY` is set. (FLV because RTMP
*is* FLV-over-TCP, and truncated FLV stays playable.)

**Pacer model (the supply invariant):** a single wall-clock pacer thread owns every
write to the video pipe, emitting the most recent decoded frame at a 60fps ceiling from
a latest-frame slot. Decoders run under `-re` and only feed the slot — they never touch
the pipe. The slot is always full (black frame, then last decoded frame), so the master
ffmpeg never blocks on an empty video pipe and its output never drops below realtime
(the old direct-write model under-supplied at transitions and slowly drained YouTube's
buffer — the "stream dies after ~30s" symptom). This is the same invariant galton gets
free from x11grab, which samples the display at a fixed wall-clock rate. Disposable
mid-piece decoders are SIGKILLed (ffmpeg under `-re` ignores SIGTERM for ~5s).

Transitions happen at **song boundaries**: `video_player.py` cuts at the first boundary
after `DWELL_SEC` (default 900) — predictively from the music player's song clock, or
reactively by watching `/tmp/current_song.txt` — to a random ident, then the next
shuffled piece. A piece that ends naturally (EOF before a boundary) also transitions
through an ident. If the master ffmpeg dies/restarts, the player reopens the pipe,
restarts the pacer, and replays the current piece.

```bash
# everything (music + titles + playout) in one command:
DWELL_SEC=900 CATALOG_DIR=./catalog OUTPUT=./playout_test.mp4 scripts/playout/run_local.sh
# or run playout.sh alone if the music stack is already up
```

| Variable | Default | Description |
|---|---|---|
| `CATALOG_DIR` | `./catalog` | Dir containing mp4s + `catalog.json` |
| `VIDEO_PIPE` / `AUDIO_PIPE` | `/tmp/video_pipe` / `/tmp/audio_pipe` | Named pipes into the master ffmpeg |
| `SONG_FILE` | `/tmp/current_song.txt` | Watched for song-boundary detection + drawtext overlay |
| `DWELL_SEC` | `900` | Minimum piece play time before a boundary can cut it |
| `OUTPUT` | `./playout_test.flv` | File path, or rtmp URL (auto when `YOUTUBE_STREAM_KEY` set) |

Startup order matters: pipe writers (music player, video player) first, master ffmpeg
last — pipe opens block until both ends exist. Roadmap (Railway playout service, cloud
rendering, 50-piece catalog): see `TODO.md`.

## Pre-baked broadcast opening

Production playout uses `scripts/bake/` and `scripts/broadcast/` (see the
[go-live runbook](docs/go-live-runbook.md)), rather than the raw-pipe player above.
Each broadcast opens with **16 minutes of silent countdown**, displaying
`Danger Third Rail Radio` and `HH:MM:SS`, followed by a **complete opening ident**,
then the first song from the beginning. The first song never plays underneath
the countdown or ident. An ident's own audio, if present, is retained.

YouTube may become live partway through the countdown. At zero, playout waits
for the monitor to observe the owned broadcast as `live` with an active stream;
a successful transition request alone is not enough. The countdown, zero hold,
ident, and show share one stream-copy RTMP connection. Restarting the transport
starts a fresh countdown, and stale release commands cannot unlock the new run.
The monitor resets an already-used opening before publishing another broadcast.

The nightly bake publishes `.countdown.mkv`, `.hold.mkv`, `.ident.mkv`, and
`.opening.json` beside each show. A show without these opening assets is refused,
not streamed directly. Existing shows require opening generation before rollout;
see the runbook for deployment order and local-output verification.


## Legacy Galton deployment

The following describes the older Galton service stack. Current pre-baked
playout deployment is documented in the [go-live runbook](docs/go-live-runbook.md).

Railway auto-deploys both services from the `release` branch (not `main`). Push to `main` for development, then merge to `release` to deploy.

### Two-service architecture

| Service | Purpose | Watch paths |
|---------|---------|-------------|
| **galton-stream** | Godot + FFmpeg streaming to YouTube | Everything except `monitor/` |
| **galton-monitor** | Manages the day's YouTube broadcast, polls galton-stream health, updates the radio redirect | `monitor/**` |

### Daily broadcast lifecycle

YouTube deranks channels that stream 24/7. galton-monitor creates a fresh broadcast at window open (12:00 PT) and tears it down at window close (18:00 PT):

- **At window open** — create a new broadcast with `ultraLow` latency stamped with the **pinned** `BROADCAST_TITLE` / `BROADCAST_DESCRIPTION`, and bind it to the `liveStream` whose stream key matches `YOUTUBE_STREAM_KEY` (resolved + cached by `get_production_stream_id()`). FFmpeg is bounced so the stream goes inactive→active with the broadcast bound, which fires YouTube's `enableAutoStart`. Once live, update `radio.dangerthirdrail.com` to redirect to `youtube.com/live/<new_video_id>`.
- **At window close** — transition the broadcast to `complete`, set privacy to `private`, and flip the radio redirect to the offline title card.

> **Why pinned, not cloned.** An earlier version cloned the title/description **and** the bound stream from whatever broadcast was newest in the account. That made production hostage to any stray broadcast: spinning up a **test broadcast on a second stream key** (e.g. while testing another branch) made *it* the newest broadcast, so the monitor began binding production broadcasts to the test key. FFmpeg pushes to `YOUTUBE_STREAM_KEY`, but the bound stream received `noData` → `enableAutoStart` never fired → the broadcast sat in `ready` → after 15 min it was declared stale, deleted, and FFmpeg killed — a ~16-minute churn loop. The broadcasts also inherited the test branch's title. Binding by key and pinning the metadata makes the lineage independent of other broadcasts, so **you can run test broadcasts on a different stream key without touching production.** To change the production stream, repoint `YOUTUBE_STREAM_KEY` (both services); to change copy, set `BROADCAST_TITLE` / `BROADCAST_DESCRIPTION` (env-overridable, defaults baked into `monitor/monitor.py`).

The radio redirect is implemented as an S3 website bucket (`radio.dangerthirdrail.com`) fronted by CloudFront. Online = bucket routing rule 302s to YouTube (302 not 301, so the target can change daily without browsers caching it). Offline = routing rule is removed and `index.html` (a responsive title card) is served.

### Recovery escalation (within the active window)

galton-monitor polls galton-stream's `/health` endpoint every 120s over Railway internal networking:

1. **1 fail (120s)** — start fallback stream (backup image to YouTube)
2. **5 fails (600s)** — restart galton-stream container
3. **6 fails (720s)** — redeploy galton-stream via Railway API
4. **7 fails (840s)** — alert that all recovery failed

### Running locally

```bash
docker build -t galton-stream .
docker run -e YOUTUBE_STREAM_KEY=your-key \
           -e S3_MUSIC_BUCKET=s3://bucket/path/ \
           -e AWS_ACCESS_KEY_ID=... \
           -e AWS_SECRET_ACCESS_KEY=... \
           galton-stream
```

The container runs Godot headless with Xvfb and streams to YouTube via FFmpeg at 720p 30fps with audio.
