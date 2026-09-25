# Go-live runbook (vxstory pre-baked playout)

The steady state: **render → S3 catalog → nightly bake (03:00 PT) → window-gated
playout (11:45–18:05 PT) → monitor owns the YouTube broadcast + redirect.**
Playout starts with a **16-minute silent countdown**, then a complete ident,
then the first song from its beginning. A normal 11:45 start reaches zero at
12:01 PT; the radio redirect still opens at noon.

## Add content (render host, needs the GPU)

    cd ~/code/vibes/radio.dangerthirdrail.com
    # spec = [{model, preset, seed, duration_sec, kind}] — see scripts/render/specs/
    ./scripts/render/render_catalog.sh scripts/render/specs/<spec>.json catalog_v2
    eval "$(ssh -n radio-playout 'sudo grep -E "^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|DEFAULT_REGION)=" /etc/radio.env' | sed 's/^/export /')"
    ./scripts/render/upload_catalog.sh catalog_v2 s3://music-backup-996646211514-us-west-1-an/catalog

`render_catalog.sh` skips pieces whose mp4 already exists, so it is resumable.
`upload_catalog.sh` uploads mp4s first and `catalog.json` last — a bake that
starts mid-upload still sees a consistent manifest. The next nightly bake picks
new pieces up automatically; nothing else has to be touched.

Roughly 30 min of render time per 300 s piece on an RTX 3080 Ti (1080p60 into
a temp AVI, Lanczos-downscaled to 720p at CRF 18).

### Piece length sets the music budget

`plan.py` cuts video at song boundaries: a piece segment runs `dwell + the rest
of the current song`, so it must fit inside the **shortest** piece. Songs longer
than `min_piece - dwell` are dropped from the show (listed as `excluded_songs`
in the plan, and printed by the bake). With 300 s pieces and `BAKE_DWELL=30`
that drops 3 of 165 tracks. Want the long tracks on air? Render longer pieces.

## Bake by hand (normally the timer does this)

    ssh radio-playout 'sudo systemctl start radio-bake.service'   # ~2.5 h for a 6 h show
    ssh radio-playout 'journalctl -u radio-bake -f'

Output: `/data/shows/show-<date>.mkv` + `.plan.json`, plus `.countdown.mkv`,
`.hold.mkv`, `.ident.mkv`, and `.opening.json`. The opening manifest records
each segment's filename and timeline duration, including AAC preroll/padding
needed for non-overlapping packet timestamps. The visible countdown is exactly
960 seconds. All assets are published to S3 before
`/data/shows/LATEST` is updated. `RETENTION_DAYS=3` covers the whole bundle.
At 6 Mbps, the countdown adds approximately 720 MB per show.

The opening ident is the first ident source in the seeded plan, played in full
from zero regardless of its interior EDL slot. Its own audio is retained if
present; otherwise it is silent. First-song audio begins only after it finishes.

## Startup safety and rollout

`playout.sh` launches `playout.py`. One FFmpeg connection stream-copies the
countdown, zero hold, ident, and show; no reconnect occurs at their boundaries.
The countdown displays `Danger Third Rail Radio` and `HH:MM:SS`.
At zero, playout continues silent zero frames until the monitor confirms the
owned broadcast is listed `live` and the stream is active. A transition request
or `testing` state cannot release the ident or music.

Each transport attempt has a new UUID. The authenticated health service exposes
`opening: {run_id, phase, released}` and accepts matching-UUID commands at
`POST /opening/release` and `/opening/restart`. Late commands for an earlier
connection return 409. If a new broadcast is needed after an opening has already
been released, the monitor restarts the opening before making it live.
Missing box state or opening assets fails closed; there is no direct-show fallback.

Deploy the updated bake scripts, playout scripts, health service, and Railway
monitor together, preferably outside the operational window:

1. Update `/opt/radio/scripts/bake/` and `/opt/radio/scripts/broadcast/` on the
   box. Keep playout stopped during an in-window rollout.
2. Generate opening assets for the current show before enabling the new runtime.
   A full bake does this automatically. To keep an existing show without rebaking
   its music/video, run on the box:

       SHOW="$(cat /data/shows/LATEST)"
       sudo systemd-run --wait --collect -p EnvironmentFile=/etc/radio.env \
         /usr/bin/python3 /opt/radio/scripts/bake/opening.py \
         --plan "/data/shows/${SHOW%.mkv}.plan.json" --show "/data/shows/$SHOW"

   This requires the plan's ident source to remain available locally. Publish
   the new opening bundle alongside the show if restoring shows from S3.
3. Ensure the cloudflared route for `radio-sys.dangerthirdrail.com` forwards
   `/opening/*` to the health service as well as `/health`. Restart
   `radio-health.service` and `radio-playout.service`.
4. Deploy the monitor through the normal `main` → `release` merge and push.
   An old health service blocks the new monitor; an old monitor leaves the new
   playout holding at zero. Complete both sides before expecting music.

The health and playout services share these paths (override both services
identically if needed):

| Environment variable | Default |
|---|---|
| `PLAYOUT_STATE` | `/tmp/playout_state.json` |
| `PLAYOUT_RELEASE` | `/tmp/playout_release.json` |
| `PLAYOUT_RESTART` | `/tmp/playout_restart.json` |
| `PLAYOUT_HEARTBEAT` | `/tmp/playout_heartbeat` |

`SHOWS_DIR` defaults to `/data/shows`. For isolated local runs, `OUTPUT` writes
FLV to a file instead of YouTube and does not require `YOUTUBE_STREAM_KEY`;
`WINDOW_CMD=true` bypasses the schedule. Use private paths for all four state
files above. The live-confirmation gate still applies to local output.
The decoded-media and local HTTP regression suite exercises it without YouTube:

    uv run --with pytest --with boto3 python -m pytest monitor/tests scripts/broadcast/tests scripts/bake/tests -q

## Go live

    ssh radio-playout 'sudo systemctl enable --now radio-bake.timer'
    ssh radio-playout 'sudo systemctl enable --now radio-playout.service'
    # resume the Railway monitor (owns broadcast lifecycle + redirect)
    curl -sS -X POST https://backboard.railway.com/graphql/v2 \
      -H "Authorization: Bearer $CLAUDE_RAILWAY_TOKEN" -H "Content-Type: application/json" \
      -d '{"query":"mutation { deploymentRedeploy(id: \"<latest-deployment-id>\") { id status } }"}'

Both sides self-gate to the operational window, so enabling them outside it is
safe — playout idles until 11:45 PT and the monitor sleeps to the next boundary.

## Verify (during the window)

- `ssh radio-playout 'systemctl status radio-playout'` — FFmpeg streaming the opening/show through one master connection.
- `curl -A DangerThirdRailMonitor/1.0 -H "Authorization: Bearer $BOX_HEALTH_TOKEN" https://radio-sys.dangerthirdrail.com/health` — `playout_alive`, `ffmpeg_alive`, fresh `heartbeat_age_s`, and `opening`.
- YouTube Studio: stream `active` → broadcast `live` during the countdown.
- `opening.phase`: `countdown` → `waiting` → `ident` → `show`; `released` becomes true only after observed live confirmation. A late confirmation holds at zero, without consuming the song.
- `radio.dangerthirdrail.com` redirects to the live video from 12:00 PT; the complete first song follows the opening ident after the countdown.
- At 18:05 PT: box stops → broadcast completes → redirect goes offline.

## Kill switch

    ssh radio-playout 'sudo systemctl stop radio-playout'          # off air now
    ssh radio-playout 'sudo systemctl disable radio-playout radio-bake.timer'

Stopping playout stops sending media immediately. The monitor completes the
broadcast at the operational window's end. Run playout only through its single
systemd service; do not launch a second writer to the same stream key/state files.
