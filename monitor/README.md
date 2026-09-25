# vxstory monitor

Observes the Hetzner playout box, owns the YouTube broadcast lifecycle (scoped
to its own stream), drives the radio.dangerthirdrail.com redirect, alerts.

## Required env (Railway service `galton-monitor` / `vxstory-monitor`)
- `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`
- `YOUTUBE_STREAM_KEY` — the owned production stream key (the box pushes to this).
- `BROADCAST_TITLE`, `BROADCAST_DESCRIPTION`, `BROADCAST_PRIVACY` (default `public`)
- `BOX_HEALTH_URL`, `BOX_HEALTH_TOKEN` — box `/health` + shared secret.
- `RADIO_BUCKET`, `RADIO_CF_DISTRIBUTION_ID`, `RADIO_REGION`, `RADIO_OFFLINE_HTML_PATH`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Optional: `POLL_INTERVAL` (120), `DEGRADED_GRACE_POLLS` (3), `FORCE_ACTIVE` (`1` = always-on)

## Removed vs galton-monitor-orig
`GALTON_STREAM_URL`, `RAILWAY_API_TOKEN`, `GALTON_STREAM_SERVICE_ID`, and all
chat/title/fallback/Railway-restart logic. Window definition must match the box's
playout scheduling.

## First-song gate

Every operational poll reads the box's authenticated `/health`. Its `opening`
is `{run_id, phase, released}` (or `null` when unavailable); phases are
`countdown`, `waiting`, `ident`, `show`, and `idle`.

- Only a fresh countdown/waiting opening can receive a new broadcast.
- A listed owned `live` broadcast with an active stream authorizes
  `POST /opening/release` with `{"run_id": "<current UUID>"}`. Transition-request
  success, including the `testing` fallback, never releases playback.
- The box finishes the full 16-minute countdown, then holds at zero until
  released, plays the whole opening ident, and starts the first song at zero.
- If an earlier broadcast already released the opening, or playout reached
  the ident/show, `POST /opening/restart` starts a new opening before another
  broadcast can be created or made live. Unknown/idle state waits.

Both POST endpoints use the same bearer token and origin as `BOX_HEALTH_URL`;
the tunnel must route `/opening/*` as well as `/health`. Commands are scoped to
the transport UUID, so delayed commands from a prior connection are rejected.
`PLAYOUT_STATE`, `PLAYOUT_RELEASE`, and `PLAYOUT_RESTART` must match between
the box's health and playout services if their default `/tmp/playout_*.json`
paths are overridden. See the [runbook](../docs/go-live-runbook.md) for rollout.
