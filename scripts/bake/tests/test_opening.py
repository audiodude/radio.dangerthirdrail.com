import array
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import opening


def _audio(path, start=0, duration=None):
    args = ["ffmpeg", "-v", "error", "-ss", str(start), "-i", str(path)]
    if duration is not None:
        args += ["-t", str(duration)]
    return array.array("h", subprocess.check_output(
        args + ["-map", "0:a:0", "-ac", "1", "-f", "s16le", "-"]))


def _frame(path, second):
    return subprocess.check_output([
        "ffmpeg", "-v", "error", "-ss", str(second), "-i", str(path),
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])


@pytest.fixture(params=[True, False], ids=["ident-audio", "silent-ident"])
def baked_opening(tmp_path, monkeypatch, request):
    monkeypatch.setenv("BAKE_VBITRATE", "300k")
    source = tmp_path / "ident.mp4"
    command = ["ffmpeg", "-v", "error"]
    for color in ("red", "green", "blue"):
        command += ["-f", "lavfi", "-i", f"color=c={color}:s=640x360:r=10:d=0.4"]
    if request.param:
        command += ["-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=1.2"]
    command += ["-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]"]
    if request.param:
        command += ["-map", "3:a", "-c:a", "aac"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)]
    subprocess.run(command, check=True)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({
        "fps": 10, "resolution": "640x360",
        "edl": [
            {"kind": "piece", "src": "unused-piece.mp4", "in": 0, "out": 5},
            {"kind": "ident", "src": str(source), "in": 0.4, "out": 0.2},
            {"kind": "ident", "src": "unused-later-ident.mp4", "in": 0, "out": 1},
        ],
    }))
    manifest_path = opening._build_opening(
        plan_path, tmp_path / "show-2026-09-25.mkv", countdown_seconds=3)
    manifest = json.loads(manifest_path.read_text())
    return manifest_path, manifest, request.param


def test_opening_is_silent_until_complete_ident(baked_opening):
    manifest_path, manifest, has_audio = baked_opening
    paths = {name: manifest_path.parent / entry["file"] for name, entry in manifest.items()}
    for name, expected_frames in (("countdown", 30), ("hold", 10), ("ident", 12)):
        entry = manifest[name]
        assert Path(entry["file"]).name == entry["file"]
        probe = opening._probe(paths[name])
        streams = {stream["codec_type"]: stream for stream in probe["streams"]}
        assert streams["video"]["codec_name"] == "h264"
        assert streams["video"]["pix_fmt"] == "yuv420p"
        assert (streams["video"]["width"], streams["video"]["height"]) == (640, 360)
        assert streams["video"]["avg_frame_rate"] == "10/1"
        assert streams["audio"]["codec_name"] == "aac"
        assert streams["audio"]["sample_rate"] == "44100"
        assert streams["audio"]["channels"] == 2
        frame_count = subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(paths[name]),
        ], text=True)
        assert int(frame_count) == expected_frames
    for name in ("countdown", "hold"):
        samples = _audio(paths[name])
        assert max(abs(sample) for sample in samples) == 0

    # The EDL only uses the green middle of this ident. Its opening must retain
    # both the red beginning and the blue ending from the entire source.
    first = _frame(paths["ident"], 0.1)
    last = _frame(paths["ident"], 1.1)
    assert sum(first[0::3]) / len(first[0::3]) > 240
    assert sum(first[2::3]) / len(first[2::3]) < 10
    assert sum(last[0::3]) / len(last[0::3]) < 10
    assert sum(last[2::3]) / len(last[2::3]) > 240
    for start in (0.05, 1.0):
        samples = _audio(paths["ident"], start, 0.1)
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        if has_audio:
            assert rms > 1000
            # The ident's own 880 Hz tone survives both ends, not a music bed.
            crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
            assert 840 < crossings * 44100 / len(samples) < 920
        else:
            assert rms == 0


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="decoded text needs tesseract")
def test_countdown_text_reaches_one_then_zero_hold(baked_opening, tmp_path):
    manifest_path, manifest, _ = baked_opening
    for name, second, expected in (
        ("countdown", 0.5, "00:00:03"),
        ("countdown", 1.5, "00:00:02"),
        ("countdown", 2.5, "00:00:01"),
        ("hold", 0.5, "00:00:00"),
    ):
        image = tmp_path / f"{name}-{second}.png"
        subprocess.run([
            "ffmpeg", "-v", "error", "-ss", str(second),
            "-i", str(manifest_path.parent / manifest[name]["file"]),
            "-frames:v", "1", str(image),
        ], check=True)
        text = subprocess.check_output([
            "tesseract", str(image), "stdout", "--psm", "6"], text=True)
        assert text.strip().splitlines() == ["Danger Third Rail Radio", expected]
