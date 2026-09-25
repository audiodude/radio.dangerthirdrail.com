#!/usr/bin/env bash
# Nightly bake: sync inputs, plan -> assemble -> opening -> validate -> publish.
# Env: S3_MUSIC_BUCKET, S3_CATALOG_URI, SHOWS_DIR, S3_SHOWS_URI, AWS creds,
#      BAKE_RES (default 1280x720), BAKE_FPS (60), BAKE_DWELL (600),
#      SHOW_DUR (default 22800), BAKE_VBITRATE (6M), RETENTION_DAYS (3).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATE="$(date +%F)"
SHOWS_DIR="${SHOWS_DIR:-/data/shows}"
MUSIC_DIR="${MUSIC_DIR:-/data/mp3}"
CATALOG_DIR="${CATALOG_DIR:-/data/catalog}"
mkdir -p "$SHOWS_DIR" "$MUSIC_DIR" "$CATALOG_DIR"

echo "[bake] syncing inputs from S3"
aws s3 sync "${S3_MUSIC_BUCKET:?}" "$MUSIC_DIR/" --no-progress --exclude "*" --include "*.mp3"
aws s3 sync "${S3_CATALOG_URI:?}" "$CATALOG_DIR/" --no-progress

PLAN="$SHOWS_DIR/show-$DATE.plan.json"
SHOW="$SHOWS_DIR/show-$DATE.mkv"
SEED="$(date +%Y%m%d)"

echo "[bake] planning (seed=$SEED res=${BAKE_RES:-1280x720})"
python3 "$HERE/plan.py" --catalog "$CATALOG_DIR/catalog.json" --library-dir "$MUSIC_DIR" \
    --seed "$SEED" --show-dur "${SHOW_DUR:-22800}" --dwell "${BAKE_DWELL:-600}" \
    --fps "${BAKE_FPS:-60}" --resolution "${BAKE_RES:-1280x720}" --out "$PLAN"

echo "[bake] assembling"
BAKE_VBITRATE="${BAKE_VBITRATE:-6M}" python3 "$HERE/assemble.py" --plan "$PLAN" --out "$SHOW"

echo "[bake] rendering opening (16-minute silent countdown, hold, full ident)"
BAKE_VBITRATE="${BAKE_VBITRATE:-6M}" python3 "$HERE/opening.py" --plan "$PLAN" --show "$SHOW"

echo "[bake] validating"
python3 "$HERE/validate.py" --plan "$PLAN" --show "$SHOW"

echo "[bake] publishing to S3"
S3_SHOWS="${S3_SHOWS_URI:?}"; S3_SHOWS="${S3_SHOWS%/}"   # env may carry a trailing /
aws s3 cp "$SHOW" "$S3_SHOWS/show-$DATE.mkv" --no-progress
aws s3 cp "$PLAN" "$S3_SHOWS/show-$DATE.plan.json" --no-progress
for part in countdown hold ident; do
    aws s3 cp "$SHOWS_DIR/show-$DATE.$part.mkv" "$S3_SHOWS/show-$DATE.$part.mkv" --no-progress
done
aws s3 cp "$SHOWS_DIR/show-$DATE.opening.json" "$S3_SHOWS/show-$DATE.opening.json" --no-progress
echo "show-$DATE.mkv" > "$SHOWS_DIR/LATEST"
aws s3 cp "$SHOWS_DIR/LATEST" "$S3_SHOWS/LATEST" --no-progress

RETAIN="${RETENTION_DAYS:-3}"
echo "[bake] pruning shows older than $RETAIN days"
CUTOFF="$(date -u -d "-$RETAIN days" +%Y-%m-%d)"
for output in "$SHOWS_DIR"/show-*; do
    [[ -f "$output" ]] || continue
    base="${output##*/}"
    [[ "$base" =~ ^show-([0-9]{4}-[0-9]{2}-[0-9]{2})\. ]] || continue
    [[ "${BASH_REMATCH[1]}" < "$CUTOFF" ]] || continue
    rm -f -- "$output" || true
done

# S3 too: a show is ~16 GB, so keeping every night forever is a real bill. Match
# on the show's own date stamp (not S3 mtime) and never touch LATEST.
BUCKET="${S3_SHOWS#s3://}"; BUCKET="${BUCKET%%/*}"
aws s3 ls "$S3_SHOWS/" --recursive | awk '{print $4}' | while read -r key; do
    base="${key##*/}"
    [[ "$base" =~ ^show-([0-9]{4}-[0-9]{2}-[0-9]{2})\. ]] || continue   # never LATEST
    [[ "${BASH_REMATCH[1]}" < "$CUTOFF" ]] || continue
    echo "[bake] pruning s3://$BUCKET/$key"
    aws s3 rm "s3://$BUCKET/$key" || true
done
echo "[bake] done: $SHOW"
