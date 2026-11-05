#!/usr/bin/env python3
"""Validate a baked set against its plan. Empty result = valid."""
import json
import os
import subprocess
import sys


def _probe(path, stream, entry):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    return out.stdout.strip()


def validate_vod(vod: dict, path: str, resolution: str) -> list[str]:
    name = os.path.basename(path)
    if not os.path.exists(path):
        return [f"{name}: missing"]
    dur = _probe(path, "v:0", "format=duration")
    if not dur:
        return [f"{name}: no video stream / unreadable"]
    problems = []
    if abs(float(dur) - float(vod["dur"])) > 1.0:
        problems.append(f"{name}: duration {float(dur):.1f}s != plan {vod['dur']:.1f}s")
    if _probe(path, "v:0", "stream=codec_name") != "h264":
        problems.append(f"{name}: video codec not h264")
    if _probe(path, "a:0", "stream=codec_name") != "aac":
        problems.append(f"{name}: audio codec not aac (or missing)")
    w = _probe(path, "v:0", "stream=width"); h = _probe(path, "v:0", "stream=height")
    if f"{w}x{h}" != resolution:
        problems.append(f"{name}: resolution {w}x{h} != plan {resolution}")
    return problems


def validate(plan_path: str, set_dir: str) -> list[str]:
    with open(plan_path) as f:
        p = json.load(f)
    return [x for v in p["vods"]
            for x in validate_vod(v, os.path.join(set_dir, v["file"]), p["resolution"])]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--dir", required=True)
    a = ap.parse_args()
    probs = validate(a.plan, a.dir)
    if probs:
        print("INVALID:\n  " + "\n  ".join(probs), file=sys.stderr)
        sys.exit(1)
    print("valid")
