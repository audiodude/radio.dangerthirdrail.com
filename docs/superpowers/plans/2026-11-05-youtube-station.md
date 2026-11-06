# YouTube Station implementation plan (2026-11-05)

Spec: [../specs/2026-11-05-youtube-station-design.md](../specs/2026-11-05-youtube-station-design.md).
Branch `youtube-station`. Stdlib-only Python, matching the bake and monitor.

## 1. Catalog at 1080p (render host)

- [x] `render_piece.sh`: encode at `RENDER_RES` (default 1920x1080) instead of a
      fixed 720p downscale.
- [x] `scripts/render/specs/station-1080.json`: today's 18 pieces + 4 new 600 s
      pieces + the text ident.
- [ ] Render into `catalog_1080/` (hand-made idents copied from `catalog_v2/`);
      carry the text ident's `weight`/`filler` into its new sidecar; build the
      manifest; upload to the S3 catalog.

## 2. Set planner (`scripts/bake/plan.py`)

- [ ] Replace the show planner with `build_set(catalog, library, config)`:
      stretches of 2–3 songs, weighted ident per VOD (filler may repeat, wraps
      the seam), pre-roll vs overlay idents, uncut/cut visuals with the 600 s
      cap, LRU pieces with no repeat across any boundary including the seam,
      subtitles, VOD title + description.
- [ ] Catalog loader keeps `model` and optional `title`; songs get `slug`.
- [ ] CLI: `--seed --swap-at --blurb-file --out`.
- [ ] Tests rewritten for the set planner.

## 3. Encode + validate (`assemble.py`, `validate.py`)

- [ ] `assemble.py --plan P --out-dir D`: one MP4 per VOD (filter-graph concat
      so 720p idents and 1080p pieces mix; burned titles; CRF 18 slow, 20 Mbps
      cap; AAC 384k 48 kHz; `+faststart`; atomic). Existing VOD files are
      skipped, so a crashed bake resumes.
- [ ] `validate.py --plan P --dir D`: every VOD's duration, codecs, size.
- [ ] Tests on decoded-media fixtures.

## 4. Weekly bake (`bake.sh`)

- [ ] `station.py target` → seed + swap time, or skip; `--now` for launch.
- [ ] Free-space guard, S3 sync, plan, assemble, validate, publish to
      `$S3_SHOWS_URI/set-<seed>/`, `station.py register`, start
      `radio-station.service`. Any failure alerts via `station.py alert`.
- [ ] Delete `opening.py` usage (file removed at teardown).

## 5. Station job (`scripts/station/`)

- [ ] `youtube.py`: OAuth refresh, JSON API with typed errors (`reason`),
      resumable chunked upload with session resume, playlist + video ops.
- [ ] `station.py`: state file (flock, atomic), `target`, `register`, `run`,
      `alert`; run = ensure playlist → uploads → processing → swap → old-set
      deletion → daily health → prune → alerts.
- [ ] Tests against a fake YouTube.

## 6. Box units (`scripts/systemd/`)

- [ ] `radio-station-bake.{service,timer}` (Wed 03:00 PT),
      `radio-station.{service,timer}` (hourly).

## 7. Launch (needs the renders)

- [ ] Smoke: plan + encode a tiny set locally; station run against real
      YouTube with an isolated state + throwaway playlist, then clean up.
- [ ] Upload `catalog_1080`; ship code + units + env to the box; disable the
      live `radio-bake.timer`; bake `--now`; first set uploads and swaps.
- [ ] **Manual:** Start Station in Studio on the new playlist.
- [ ] Hard cutover: stop `radio-playout`, stop the Railway monitor, fixed 302.

## 8. Teardown (after the station is verified)

- [ ] Delete live code, units, `/data/shows`, old S3 shows, Railway service,
      `radio-sys` tunnel; rewrite README, runbook, CLAUDE.md deployment notes.

## Log

- 2026-11-05: probe upload from the box landed unlisted (force-ssl token).
  Encode benchmark at 1080p60 CRF 18 slow: 2.4× slower than realtime;
  `supernova_orbit` 39 Mbps uncapped, `fluid_swirl` 8 Mbps → 20 Mbps cap.
- 2026-11-05 evening: station code merged and deployed to the box; units
  installed, `radio-station.timer` + `radio-station-bake.timer` enabled; the
  live `radio-bake.timer` disabled (playout replays `show-2026-11-05` until
  cutover); old shows pruned; `BAKE_RES=1920x1080`. Station playlist created:
  `PLG_1kc8loxkk`. A detached chain on the render host publishes `catalog_1080`
  when the render finishes and starts the launch bake (`bake.sh --now`, unit
  `radio-station-bake-now`). Target: station live Saturday 2026-11-07.
