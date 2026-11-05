#!/usr/bin/env bash
# Weekly station bake: pick the target set, sync inputs, plan -> assemble ->
# validate -> publish, then hand the set to the station job for upload.
# Usage: bake.sh [--now]   (--now: the launch set, swapped in as soon as it's up)
# Env: S3_MUSIC_BUCKET, S3_CATALOG_URI, S3_SHOWS_URI, AWS creds, STATION_DIR
#      (/data/station), MUSIC_DIR, CATALOG_DIR, BAKE_RES (1920x1080),
#      BAKE_FPS (60), STATION_BLURB_FILE (/etc/radio-blurb.txt),
#      BAKE_MIN_FREE_GB (70); encode knobs in assemble.py.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STATION=(python3 "$HERE/../station/station.py")
trap 'rc=$?; (( rc == 0 )) || "${STATION[@]}" alert "bake failed (exit $rc); journalctl -u radio-station-bake" || true' EXIT

STATION_DIR="${STATION_DIR:-/data/station}"
MUSIC_DIR="${MUSIC_DIR:-/data/music}"
CATALOG_DIR="${CATALOG_DIR:-/data/catalog}"
mkdir -p "$STATION_DIR" "$MUSIC_DIR" "$CATALOG_DIR"

TARGET="$("${STATION[@]}" target "$@")"
if [[ -z "$TARGET" ]]; then
    echo "[bake] nothing to bake"
    exit 0
fi
read -r SEED SWAP_AT <<<"$TARGET"
SET_DIR="$STATION_DIR/set-$SEED"
echo "[bake] set $SEED, on air $SWAP_AT"

FREE_GB="$(df -BG --output=avail "$STATION_DIR" | tail -1 | tr -dc 0-9)"
if (( FREE_GB < ${BAKE_MIN_FREE_GB:-70} )); then
    echo "[bake] only ${FREE_GB} GB free in $STATION_DIR (need ${BAKE_MIN_FREE_GB:-70})" >&2
    exit 1
fi

echo "[bake] syncing inputs from S3"
# --delete: a song removed from S3 must also leave the box's library.
aws s3 sync "${S3_MUSIC_BUCKET:?}" "$MUSIC_DIR/" --no-progress --delete --exclude "*" --include "*.mp3"
aws s3 sync "${S3_CATALOG_URI:?}" "$CATALOG_DIR/" --no-progress

mkdir -p "$SET_DIR"
PLAN="$SET_DIR/plan.json"
echo "[bake] planning (res=${BAKE_RES:-1920x1080})"
python3 "$HERE/plan.py" --catalog "$CATALOG_DIR/catalog.json" --library-dir "$MUSIC_DIR" \
    --seed "$SEED" --swap-at "$SWAP_AT" --fps "${BAKE_FPS:-60}" \
    --resolution "${BAKE_RES:-1920x1080}" \
    --blurb-file "${STATION_BLURB_FILE:-/etc/radio-blurb.txt}" --out "$PLAN.new"
# A re-run resumes a crashed bake (assemble skips finished VODs), but only if
# the VODs are unchanged; new songs or pieces since then invalidate every one.
same_vods() { python3 -c 'import json,sys; a,b=(json.load(open(p))["vods"] for p in sys.argv[1:]); sys.exit(a!=b)' "$@"; }
if [[ -f "$PLAN" ]] && ! same_vods "$PLAN" "$PLAN.new"; then
    echo "[bake] plan changed since the last attempt; discarding its VODs"
    rm -f "$SET_DIR"/vod-*.mp4
fi
mv "$PLAN.new" "$PLAN"

echo "[bake] encoding VODs"
python3 "$HERE/assemble.py" --plan "$PLAN" --out-dir "$SET_DIR"

echo "[bake] validating"
python3 "$HERE/validate.py" --plan "$PLAN" --dir "$SET_DIR"

echo "[bake] publishing to S3"
S3_SHOWS="${S3_SHOWS_URI:?}"; S3_SHOWS="${S3_SHOWS%/}"   # env may carry a trailing /
aws s3 sync "$SET_DIR" "$S3_SHOWS/set-$SEED/" --no-progress --exclude ".work-*"

"${STATION[@]}" register "$PLAN"
systemctl start --no-block radio-station.service 2>/dev/null || true
echo "[bake] done: $SET_DIR ($(du -sh "$SET_DIR" | cut -f1))"
