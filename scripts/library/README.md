# scripts/library — Music Library Tools

## Sync from songs.travisbriggs.com

The station's S3 library mirrors the songs site. Its public catalog
(`https://songs.travisbriggs.com/songs.json`, every visible song) is the source
of truth. After adding or removing songs on the site and deploying it:

```bash
python3 scripts/library/sync_site.py           # dry run: lists + uploads and - removals
python3 scripts/library/sync_site.py --apply   # change S3
```

- **Uploads** songs on the site that are missing from S3, unless they're in the
  bake denylist (`scripts/bake/song_denylist.txt`, e.g. covers). Mastered-in
  silence is trimmed first (same thresholds as `audit.py`/`trim.py`).
- **Removes** S3 mp3s that are no longer on the site. The bucket is not
  versioned, so removal is permanent.
- Denylisted songs already in S3 are left there; the bake skips them.
- Replacing the audio of an existing song (same slug) is not detected.

Needs `S3_MUSIC_BUCKET` and AWS credentials (the box's, as in the
[runbook](../../docs/go-live-runbook.md)). The nightly bake mirrors S3 to the
box with `--delete`, so changes reach the next show automatically.

## Silence cleanup

Audits and trims mastered-in silence from the station's S3 music library.

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
