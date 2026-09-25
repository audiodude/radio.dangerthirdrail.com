"""Exercise the real stream-copy path, including the first audio packet."""
from array import array
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

PLAYOUT = Path(__file__).resolve().parents[1] / "playout.sh"
sys.path.insert(0, str(PLAYOUT.parents[1] / "bake"))
import opening


def wait_phase(state, phase, process, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, "playout exited unexpectedly"
        try:
            value = json.loads(state.read_text())
            if value["phase"] == phase:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"playout never reached {phase}")


def make_segment(path, color, duration, frequency=None):
    audio = (f"sine=frequency={frequency}:sample_rate=44100"
             if frequency else "anullsrc=r=44100:cl=stereo")
    subprocess.run([
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color={color}:s=160x90:r=25",
        "-f", "lavfi", "-i", audio, "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-g", "25", "-c:a", "aac", "-ar", "44100", "-ac", "2",
        "-avoid_negative_ts", "disabled", str(path),
    ], check=True)
    return {"file": path.name, "duration_sec": opening._timeline_duration(path)}


def audio_hashes(path):
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_packets",
        "-show_data_hash", "sha256", "-show_entries", "packet=data_hash",
        "-of", "json", str(path)], text=True)
    return [p["data_hash"] for p in json.loads(result)["packets"]]


def stop(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        pytest.fail("playout did not shut down")


def test_late_live_gate_preserves_complete_ident_and_first_song(tmp_path):
    show = tmp_path / "show-test.mkv"
    make_segment(show, "red", 3, frequency=997)
    parts = {
        "countdown": make_segment(tmp_path / "countdown.mkv", "blue", 2),
        "hold": make_segment(tmp_path / "hold.mkv", "black", 1),
        "ident": make_segment(tmp_path / "ident.mkv", "green", 1),
    }
    show.with_suffix(".opening.json").write_text(json.dumps(parts))
    (tmp_path / "LATEST").write_text(show.name)
    state = tmp_path / "state.json"
    release = tmp_path / "release.json"
    output = tmp_path / "broadcast.flv"
    env = dict(os.environ, SHOWS_DIR=str(tmp_path), OUTPUT=str(output),
               PLAYOUT_STATE=str(state), PLAYOUT_RELEASE=str(release),
               PLAYOUT_RESTART=str(tmp_path / "restart.json"),
               PLAYOUT_HEARTBEAT=str(tmp_path / "heartbeat"),
               WINDOW_CMD="true", SUPERVISE_INTERVAL="0.1")
    with (tmp_path / "playout.log").open("w") as log:
        process = subprocess.Popen(["bash", str(PLAYOUT)], env=env, stdout=log, stderr=log)
        try:
            opening = wait_phase(state, "waiting", process)
            release.write_text(json.dumps({"run_id": "stale-connection"}))
            time.sleep(2)
            assert json.loads(state.read_text()) == opening
            release.write_text(json.dumps({"run_id": opening["run_id"]}))
            wait_phase(state, "ident", process)
            wait_phase(state, "show", process)
            wait_phase(state, "idle", process)
        finally:
            stop(process)
    # Every encoded audio packet, including the first and last, reaches output
    # intact and in order. This catches clipping at the MPEG-TS/FLV boundary.
    original = audio_hashes(show)
    broadcast = audio_hashes(output)
    start = broadcast.index(original[0])
    assert broadcast[start:start + len(original)] == original
    packet_info = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_packets",
        "-show_entries", "packet=codec_type,dts_time,duration_time",
        "-of", "json", str(output)]))["packets"]
    for codec_type in ("video", "audio"):
        packets = [p for p in packet_info if p["codec_type"] == codec_type]
        for previous, current in zip(packets, packets[1:]):
            delta = float(current["dts_time"]) - float(previous["dts_time"])
            assert delta > 0
            if codec_type == "audio":
                assert delta >= float(previous["duration_time"]) - 0.001
    # Decode every frame: full ident precedes the first show frame, never
    # overlays its music. The three solid-color sources make cuts unambiguous.
    pixels = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(output), "-map", "0:v:0",
        "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-fps_mode", "passthrough",
        "-f", "rawvideo", "pipe:1"])
    colors = [tuple(pixels[i:i + 3]) for i in range(0, len(pixels), 3)]
    green = [i for i, (r, g, b) in enumerate(colors) if g > 70 and r < 30 and b < 30]
    red = [i for i, (r, g, b) in enumerate(colors) if r > 180 and g < 30 and b < 30]
    assert len(green) == 25
    assert len(red) == 75
    assert max(green) < min(red)
    audio = array("h", subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(output), "-map", "0:a:0",
        "-ar", "44100", "-ac", "2", "-f", "s16le", "pipe:1"]))
    first_sound = next(i for i, sample in enumerate(audio) if abs(sample) > 1000)
    assert first_sound / (44100 * 2) >= min(red) / 25 - 0.05


def test_legacy_show_without_opening_cannot_go_on_air(tmp_path):
    (tmp_path / "show-old.mkv").write_bytes(b"legacy show")
    (tmp_path / "LATEST").write_text("show-old.mkv")
    output = tmp_path / "broadcast.flv"
    env = dict(os.environ, SHOWS_DIR=str(tmp_path), OUTPUT=str(output),
               PLAYOUT_STATE=str(tmp_path / "state.json"), WINDOW_CMD="true")
    process = subprocess.Popen(["bash", str(PLAYOUT)], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_phase(tmp_path / "state.json", "idle", process)
        time.sleep(0.5)
        assert not output.exists()
    finally:
        stop(process)
