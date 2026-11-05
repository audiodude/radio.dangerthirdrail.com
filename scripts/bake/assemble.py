#!/usr/bin/env python3
"""Realize a set plan into one upload-ready MP4 per VOD.

Per VOD: video (each EDL segment trimmed, scaled to the set resolution and
concatenated in one filter graph, titles burned, one x264 pass), audio (each
clip decoded to identical PCM, concatenated, AAC), then a stream-copy mux.
VOD files are written atomically and skipped if present, so a crashed bake
resumes where it stopped.
"""
import json
import os
import shutil
import subprocess
import tempfile

PRESET = os.environ.get("BAKE_PRESET", "slow")
CRF = os.environ.get("BAKE_CRF", "18")
MAXRATE = os.environ.get("BAKE_MAXRATE", "20M")   # peak cap: bounds a set to ~58 GB on disk
BUFSIZE = os.environ.get("BAKE_BUFSIZE", "40M")


def _ass_t(sec: float) -> str:
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec - h * 3600 - m * 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def write_ass(subs: list[dict], path: str, resolution: str):
    w, h = (int(x) for x in resolution.split("x"))
    size = round(28 * h / 720)                     # the 720p look, at any size
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Bold, "
        "Alignment, MarginL, MarginR, MarginV\n"
        f"Style: t,DejaVu Sans,{size},&H00FFFFFF,&H64000000,1,1,"
        f"{round(40 * h / 720)},{round(40 * h / 720)},{round(40 * h / 720)}\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n")
    lines = []
    for c in subs:
        # fade in 300ms; the note must be the literal ♪ glyph — ASS has no
        # \u escape, so "♪" would render verbatim (the original bug).
        lines.append(f"Dialogue: 0,{_ass_t(c['start'])},{_ass_t(c['end'])},t,"
                     f"{{\\fad(300,300)}}♪ {c['text']}")
    with open(path, "w") as f:
        f.write(head + "\n".join(lines) + "\n")


def _run(cmd):
    subprocess.run(cmd, check=True)


def assemble_vod(vod: dict, fps: int, res: str, out_path: str, work: str):
    w, h = res.split("x")

    # --- video: trim + scale every segment (720p idents mix with 1080p pieces) ---
    ass = os.path.join(work, "titles.ass")
    write_ass(vod["subtitles"], ass, res)
    inputs, chains = [], []
    for k, seg in enumerate(vod["edl"]):
        inputs += ["-ss", str(seg["in"]), "-t", str(seg["out"]), "-i", os.path.abspath(seg["src"])]
        chains.append(f"[{k}:v]scale={w}:{h}:flags=lanczos,setsar=1,fps={fps},"
                      f"format=yuv420p,setpts=PTS-STARTPTS[v{k}]")
    n = len(vod["edl"])
    graph = ";".join(chains) + ";" + "".join(f"[v{k}]" for k in range(n)) + \
        f"concat=n={n}:v=1:a=0[cat];[cat]subtitles=filename='{ass}'[vout]"
    video = os.path.join(work, "video.mp4")
    _run(["ffmpeg", "-y", "-loglevel", "warning", *inputs, "-filter_complex", graph,
          "-map", "[vout]", "-c:v", "libx264", "-preset", PRESET, "-crf", CRF,
          "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
          "-g", str(fps // 2), "-bf", "2", "-pix_fmt", "yuv420p", "-an", video])

    # --- audio: pre-roll ident (if it has sound) then the songs ---
    # The concat demuxer can't switch decoder parameters mid-stream, and the
    # library is not uniform: 48 kHz files, mono files, and at least one that is
    # PCM wearing an .mp3 name. Feeding those straight to concat once dropped a
    # whole track (2:17 of dead air). So decode each clip to identical PCM first,
    # clamped to exactly its planned slot (apad fills a short decode, -t trims a
    # long one). No gain: the library is stored at the on-air level.
    clips = [(m["file"], round(m["end"] - m["start"], 3)) for m in vod["music"]]
    if vod["ident"]["audio"]:
        clips.insert(0, (vod["ident"]["src"], vod["music"][0]["start"]))
    alist = os.path.join(work, "audio.txt")
    with open(alist, "w") as f:
        for i, (src, slot) in enumerate(clips):
            wav = os.path.join(work, f"{i:02d}.wav")
            _run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.abspath(src),
                  "-vn", "-af", "apad", "-t", str(slot),
                  "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", wav])
            f.write(f"file '{wav}'\n")
    audio = os.path.join(work, "audio.m4a")
    _run(["ffmpeg", "-y", "-loglevel", "warning", "-f", "concat", "-safe", "0", "-i", alist,
          "-c:a", "aac", "-b:a", "384k", "-ar", "48000", "-ac", "2", audio])

    # --- mux (stream-copy), atomic rename ---
    tmp_out = out_path + ".tmp.mp4"
    _run(["ffmpeg", "-y", "-loglevel", "warning", "-i", video, "-i", audio,
          "-map", "0:v", "-map", "1:a", "-c", "copy", "-shortest",
          "-movflags", "+faststart", tmp_out])
    os.replace(tmp_out, out_path)


def assemble(plan_path: str, out_dir: str):
    with open(plan_path) as f:
        p = json.load(f)
    os.makedirs(out_dir, exist_ok=True)
    for vod in p["vods"]:
        out = os.path.join(out_dir, vod["file"])
        if os.path.exists(out):
            continue
        # Intermediates go next to the output: /tmp may be small, and a VOD's
        # decoded audio and video are a few GB.
        work = tempfile.mkdtemp(prefix=".work-", dir=out_dir)
        try:
            assemble_vod(vod, int(p["fps"]), p["resolution"], out, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(f"[assemble] {vod['file']} ({vod['index'] + 1}/{len(p['vods'])}, "
              f"{vod['dur']:.0f}s)", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    assemble(a.plan, a.out_dir)
