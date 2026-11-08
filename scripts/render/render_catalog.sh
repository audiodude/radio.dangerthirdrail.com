#!/usr/bin/env bash
# render_catalog.sh <spec.json> <out_dir>
# Renders all entries in a spec JSON through render_piece.sh, then runs make_manifest.py.
#
# Spec format: array of objects:
#   { "model": "...", "preset": "...", "seed": N, "duration_sec": N, "kind": "piece"|"ident",
#     "title": "On-screen credit" (optional) }
set -euo pipefail

SPEC="${1:?usage: render_catalog.sh <spec.json> <out_dir>}"
OUT_DIR="${2:?}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

[[ -f "$SPEC" ]] || { echo "spec not found: $SPEC" >&2; exit 1; }
mkdir -p "$OUT_DIR"

N="$(jq 'length' "$SPEC")"
echo "[render_catalog] $N entries in $SPEC -> $OUT_DIR"

for i in $(seq 0 $((N - 1))); do
    MODEL="$(jq -r ".[$i].model" "$SPEC")"
    PRESET="$(jq -r ".[$i].preset" "$SPEC")"
    SEED="$(jq -r ".[$i].seed" "$SPEC")"
    DURATION="$(jq -r ".[$i].duration_sec" "$SPEC")"
    KIND="$(jq -r ".[$i].kind // \"piece\"" "$SPEC")"
    TITLE="$(jq -r ".[$i].title // \"\"" "$SPEC")"

    echo "[render_catalog] [$((i+1))/$N] $MODEL/$PRESET seed=$SEED ${DURATION}s kind=$KIND"
    "$SCRIPT_DIR/render_piece.sh" "$MODEL" "$PRESET" "$SEED" "$DURATION" "$OUT_DIR" --kind "$KIND" --title "$TITLE"
done

echo "[render_catalog] all renders done, building manifest..."
python3 "$SCRIPT_DIR/make_manifest.py" "$OUT_DIR"
echo "[render_catalog] complete"
