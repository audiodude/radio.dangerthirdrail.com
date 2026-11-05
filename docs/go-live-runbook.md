# Station runbook

The steady state: **render → S3 catalog → weekly bake (Wed 03:00 PT) → hourly
station job uploads → Friday 06:00 PT swap into the station's playlist.**
Design: [specs/2026-11-05-youtube-station-design.md](superpowers/specs/2026-11-05-youtube-station-design.md).

## Add content (render host, needs the GPU)

    cd ~/code/vibes/radio.dangerthirdrail.com
    # spec = [{model, preset, seed, duration_sec, kind}] — see scripts/render/specs/
    ./scripts/render/render_catalog.sh scripts/render/specs/<spec>.json catalog_1080
    eval "$(ssh -n radio-playout 'sudo grep -E "^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|DEFAULT_REGION)=" /etc/radio.env' | sed 's/^/export /')"
    ./scripts/render/upload_catalog.sh catalog_1080 s3://music-backup-996646211514-us-west-1-an/catalog

`catalog_1080/` is the local source of truth for the S3 catalog: `upload_catalog.sh`
rebuilds `catalog.json` from the sidecars in that directory, so it must hold every
piece and ident, including the hand-made idents and their sidecars. Re-rendering a
piece overwrites its sidecar: carry over hand-added fields (`weight`, `filler`,
`title`) afterwards. `render_catalog.sh` skips pieces whose mp4 already exists, so it
is resumable; `upload_catalog.sh` uploads mp4s first and `catalog.json` last, so a
bake that starts mid-upload still sees a consistent manifest. The next weekly bake
picks new pieces up.

Roughly 30 min of render time per 300 s piece on an RTX 3080 Ti (1080p60 into a
temp AVI, then CRF 18).

### Song denylist

`scripts/bake/song_denylist.txt` lists song filenames the bake never plans, even
when they are in the S3 library. Edit it on `main`, merge to `release`, and
`git pull` in `/opt/radio` on the box; the next bake picks it up.

### Music library

The S3 music library mirrors songs.travisbriggs.com: run
`scripts/library/sync_site.py` (dry run) then `--apply`, with the box's AWS keys
(as above) plus `S3_MUSIC_BUCKET`. See `scripts/library/README.md`.

## Box setup (once)

    ssh radio-playout
    cd /opt/radio && sudo git pull
    sudo cp scripts/systemd/radio-station*.{service,timer} /etc/systemd/system/
    sudo systemctl daemon-reload

`/etc/radio.env` (root-only) needs, besides the AWS and `S3_*` entries:

| Variable | Value |
|---|---|
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | OAuth client + refresh token with `youtube.force-ssl` (`scripts/youtube_oauth.py` mints one) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Alert bot and chat |
| `MUSIC_DIR`, `CATALOG_DIR` | `/data/music`, `/data/catalog` |
| `BAKE_RES` | `1920x1080` (or unset) |
| Optional | `STATION_DIR` (`/data/station`), `STATION_BLURB_FILE` (`/etc/radio-blurb.txt`), `STATION_PLAYLIST_TITLE`, `BAKE_MIN_FREE_GB` (70), `BAKE_PRESET`/`BAKE_CRF`/`BAKE_MAXRATE`/`BAKE_BUFSIZE` |

`/etc/radio-blurb.txt` opens every VOD description and is the playlist's
description: plain text, multiple lines are fine.

## Launch (first set)

1. Bake a launch set that swaps in as soon as it's processed (~10–16 h of
   encoding; it runs under systemd, so it survives SSH disconnects):

       sudo systemd-run --unit=radio-station-bake-now -p EnvironmentFile=/etc/radio.env \
         /usr/bin/env bash /opt/radio/scripts/bake/bake.sh --now
       journalctl -u radio-station-bake-now -f

2. The bake starts `radio-station.service`, which creates the playlist (Telegram
   posts its URL), uploads, and fills it once every VOD is processed. Then enable
   the schedule:

       sudo systemctl enable --now radio-station.timer radio-station-bake.timer

3. **Manual, in YouTube Studio:** start the station on that playlist.
4. Point the domain at the station (one fixed 302; `<path>` is the station's
   URL path on www.youtube.com, e.g. `watch?v=ID`):

       aws s3api put-bucket-website --bucket radio.dangerthirdrail.com --website-configuration \
         '{"IndexDocument":{"Suffix":"index.html"},"RoutingRules":[{"Redirect":{"HostName":"www.youtube.com","HttpRedirectCode":"302","Protocol":"https","ReplaceKeyWith":"<path>"}}]}'
       aws cloudfront create-invalidation --distribution-id E24RTA588S2VSH --paths '/*'

The next scheduled swap is the first Friday at least 4 days after launch.

## Deploys

The box runs the `release` branch in `/opt/radio`. Merge `main` → `release` with a
regular merge (never a squash), push, then `cd /opt/radio && sudo git pull`. Unit
file changes also need the copy + `daemon-reload` above. Ship bake changes before
Wednesday 03:00 PT; a bake already running keeps the code it started with.

## Verify

- `sudo python3 /opt/radio/scripts/station/station.py status`: the live set, the
  waiting set's upload/processing progress, active alerts.
- `systemctl list-timers 'radio-station*'`: Wednesday bake and hourly job scheduled.
- `journalctl -u radio-station-bake` / `-u radio-station`.
- After a swap: the playlist in Studio holds only the new set; the station plays it.

## When things go wrong

- **Bake failed** (Telegram): fix, then `sudo systemctl start radio-station-bake`.
  It resumes: finished VODs are kept unless the plan changed (new songs or pieces).
- **Not ready / swap overdue**: `status` shows what's missing. Uploads paused by
  YouTube's cap resume by themselves after 24 h; the swap happens on the first
  hourly run after the last VOD is processed.
- **Playlist order unexpected**: someone edited the playlist mid-swap. Remove the
  new set's items from it (or restore the old order) and let the next run retry.
- **Re-bake a waiting set from scratch**: delete its entry from `state.json` and its
  `set-<seed>` directory, then start the bake.

## Kill switch

    ssh radio-playout 'sudo systemctl disable --now radio-station.timer radio-station-bake.timer'

That stops all uploads and swaps; the station keeps looping the current playlist.
To take it off air, stop the station in YouTube Studio.
