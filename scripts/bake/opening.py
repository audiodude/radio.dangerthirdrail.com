#!/usr/bin/env python3
"""Bake the silent countdown, zero hold, and complete standalone opening ident."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


COUNTDOWN_SECONDS = 16 * 60


def _probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams",
         "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _timeline_duration(path):
    # AAC preroll has negative PTS even when Matroska's format start is zero.
    # Reserve it at every join so the next segment's first audio packet cannot
    # overlap the preceding segment's final padded AAC frame.
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-read_intervals", "%+#8",
        "-show_entries", "format=duration:packet=pts_time", "-of", "json", str(path)])
    probe = json.loads(result)
    first_pts = min((float(p["pts_time"]) for p in probe["packets"] if "pts_time" in p),
                    default=0.0)
    return float(probe["format"]["duration"]) - min(0.0, first_pts)


def _ass_time(seconds):
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}.00"


def _write_countdown_ass(path, resolution, seconds, *, hold=False):
    width, height = map(int, resolution.split("x"))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: opening,DejaVu Sans,{height / 16:g},&H00FFFFFF,&H00FFFFFF,"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    with open(path, "w") as out:
        out.write(header)
        for elapsed in range(seconds):
            remaining = 0 if hold else seconds - elapsed
            timer = f"{remaining // 3600:02d}:{remaining // 60 % 60:02d}:{remaining % 60:02d}"
            out.write(
                f"Dialogue: 0,{_ass_time(elapsed)},{_ass_time(elapsed + 1)},"
                f"opening,,0,0,0,,Danger Third Rail Radio\\N{timer}\n")


def _encode(inputs, video_filter, audio_map, duration, target, fps):
    bitrate = os.environ.get("BAKE_VBITRATE", "6M")
    gop = str(2 * fps)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "warning", *inputs,
        "-map", "0:v:0", "-map", audio_map,
        "-vf", video_filter, "-af", "asetpts=PTS-STARTPTS,apad",
        "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
        "-b:v", bitrate, "-minrate", bitrate, "-maxrate", bitrate,
        "-bufsize", bitrate, "-x264-params", "nal-hrd=cbr",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-avoid_negative_ts", "disabled", str(target),
    ], check=True)


def build_opening(plan_path, show_path):
    """Write show-path-stem.{countdown,hold,ident}.mkv and .opening.json.

    Production always uses the full sixteen-minute countdown. The show itself
    is neither read nor modified. Return the manifest path.
    """
    return _build_opening(plan_path, show_path, countdown_seconds=COUNTDOWN_SECONDS)


def _build_opening(plan_path, show_path, *, countdown_seconds=COUNTDOWN_SECONDS):
    """Internal short-countdown entry point for decoded-media smoke fixtures."""
    if not isinstance(countdown_seconds, int) or countdown_seconds < 1:
        raise ValueError("countdown_seconds must be a positive integer")
    with open(plan_path) as source:
        plan = json.load(source)
    # EDL order is already seeded by the planner. Never use the interior slot's
    # in/out: it may truncate the ident at the end of the music timeline.
    ident = next((entry for entry in plan["edl"] if entry["kind"] == "ident"), None)
    if ident is None:
        raise ValueError("opening requires an ident source in the plan EDL")
    ident_source = Path(ident["src"]).resolve()
    ident_probe = _probe(ident_source)
    ident_duration = float(ident_probe["format"]["duration"])
    fps = int(plan["fps"])
    resolution = plan["resolution"]
    show = Path(show_path).resolve()
    stem = show.stem
    manifest_path = show.with_name(f"{stem}.opening.json")
    silence = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    manifest = {}
    # Stage all outputs before replacing any published local media. Publish the
    # manifest last so a failed encoder cannot advertise a partial opening.
    with tempfile.TemporaryDirectory(prefix="bake_opening_") as work_dir:
        work = Path(work_dir)
        with tempfile.TemporaryDirectory(prefix=".opening_", dir=show.parent) as stage_dir:
            stage = Path(stage_dir)
            for name, duration in (("countdown", countdown_seconds), ("hold", 1)):
                ass = work / f"{name}.ass"
                _write_countdown_ass(ass, resolution, duration, hold=name == "hold")
                target = stage / f"{stem}.{name}.mkv"
                _encode(
                    ["-f", "lavfi", "-i", f"color=c=black:s={resolution}:r={fps}", *silence],
                    f"subtitles='{ass}'", "1:a:0", duration, target, fps)
                manifest[name] = {"file": target.name,
                                  "duration_sec": _timeline_duration(target)}
            target = stage / f"{stem}.ident.mkv"
            inputs = ["-i", str(ident_source)]
            has_audio = any(stream["codec_type"] == "audio" for stream in ident_probe["streams"])
            if not has_audio:
                inputs.extend(silence)
            _encode(
                inputs,
                f"setpts=PTS-STARTPTS,scale={resolution.replace('x', ':')}:flags=lanczos,"
                f"fps={fps},tpad=stop_mode=clone:stop=-1",
                "0:a:0" if has_audio else "1:a:0", ident_duration, target, fps)
            manifest["ident"] = {"file": target.name,
                                 "duration_sec": _timeline_duration(target)}
            staged_manifest = stage / manifest_path.name
            staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
            for entry in manifest.values():
                os.replace(stage / entry["file"], show.parent / entry["file"])
            os.replace(staged_manifest, manifest_path)
    print(f"[opening] wrote {manifest_path}")
    return manifest_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--show", required=True)
    args = parser.parse_args()
    build_opening(args.plan, args.show)
