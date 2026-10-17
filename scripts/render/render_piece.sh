#!/usr/bin/env bash
# render_piece.sh <model> <preset> <seed> <duration_sec> <out_dir> [--kind piece|ident]
# Renders a single vxstory model+preset to 720p60 mp4 with seed substitution.
set -euo pipefail

MODEL="${1:?usage: render_piece.sh <model> <preset> <seed> <duration_sec> <out_dir> [--kind piece|ident]}"
PRESET="${2:?}"
SEED="${3:?}"
DURATION="${4:?}"
OUT_DIR="${5:?}"
KIND="piece"

shift 5
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kind) KIND="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

VXSTORY_DIR="${VXSTORY_DIR:-$(cd "$(dirname "$0")/../../.." && pwd)/vxstory}"
MODEL_DIR="$VXSTORY_DIR/$MODEL"
PRESET_PATH="$MODEL_DIR/presets/$PRESET.json"

[[ -d "$MODEL_DIR" ]] || { echo "model dir not found: $MODEL_DIR" >&2; exit 1; }
[[ -f "$PRESET_PATH" ]] || { echo "preset not found: $PRESET_PATH" >&2; exit 1; }

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"  # resolve to absolute

OUT_NAME="${MODEL}_${PRESET}_s${SEED}"
OUT_MP4="$OUT_DIR/${OUT_NAME}.mp4"
OUT_JSON="$OUT_DIR/${OUT_NAME}.json"

# Skip if already rendered
if [[ -f "$OUT_MP4" ]]; then
    echo "[render_piece] skipping (exists): $OUT_MP4"
    exit 0
fi

# Temp files. The intermediate AVI is multi-GB (a 300s 1080p60 piece is ~5 GB);
# /tmp is tmpfs on some hosts, so park it next to the output by default.
TMP_PRESET="$(mktemp /tmp/preset_XXXXXX.json)"
TMP_AVI="$(mktemp "${RENDER_TMPDIR:-$OUT_DIR}/render_XXXXXX.avi")"


cleanup() {
    rm -f "$TMP_PRESET" "$TMP_AVI"
}
trap cleanup EXIT

# Substitute seed into preset
jq --argjson seed "$SEED" '.seed = $seed' "$PRESET_PATH" > "$TMP_PRESET"

# Render at the models' native 1080p design space, downscale to 720p in the
# encode. Do NOT shrink the project viewport (the old override.cfg approach):
# that changes the design space and CROPS the composition rather than scaling
# it — every model is composed in 1920x1080 coordinates.
echo "[render_piece] rendering $MODEL/$PRESET seed=$SEED duration=${DURATION}s -> $OUT_MP4"
START_TIME="$(date +%s)"

if [[ -f "$MODEL_DIR/render.mjs" ]]; then
    # hydra: headless GPU Chromium, no display; writes the same MJPEG AVI intermediate.
    node "$MODEL_DIR/render.mjs" --preset "$TMP_PRESET" --duration "$DURATION" --out "$TMP_AVI"
else
    xvfb-run -a -s "-screen 0 1920x1080x24" \
        godot --path "$MODEL_DIR" \
        --write-movie "$TMP_AVI" \
        --fixed-fps 60 \
        -- --preset "$TMP_PRESET" --duration "$DURATION"
fi

ELAPSED=$(( $(date +%s) - START_TIME ))
echo "[render_piece] render done in ${ELAPSED}s, encoding mp4..."

# Encode to a temp name and mv into place so a killed run can never leave a
# half-written mp4 that the skip-if-exists check would mistake for complete.
# Lanczos downscale 1080p -> 720p doubles as supersampling AA.
ffmpeg -y -loglevel error \
    -i "$TMP_AVI" \
    -vf "scale=1280:720:flags=lanczos" \
    -c:v libx264 -crf 18 -pix_fmt yuv420p \
    -r 60 -an \
    -f mp4 "${OUT_MP4}.tmp"
mv "${OUT_MP4}.tmp" "$OUT_MP4"

# Write sidecar JSON (duration via ffprobe)
ACTUAL_DUR="$(ffprobe -v error -select_streams v:0 \
    -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$OUT_MP4")"
if ! [[ "$ACTUAL_DUR" =~ ^[0-9.]+$ ]]; then
    echo "[render_piece] ERROR: ffprobe returned no duration for $OUT_MP4" >&2
    exit 1
fi

RENDERED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq -n \
    --arg id "${OUT_NAME}" \
    --arg kind "$KIND" \
    --arg model "$MODEL" \
    --arg preset "$PRESET" \
    --argjson seed "$SEED" \
    --argjson duration_sec "$ACTUAL_DUR" \
    --arg file "$OUT_MP4" \
    --arg rendered_at "$RENDERED_AT" \
    '{id: $id, kind: $kind, model: $model, preset: $preset, seed: $seed,
      duration_sec: $duration_sec, file: $file, rendered_at: $rendered_at}' \
    > "$OUT_JSON"

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "[render_piece] done in ${TOTAL_ELAPSED}s: $OUT_MP4"
