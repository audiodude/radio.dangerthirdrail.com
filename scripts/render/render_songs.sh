#!/usr/bin/env bash
# render_songs.sh <music_dir> <out_dir> [--jobs K] [--only FILE] [--dry-run]
# SONG_VERSIONS (3) song renders per library mp3 (minus the bake's denylist),
# each a different sketch, synced to that song; then rebuilds the manifest.
# <music_dir> must hold the exact files the bake airs: a mirror of
# S3_MUSIC_BUCKET. A song's sketches are the SONG_VERSIONS presets ranking
# highest by hash(song, preset) (rendezvous hashing), with a seed from the same
# hash: adding songs never touches others, and adding a sketch only moves the
# songs that now rank it in their top SONG_VERSIONS.
#
# --jobs K    K renders at once (default 1; their logs interleave). A failed
#             song doesn't stop the others; the run exits 1 at the end. A song
#             is one unit of work: all its versions render in the same slot.
# --only FILE render only the song file names listed in FILE, one per line.
# --dry-run   print the songs with any version missing or stale (no sidecar, or its
#             song_sha256 isn't the mp3's), one per line; render nothing.
#
# Env: SONG_MODEL (hydra), SONG_PRESETS (the ten sketches below), SONG_VERSIONS
# (3), plus
# render_piece.sh's (VXSTORY_DIR, RENDER_RES, RENDER_TMPDIR).
set -euo pipefail

USAGE='usage: render_songs.sh <music_dir> <out_dir> [--jobs K] [--only FILE] [--dry-run]'
JOBS=1 ONLY="" DRY_RUN=0 ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --jobs) JOBS="${2:?$USAGE}"; shift 2 ;;
        --only) ONLY="${2:?$USAGE}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -*) echo "unknown arg: $1" >&2; echo "$USAGE" >&2; exit 1 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
[[ ${#ARGS[@]} -eq 2 ]] || { echo "$USAGE" >&2; exit 1; }
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo "--jobs must be a positive integer" >&2; exit 1; }
MUSIC_DIR="${ARGS[0]}"
OUT_DIR="${ARGS[1]}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DENYLIST="$SCRIPT_DIR/../bake/song_denylist.txt"
MODEL="${SONG_MODEL:-hydra}"
read -r -a PRESETS <<< "${SONG_PRESETS:-babylon_tile fold glass halftone hex_tunnel mandala organ_pipes slices third_eye tiger}"
VERSIONS="${SONG_VERSIONS:-3}"
[[ "$VERSIONS" =~ ^[1-9][0-9]*$ ]] && (( VERSIONS <= ${#PRESETS[@]} )) \
    || { echo "SONG_VERSIONS must be 1..${#PRESETS[@]}" >&2; exit 1; }

# --dry-run's stdout is the song list only; progress goes to stderr
if (( DRY_RUN )); then exec 3>&2; else exec 3>&1; fi
log() { echo "[render_songs] $*" >&3; }

denied() { sed 's/#.*//; s/[[:space:]]*$//; s/^[[:space:]]*//' "$DENYLIST" | grep -qxF "$1"; }

mapfile -t SONGS < <(find "$MUSIC_DIR" -maxdepth 1 -name '*.mp3' -printf '%f\n' | sort)
if [[ -n "$ONLY" ]]; then
    mapfile -t WANT < <(sed 's/[[:space:]]*$//; s/^[[:space:]]*//; /^$/d' "$ONLY" | sort -u)
    missing="$(comm -23 <(printf '%s\n' "${WANT[@]}") <(printf '%s\n' "${SONGS[@]}"))"
    [[ -z "$missing" ]] || { echo "--only songs not in $MUSIC_DIR:" >&2; echo "$missing" >&2; exit 1; }
    SONGS=("${WANT[@]}")
fi
[[ ${#SONGS[@]} -gt 0 ]] || { echo "no mp3s in $MUSIC_DIR" >&2; exit 1; }
log "${#SONGS[@]} songs in $MUSIC_DIR -> $OUT_DIR (${PRESETS[*]})"

# versions <song>: "preset seed title" lines, one per version
versions() {
    local p h words
    for p in "${PRESETS[@]}"; do
        h="$(printf '%s\0%s' "$1" "$p" | sha256sum)"
        echo "${h:0:12} $p $(( 16#${h:12:6} % 9000 + 1000 ))"
    done | sort -r | head -n "$VERSIONS" | while read -r _ p seed; do
        read -r -a words <<< "${p//_/ }"
        echo "$p $seed ${MODEL^} ${words[*]^}"     # hex_tunnel -> Hydra Hex Tunnel
    done
}

TODO=()
for i in "${!SONGS[@]}"; do
    song="${SONGS[$i]}"
    if denied "$song"; then
        log "[$((i+1))/${#SONGS[@]}] $song: denylisted, skipped"
        continue
    fi
    if (( DRY_RUN )); then
        # By sidecar alone: render_piece.sh writes it after the mp4, and runpod_songs.py
        # uploads it after the mp4, so <out_dir> can be a mirror of S3's sidecars only
        sha="$(sha256sum "$MUSIC_DIR/$song" | cut -d' ' -f1)"
        while read -r preset seed _; do
            out="$OUT_DIR/${MODEL}_${preset}_s${seed}_${song%.*}"
            if [[ "$(jq -r '.song_sha256 // ""' "$out.json" 2>/dev/null)" != "$sha" ]]; then
                echo "$song"
                break
            fi
        done < <(versions "$song")
        continue
    fi
    TODO+=("$((i+1))" "$song")
done
(( DRY_RUN )) && exit 0

FAILED="$(mktemp)"
trap 'rm -f "$FAILED"' EXIT
render_one() {
    local song="$2" preset seed title bad=0
    while read -r preset seed title; do
        echo "[render_songs] [$1/$N_SONGS] $song: $MODEL/$preset seed=$seed"
        "$SCRIPT_DIR/render_piece.sh" "$MODEL" "$preset" "$seed" song "$OUT_DIR" \
            --audio "$MUSIC_DIR/$song" --title "$title" </dev/null || bad=1
    done < <(versions "$song")
    if (( bad )); then
        echo "[render_songs] FAILED: $song" >&2
        echo "$song" >> "$FAILED"
        return 1
    fi
}
export -f render_one versions
export MODEL MUSIC_DIR OUT_DIR SCRIPT_DIR FAILED VERSIONS N_SONGS="${#SONGS[@]}" PRESETS_STR="${PRESETS[*]}"
# Arrays don't export; the child rebuilds PRESETS from PRESETS_STR.
# xargs exits 123 when some song failed (listed in $FAILED); anything else is fatal.
if [[ ${#TODO[@]} -gt 0 ]]; then
    rc=0
    printf '%s\0' "${TODO[@]}" | xargs -0 -n2 -P "$JOBS" \
        bash -c 'read -r -a PRESETS <<< "$PRESETS_STR"; render_one "$@"' _ || rc=$?
    (( rc == 0 || rc == 123 )) || exit "$rc"
fi

python3 "$SCRIPT_DIR/make_manifest.py" "$OUT_DIR"
if [[ -s "$FAILED" ]]; then
    echo "[render_songs] $(wc -l < "$FAILED") song(s) FAILED:" >&2
    sed 's/^/  /' "$FAILED" >&2
    exit 1
fi
echo "[render_songs] complete"
