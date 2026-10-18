#!/usr/bin/env python3
"""Gain-only loudness normalization: two-pass ffmpeg loudnorm in linear mode.

    normalize.py SRC DST    # normalize one file (for checking by ear / meter)

Pass 1 measures integrated loudness (I), true peak (TP) and loudness range
(LRA). Pass 2 applies one constant gain (loudnorm linear=true), so the song's
dynamics are untouched. If loudnorm reports it did anything but linear gain,
normalize() raises rather than write compressed audio.

The target is the on-air level, -21 LUFS: the bake adds no gain of its own.
Nearly the whole library is mastered with peaks at ~0 dBTP, so the target has
to sit well below the loud masters for plain gain to reach it without pushing
peaks over TARGET_TP (at -14 LUFS, 121 of 168 songs could not). Songs that
still can't reach it (quiet masters that already peak near 0 dBTP) get their
gain capped at the true-peak ceiling instead of loudnorm's dynamic fallback:
they keep their dynamics and land a little under TARGET_I ("capped").

Output is MP3 at the source's sample rate and channel count, re-encoded with
LAME VBR -q:a 0 (~245 kbps, the same setting trim.py uses). The re-encode is
lossy; at V0 it is transparent in practice.

Requires ffmpeg (libmp3lame) and ffprobe.
"""
import json
import math
import os
import subprocess
import sys

TARGET_I = -21.0   # LUFS, integrated; the on-air level (assemble.py adds no gain)
TARGET_TP = -1.0   # dBTP ceiling
TARGET_LRA = 20.0  # high enough that loudnorm never forces dynamic mode for LRA
# The V0 MP3 re-encode overshoots the true peak loudnorm saw by up to ~0.2 dB,
# so the gain cap aims this far below TARGET_TP.
CODEC_HEADROOM_DB = 0.3

# Recorded on every processed S3 object (x-amz-meta-library-process). Bump the
# version, or change a target, and the next sync re-processes every song.
PROCESS_TAG = f"trim+loudnorm-linear-v1;I={TARGET_I:g};TP={TARGET_TP:g}"


def _loudnorm_json(stderr: str) -> dict:
    """The JSON block loudnorm prints (print_format=json) at the end of stderr."""
    start = stderr.rindex("{")
    return json.loads(stderr[start:stderr.index("}", start) + 1])


def _window_args(window: tuple[float, float | None]) -> list[str]:
    start, end = window
    args = ["-ss", f"{start:.3f}"] if start > 0 else []
    return args + (["-to", f"{end:.3f}"] if end is not None else [])


def _sample_rate(path: str) -> str:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                          "-show_entries", "stream=sample_rate", "-of", "default=nw=1:nk=1", path],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def measure(src: str, window: tuple[float, float | None] = (0.0, None)) -> dict:
    """loudnorm pass 1 over the window: input_i/_tp/_lra/_thresh, target_offset."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", *_window_args(window), "-i", src, "-vn",
         "-af", f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, check=True)
    return _loudnorm_json(out.stderr)


def gain_target(m: dict) -> tuple[float, bool]:
    """(I to aim for, capped?): TARGET_I unless that gain would break the TP ceiling."""
    ceiling = float(m["input_i"]) + (TARGET_TP - CODEC_HEADROOM_DB) - float(m["input_tp"])
    if ceiling >= TARGET_I:
        return TARGET_I, False
    return math.floor(ceiling * 100) / 100, True   # round down: stay under the ceiling


def normalize(src: str, dst: str, window: tuple[float, float | None] = (0.0, None)) -> dict:
    """Write src's window, gain-normalized, to dst (MP3). Returns the measurements."""
    m = measure(src, window)
    target_i, capped = gain_target(m)
    lra = max(TARGET_LRA, float(m["input_lra"]))
    af = (f"loudnorm=I={target_i}:TP={TARGET_TP}:LRA={lra}"
          f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
          f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
          f":offset={m['target_offset']}:linear=true:print_format=json")
    tmp = dst + ".tmp.mp3"
    out = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-nostats", *_window_args(window), "-i", src,
         "-map", "0:a:0", "-map", "0:v?", "-c:v", "copy", "-map_metadata", "0",
         "-af", af, "-ar", _sample_rate(src),
         "-codec:a", "libmp3lame", "-q:a", "0", tmp],
        capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"ffmpeg failed on {src}:\n{out.stderr[-2000:]}")
    applied = _loudnorm_json(out.stderr)
    if applied["normalization_type"] != "linear":
        # Never upload compressed/limited audio by accident.
        raise RuntimeError(f"loudnorm fell back to {applied['normalization_type']} mode on {src}")
    os.replace(tmp, dst)
    return {
        "input_i": float(m["input_i"]), "input_tp": float(m["input_tp"]),
        "input_lra": float(m["input_lra"]),
        "gain_db": round(target_i - float(m["input_i"]), 2), "target_i": target_i,
        "capped": capped,
        "output_i": float(applied["output_i"]), "output_tp": float(applied["output_tp"]),
        "output_lra": float(applied["output_lra"]),
    }


def describe(r: dict) -> str:
    s = (f"{r['input_i']:.1f} -> {r['output_i']:.1f} LUFS ({r['gain_db']:+.1f} dB), "
         f"TP {r['input_tp']:.1f} -> {r['output_tp']:.1f} dBTP")
    return s + (f"; CAPPED by true peak, {TARGET_I - r['output_i']:.1f} LU under target"
                if r["capped"] else "")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: normalize.py SRC DST")
    print(describe(normalize(sys.argv[1], sys.argv[2])))
