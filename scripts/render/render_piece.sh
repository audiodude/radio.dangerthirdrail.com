#!/usr/bin/env bash
# render_piece.sh <model> <preset> <seed> <duration_sec|song> <out_dir> [--kind piece|ident]
#                 [--title "Credit"] [--audio song.mp3]
# Renders a single vxstory model+preset to a 60 fps mp4 (RENDER_RES, default
# 1920x1080: the models' native size) with seed substitution. --title is the
# piece's on-screen credit, written to the sidecar (default: the model name).
#
# --audio (render.mjs models only) makes a song render: kind "song", synced to
# that mp3, which must be the exact file the bake airs (the S3 library copy).
# Duration `song` covers the whole song; a number cuts a short proof. The
# sidecar records the song and its sha256; a render whose song has changed
# since is redone instead of skipped.
set -euo pipefail

USAGE='usage: render_piece.sh <model> <preset> <seed> <duration_sec|song> <out_dir> [--kind piece|ident] [--title "Credit"] [--audio song.mp3]'
MODEL="${1:?$USAGE}"
PRESET="${2:?$USAGE}"
SEED="${3:?$USAGE}"
DURATION="${4:?$USAGE}"
OUT_DIR="${5:?$USAGE}"
KIND="piece"
TITLE=""
AUDIO=""

shift 5
while [[ $# -gt 0 ]]; do
    case "$1" in
        --kind) KIND="$2"; shift 2 ;;
        --title) TITLE="$2"; shift 2 ;;
        --audio) AUDIO="$2"; shift 2 ;;
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
SONG="" SONG_SHA=""
if [[ -n "$AUDIO" ]]; then
    [[ -f "$MODEL_DIR/render.mjs" ]] || { echo "--audio needs a render.mjs model, not $MODEL" >&2; exit 1; }
    [[ -f "$AUDIO" ]] || { echo "song not found: $AUDIO" >&2; exit 1; }
    AUDIO="$(realpath "$AUDIO")"   # render.mjs runs from the model dir
    KIND="song"
    SONG="$(basename "$AUDIO")"
    SONG_SHA="$(sha256sum "$AUDIO" | cut -d' ' -f1)"
    OUT_NAME="${OUT_NAME}_${SONG%.*}"
    if [[ "$DURATION" == "song" ]]; then
        # The bake gives a song the slot ffprobe reports. Render one frame past
        # it (render.mjs reads silence past the decoded end) so the video always
        # covers the slot, however the mp3's padding rounds.
        SONG_DUR="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$AUDIO")"
        DURATION="$(awk -v d="$SONG_DUR" 'BEGIN { f = int(d * 60) + 2; printf "%.6f", f / 60 }')"
    fi
elif [[ "$DURATION" == "song" ]]; then
    echo "duration 'song' needs --audio" >&2; exit 1
fi
OUT_MP4="$OUT_DIR/${OUT_NAME}.mp4"
OUT_JSON="$OUT_DIR/${OUT_NAME}.json"

# Skip if already rendered (a song render only while its song is unchanged)
if [[ -f "$OUT_MP4" ]]; then
    if [[ -z "$SONG" ]] || [[ "$(jq -r '.song_sha256 // ""' "$OUT_JSON" 2>/dev/null)" == "$SONG_SHA" ]]; then
        echo "[render_piece] skipping (exists): $OUT_MP4"
        exit 0
    fi
    echo "[render_piece] $SONG changed since its render; re-rendering $OUT_MP4"
    rm -f "$OUT_MP4" "$OUT_JSON"
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

# Render at the models' native 1080p design space; RENDER_RES only changes the
# encode (a smaller size is a Lanczos downscale, which doubles as supersampling
# AA). Do NOT shrink the project viewport (the old override.cfg approach): that
# changes the design space and CROPS the composition rather than scaling it —
# every model is composed in 1920x1080 coordinates.
echo "[render_piece] rendering $MODEL/$PRESET seed=$SEED duration=${DURATION}s -> $OUT_MP4"
START_TIME="$(date +%s)"

if [[ -f "$MODEL_DIR/render.mjs" ]]; then
    # hydra: headless GPU Chromium, no display; writes the same MJPEG AVI intermediate.
    # Run from the model dir so asdf resolves its Node, not the caller's cwd pin.
    TMP_AVI="$(realpath "$TMP_AVI")"
    NODE_MAJOR="$(cd "$MODEL_DIR" && node -p 'process.versions.node.split(".")[0]')"
    (( NODE_MAJOR >= 24 )) || { echo "$MODEL needs Node >= 24 (got $NODE_MAJOR in $MODEL_DIR)" >&2; exit 1; }
    (cd "$MODEL_DIR" && node render.mjs --preset "$TMP_PRESET" --duration "$DURATION" \
        ${AUDIO:+--audio "$AUDIO"} --out "$TMP_AVI")
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
RES="${RENDER_RES:-1920x1080}"
ffmpeg -y -loglevel error \
    -i "$TMP_AVI" \
    -vf "scale=${RES/x/:}:flags=lanczos" \
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
    --arg title "$TITLE" \
    --arg song "$SONG" \
    --arg song_sha256 "$SONG_SHA" \
    '{id: $id, kind: $kind, model: $model, preset: $preset, seed: $seed,
      duration_sec: $duration_sec, file: $file, rendered_at: $rendered_at}
     + (if $title != "" then {title: $title} else {} end)
     + (if $song != "" then {song: $song, song_sha256: $song_sha256} else {} end)' \
    > "$OUT_JSON"

TOTAL_ELAPSED=$(( $(date +%s) - START_TIME ))
echo "[render_piece] done in ${TOTAL_ELAPSED}s: $OUT_MP4"
