# scripts/library — Music Library Tools

## Sync from songs.travisbriggs.com

The station's S3 library mirrors the songs site. Its public catalog
(`https://songs.travisbriggs.com/songs.json`, every visible song) is the source
of truth. After adding or removing songs on the site and deploying it:

```bash
python3 scripts/library/sync_site.py           # dry run: lists + uploads, ~ re-processing, - removals
python3 scripts/library/sync_site.py --apply   # change S3
```

- **Uploads** songs on the site that are missing from S3, unless they're in the
  bake denylist (`scripts/bake/song_denylist.txt`, e.g. covers). Each song is
  processed first, in a single encode: mastered-in silence is trimmed (same
  thresholds as `audit.py`/`trim.py`) and loudness is normalized (below).
- **Re-processes** (`~`) songs already in S3 that weren't made by the current
  processing: each processed upload carries the S3 object metadata
  `library-process` (`x-amz-meta-library-process`, value
  `normalize.PROCESS_TAG`, e.g. `trim+loudnorm-linear-v1;I=-21;TP=-1`). Songs
  without it (uploaded before normalization, or overwritten by `sync_up.sh` or
  a manual `aws s3 cp`) or with an older value are re-made from the site's file
  and overwritten in place. Changing a target or bumping the version in
  `normalize.py` therefore re-processes the whole library on the next
  `--apply`. Once applied, a second run reports `in sync`.
- **Removes** S3 mp3s that are no longer on the site. The bucket is not
  versioned, so removal is permanent.
- Denylisted songs already in S3 are left there, unprocessed; the bake skips them.
- Replacing the audio of an existing song (same slug) is not detected.

The dry run checks every S3 song's metadata (one `head-object` each) but
downloads nothing, so it can't predict gains; `--apply` prints each song's
before/after loudness and ends with the list of capped songs.

Needs `S3_MUSIC_BUCKET` and AWS credentials (the box's, as in the
[runbook](../../docs/go-live-runbook.md)). The nightly bake mirrors S3 to the
box with `--delete`, so changes reach the next show automatically.

### Loudness normalization

`normalize.py` (also runnable alone: `normalize.py SRC DST`) is gain-only, so
each song keeps its dynamics: two-pass ffmpeg `loudnorm` in **linear** mode
(pass 1 measures I/TP/LRA, pass 2 applies one constant gain). Targets:
**−21 LUFS integrated, true peak ≤ −1 dBTP**. That is the on-air level: the
bake (`scripts/bake/assemble.py`) adds no gain of its own.

- Why −21 and not −14: nearly every song is mastered with peaks at ~0 dBTP, so
  gain alone can only reach a target well below the loud masters. At −14 LUFS,
  121 of 168 songs would have needed peak limiting; at −21, all but a handful
  fit.
- Songs that still can't reach −21 without peaks over −1 dBTP (quiet masters
  that already peak near 0 dBTP; 6 of the 168 measured, the worst ~1.7 LU short)
  get their gain **capped** at the true-peak ceiling and land slightly under
  −21. loudnorm's dynamic-mode fallback is never used: if loudnorm reports
  anything but linear, the sync stops with an error.
- The cap aims 0.3 dB under −1 dBTP because the MP3 encode adds up to ~0.2 dB
  of inter-sample overshoot.
- Output is MP3 at the source's sample rate and channel count, re-encoded with
  LAME VBR `-q:a 0` (V0, ~245 kbps, as `trim.py` uses). That re-encode is lossy
  (transparent in practice at V0); re-processing always starts from the site's
  file, so songs never stack generations of S3 re-encodes.

**Order matters when changing the level:** the bake plays files as-is, so S3
songs and `assemble.py` must agree. Ship them together, between bakes (the
timer runs at 03:00 PT): `sync_site.py --apply` and deploy the matching bake
scripts the same day.

## Silence cleanup

Audits and trims mastered-in silence from the station's S3 music library.
`sync_site.py` now trims every song it uploads, so this is only for one-off
inspection. These tools don't normalize loudness: anything `sync_up.sh`
uploads lacks the `library-process` marker, and the next
`sync_site.py --apply` re-makes it from the site.

### Stages

| # | Script | What it does |
|---|--------|--------------|
| 1 | `sync_down.sh <workdir>` | Download originals from S3 → `<workdir>/original/` |
| 2 | `audit.py <workdir>` | Detect leading/trailing silence on every mp3; write `<workdir>/audit.json` and a human-readable table |
| 3 | `trim.py <workdir>` | Trim offenders with one ffmpeg pass; verify before/after; write `<workdir>/trim_report.json` |
| 4 | `sync_up.sh <workdir>` | Upload trimmed files over their S3 originals (**run only after human review**) |

## Safety model

- Originals are never modified. `sync_down.sh` writes into `<workdir>/original/` only.
- `trim.py` writes new files into `<workdir>/trimmed/`. Non-offenders are not copied there.
- `sync_up.sh` uploads **only** files present in `trimmed/`, touching only the changed tracks.
- `sync_up.sh` is intentionally kept separate so a human can inspect trimmed files before any S3 write happens.
- After upload, the Railway volume cache (`/data/mp3`) must be cleared manually before the station picks up the new files — `start.sh` skips sync when that directory is non-empty.

## Env requirements

All four scripts require AWS credentials and the bucket path. Source the helper before running:

```bash
source scripts/library/env.sh
```

Or export manually:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=...
export S3_MUSIC_BUCKET=...   # e.g. s3://bucket/prefix/
```

The `env.sh` helper pulls these from Railway at runtime using `railway variables --json`. It requires the Railway CLI to be linked to this project (`railway link`).

## Quick start

```bash
source scripts/library/env.sh
WORK=./library_work

bash scripts/library/sync_down.sh "$WORK"
python3 scripts/library/audit.py "$WORK"
# inspect audit.json / table output
python3 scripts/library/trim.py "$WORK"
# inspect library_work/trimmed/ and trim_report.json
# THEN, only after review:
# bash scripts/library/sync_up.sh "$WORK"
```
