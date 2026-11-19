#!/usr/bin/env bash
# Tiny synthetic catalog + library for fast integration tests (seconds long).
set -euo pipefail
DIR="${1:?usage: make_fixtures.sh <out_dir>}"
mkdir -p "$DIR/catalog" "$DIR/music"
mk() { ffmpeg -y -loglevel error -f lavfi -i "color=c=$2:s=320x180:r=60:d=$3" \
    -c:v libx264 -g 120 -keyint_min 120 -sc_threshold 0 -pix_fmt yuv420p "$DIR/catalog/$1"; }
mk piece_a.mp4 red 30
mk piece_b.mp4 green 30
mk ident.mp4 white 4
cat > "$DIR/catalog/catalog.json" <<JSON
{"pieces":[
  {"file":"/renderhost/out/piece_a.mp4","kind":"piece","duration_sec":30},
  {"file":"/renderhost/out/piece_b.mp4","kind":"piece","duration_sec":30},
  {"file":"$DIR/catalog/ident.mp4","kind":"ident"}
]}
JSON
tone() { ffmpeg -y -loglevel error -f lavfi -i "sine=f=$2:d=$3" -c:a libmp3lame "$DIR/music/$1"; }
tone song-one.mp3 330 6
tone song-two.mp3 440 5
tone song-three.mp3 550 7
# Off-spec members of the real library: a 48 kHz stereo track, and one that is
# AIFF/PCM wearing an .mp3 name. Both broke the concat demuxer in production.
ffmpeg -y -loglevel error -f lavfi -i "sine=f=660:d=6:r=48000" -ac 2 -ar 48000 \
    -c:a libmp3lame "$DIR/music/song-four.mp3"
ffmpeg -y -loglevel error -f lavfi -i "sine=f=770:d=6" -c:a pcm_s16be -f aiff \
    "$DIR/music/song-five.mp3"
echo "fixtures in $DIR"
