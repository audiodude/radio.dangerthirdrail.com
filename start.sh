#!/bin/bash
set -e

YOUTUBE_URL="${YOUTUBE_URL:-rtmp://a.rtmp.youtube.com/live2}"
RESOLUTION="1920x1080"
OUTPUT_RES="1280x720"
FPS="30"
MUSIC_DIR="/data/mp3"
AUDIO_PIPE="/tmp/audio_pipe"
S3_BUCKET="${S3_MUSIC_BUCKET:?ERROR: S3_MUSIC_BUCKET environment variable not set}"
YOUTUBE_STREAM_KEY="${YOUTUBE_STREAM_KEY:-test}"

# Operational window (America/Los_Angeles) — galton-stream runs the
# Godot/ffmpeg/chat/music stack for the full operational window. The
# consumer window (12:00-18:00 PT) lives inside this, bracketed by 15 min
# of warmup and 5 min of cooldown so the YouTube broadcast is live by
# 12:00 sharp and so 18:00 viewers don't get cut off mid-frame.
# Values below must match OPERATIONAL_START / OPERATIONAL_END in
# monitor/monitor.py.
OPERATIONAL_START_HM=1145
OPERATIONAL_END_HM=1805

in_operational_window() {
    [ "${FORCE_ACTIVE:-}" = "1" ] && return 0
    local hm
    hm=$(TZ=America/Los_Angeles date +%H%M)
    # $((10#$hm)) forces base-10 parsing so leading zeros (e.g. "0945")
    # don't get interpreted as octal.
    local h=$((10#$hm))
    [ "$h" -ge "$OPERATIONAL_START_HM" ] && [ "$h" -lt "$OPERATIONAL_END_HM" ]
}

# Sync music from S3 if the folder is empty (do this even outside the
# window so we're ready the moment it opens).
mkdir -p "$MUSIC_DIR"
# Always sync: it's incremental, and it picks up re-processed (loudness-normalized)
# and removed songs, which a "skip if the volume cache is non-empty" check never did.
echo "Syncing music from S3..."
aws s3 sync "$S3_BUCKET" "$MUSIC_DIR/" --delete --no-progress
echo "Music sync complete: $(ls $MUSIC_DIR/*.mp3 | wc -l) tracks"

# Main loop: wait for the operational window, stream during it, tear down
# at the end, then loop back to wait for the next day's window. The
# container stays alive 24/7 so Railway never sees a crash or a "completed"
# exit that it refuses to restart.
while true; do
    while ! in_operational_window; do
        now=$(TZ=America/Los_Angeles date '+%H:%M %Z')
        echo "Outside operational window (${OPERATIONAL_START_HM}-${OPERATIONAL_END_HM} PT), now $now; sleeping 15s..."
        sleep 15
    done
    echo "Inside operational window, starting stream components..."

    # Clean up stale X lock from previous run
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

    # Start virtual framebuffer with no cursor
    Xvfb :99 -screen 0 ${RESOLUTION}x24 -nocursor &
    export DISPLAY=:99

    # Wait for Xvfb to be ready
    sleep 2

    # Set root window to black and hide cursor
    xsetroot -solid black
    unclutter -idle 0 -root &

    # Start music player (decodes audio to pipe, writes playlist state)
    MUSIC_DIR="$MUSIC_DIR" python3 /app/scripts/music_player.py &
    MUSIC_PID=$!

    # Start title writer (reads playlist state, writes song title on wall-clock schedule)
    python3 /app/scripts/title_writer.py &
    TITLE_PID=$!
    echo $TITLE_PID > /tmp/title_writer.pid

    # Start YouTube chat poller (writes events to /tmp/chat_events.json for Godot)
    python3 /app/scripts/chat_poller.py &
    CHAT_PID=$!
    echo $CHAT_PID > /tmp/chat_poller.pid

    # Wait for pipe to be created
    sleep 2

    # CPU pinning: split cores between Godot (physics/render) and ffmpeg (encode)
    # so the encoder can't be starved by a Godot spike. Falls back to no pinning
    # if fewer than 4 cores are visible.
    NCORES=$(nproc)
    if [ "$NCORES" -ge 4 ]; then
        HALF=$((NCORES / 2))
        GODOT_CPUS="0-$((HALF - 1))"
        FFMPEG_CPUS="$HALF-$((NCORES - 1))"
        FFMPEG_THREADS=$((NCORES - HALF))
        if [ "$FFMPEG_THREADS" -gt 16 ]; then
            FFMPEG_THREADS=16
        fi
        GODOT_TASKSET="taskset -c $GODOT_CPUS"
        FFMPEG_TASKSET="taskset -c $FFMPEG_CPUS"
        echo "CPU pinning: Godot -> $GODOT_CPUS, ffmpeg -> $FFMPEG_CPUS ($FFMPEG_THREADS threads)"
    else
        GODOT_TASKSET=""
        FFMPEG_TASKSET=""
        FFMPEG_THREADS=0  # libx264 auto
        echo "CPU pinning: skipped (only $NCORES cores visible)"
    fi

    # Start Godot
    $GODOT_TASKSET godot --path /app --main-scene main.tscn --rendering-driver opengl3 &
    GODOT_PID=$!

    # Wait for Godot to initialize and start rendering
    sleep 5

    # Start FFmpeg in a retry loop — if YouTube's RTMP drops, restart FFmpeg
    # instead of tearing down the whole container. music_player already handles
    # the reader-died case by reopening the pipe.
    (
        while true; do
            $FFMPEG_TASKSET ffmpeg \
                -loglevel warning \
                -stats_period 5 \
                -thread_queue_size 256 \
                -f x11grab \
                -video_size ${RESOLUTION} \
                -framerate ${FPS} \
                -i :99 \
                -thread_queue_size 8 \
                -re \
                -f s16le \
                -ar 44100 \
                -ac 2 \
                -i "$AUDIO_PIPE" \
                -vf scale=${OUTPUT_RES} \
                -c:v libx264 \
                -preset ultrafast \
                -tune zerolatency \
                -threads ${FFMPEG_THREADS} \
                -b:v 2500k \
                -maxrate 2500k \
                -bufsize 7500k \
                -pix_fmt yuv420p \
                -g 60 \
                -c:a aac \
                -b:a 128k \
                -ar 44100 \
                -f flv \
                "${YOUTUBE_URL}/${YOUTUBE_STREAM_KEY}" || true
            echo "FFmpeg exited, restarting in 3s..."
            sleep 3
        done
    ) &
    FFMPEG_PID=$!

    # Start health server (replaces watchdog.sh — exposes /health for galton-monitor)
    python3 /app/scripts/health_server.py &
    HEALTH_PID=$!

    trap "kill $GODOT_PID $FFMPEG_PID $MUSIC_PID $TITLE_PID $CHAT_PID $HEALTH_PID 2>/dev/null; exit" SIGTERM SIGINT

    PROCESS_DIED=false
    while kill -0 $GODOT_PID 2>/dev/null && kill -0 $FFMPEG_PID 2>/dev/null && kill -0 $MUSIC_PID 2>/dev/null; do
        if ! in_operational_window; then
            echo "Active window closed, tearing down stream components..."
            break
        fi
        sleep 5
    done

    # If the loop exited because a process died (not window close), flag it
    if in_operational_window; then
        PROCESS_DIED=true
    fi

    echo "Shutting down stream components..."
    kill $GODOT_PID $FFMPEG_PID $MUSIC_PID $TITLE_PID $CHAT_PID $HEALTH_PID 2>/dev/null
    wait $GODOT_PID $FFMPEG_PID $MUSIC_PID $TITLE_PID $CHAT_PID $HEALTH_PID 2>/dev/null

    if [ "$PROCESS_DIED" = true ]; then
        echo "Process died during operational window, restarting in 10s..."
        sleep 10
    fi
done
