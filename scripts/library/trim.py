#!/usr/bin/env python3
# trim.py <workdir>
# Reads <workdir>/audit.json and trims silence offenders with a single ffmpeg pass.
# Non-offenders are not touched.
#
# Trim logic:
#   - Trailing: keep TAIL_KEEP_S seconds of natural audio after signal ends
#                (to=signal_end + TAIL_KEEP_S, where signal_end = duration - trailing_s)
#   - Leading:  skip to max(0, signal_start - LEAD_KEEP_S) before signal begins
#
# After trimming, each output is re-audited to verify silence is gone.
# Writes <workdir>/trim_report.json.
#
# Requires: ffmpeg (libmp3lame), ffprobe — no Python dependencies.

TAIL_KEEP_S  = 1.5   # seconds of natural tail to preserve after signal end
LEAD_KEEP_S  = 0.5   # seconds of lead-in to preserve before signal start

# Mirror the thresholds from audit.py:
TRAILING_THRESHOLD_S = 4.0
LEADING_THRESHOLD_S  = 3.0

# Silence detection params (same as audit.py):
SILENCE_NOISE = "-45dB"
SILENCE_DUR   = "2"

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def get_duration(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def detect_silence(path: str) -> list[dict]:
    result = subprocess.run(
        [
            "ffmpeg", "-i", path,
            "-af", f"silencedetect=n={SILENCE_NOISE}:d={SILENCE_DUR}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    output = result.stderr
    regions = []
    current_start = None
    for line in output.splitlines():
        m_start = re.search(r"silence_start:\s*([\d.]+)", line)
        m_end   = re.search(r"silence_end:\s*([\d.]+)", line)
        if m_start:
            current_start = float(m_start.group(1))
        if m_end and current_start is not None:
            regions.append({"start": current_start, "end": float(m_end.group(1))})
            current_start = None
    if current_start is not None:
        duration = get_duration(path)
        regions.append({"start": current_start, "end": duration})
    return regions


def measure_silence(path: str) -> tuple[float, float]:
    """Return (leading_s, trailing_s) for a file."""
    duration = get_duration(path)
    regions  = detect_silence(path)
    leading_s = trailing_s = 0.0
    for r in regions:
        if r["start"] < 0.05:
            leading_s = max(leading_s, r["end"])
        if abs(r["end"] - duration) <= 0.5:
            trailing_s = max(trailing_s, r["end"] - r["start"])
    return leading_s, trailing_s


def trim_window(record: dict) -> tuple[float, float]:
    """(trim_from, trim_to) in seconds for an audit record."""
    duration    = record["duration"]
    trailing_s  = record["trailing_s"]
    leading_s   = record["leading_s"]

    # Signal end: where the trailing silence begins
    signal_end   = duration - trailing_s
    trim_to      = signal_end + TAIL_KEEP_S   # where we cut the tail

    # Signal start: after the leading silence
    signal_start = leading_s
    trim_from    = max(0.0, signal_start - LEAD_KEEP_S)

    # Clamp
    return trim_from, min(trim_to, duration)


def trim_file(src: str, dst: str, record: dict) -> dict:
    """
    Trim src → dst.  Returns a result dict with before/after measurements.
    """
    trim_from, trim_to = trim_window(record)
    tmp_dst = dst + ".tmp.mp3"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(trim_from),
        "-to", str(trim_to),
        "-i", src,
        "-map_metadata", "0",
        "-codec:a", "libmp3lame", "-q:a", "0",
        tmp_dst,
    ]
    subprocess.run(cmd, check=True)
    os.rename(tmp_dst, dst)

    # Re-audit
    new_dur = get_duration(dst)
    new_leading, new_trailing = measure_silence(dst)

    return {
        "file":             record["file"],
        "orig_duration":    record["duration"],
        "orig_trailing_s":  record["trailing_s"],
        "orig_leading_s":   record["leading_s"],
        "trim_from":        round(trim_from, 3),
        "trim_to":          round(trim_to, 3),
        "new_duration":     round(new_dur, 3),
        "new_trailing_s":   round(new_trailing, 3),
        "new_leading_s":    round(new_leading, 3),
        "ok":               new_trailing <= TRAILING_THRESHOLD_S and new_leading <= LEADING_THRESHOLD_S,
    }


def main():
    if len(sys.argv) != 2:
        print("usage: trim.py <workdir>", file=sys.stderr)
        sys.exit(1)

    workdir    = Path(sys.argv[1])
    audit_path = workdir / "audit.json"
    orig_dir   = workdir / "original"
    trim_dir   = workdir / "trimmed"

    if not audit_path.exists():
        print(f"error: {audit_path} not found — run audit.py first", file=sys.stderr)
        sys.exit(1)

    with open(audit_path) as f:
        audit_data = json.load(f)

    records = audit_data.get("records", [])
    offenders = [
        r for r in records
        if r["trailing_s"] > TRAILING_THRESHOLD_S or r["leading_s"] > LEADING_THRESHOLD_S
    ]
    offenders.sort(key=lambda r: r["trailing_s"], reverse=True)

    print(f"[trim] {len(offenders)} offenders to trim (of {len(records)} tracks)")

    trim_dir.mkdir(exist_ok=True)
    results = []
    errors  = []

    for i, rec in enumerate(offenders, 1):
        src = str(orig_dir / rec["file"])
        dst = str(trim_dir / rec["file"])
        print(f"\n  [{i}/{len(offenders)}] {rec['file']}")
        print(f"    before: dur={rec['duration']:.1f}s  trail={rec['trailing_s']:.1f}s  lead={rec['leading_s']:.1f}s")
        try:
            result = trim_file(src, dst, rec)
            results.append(result)
            status = "OK" if result["ok"] else "WARN — silence remains"
            print(f"    after:  dur={result['new_duration']:.1f}s  trail={result['new_trailing_s']:.1f}s  lead={result['new_leading_s']:.1f}s  [{status}]")
            expected_dur = rec["duration"] - rec["trailing_s"] + TAIL_KEEP_S - max(0.0, rec["leading_s"] - LEAD_KEEP_S)
            print(f"    expected ~{expected_dur:.1f}s  got {result['new_duration']:.1f}s")
        except Exception as e:
            print(f"    ERROR: {e}")
            errors.append({"file": rec["file"], "error": str(e)})

    # Write trim_report.json
    report = {"results": results, "errors": errors}
    report_path = workdir / "trim_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\n[trim] wrote {report_path}")

    # Summary
    ok_count   = sum(1 for r in results if r["ok"])
    warn_count = len(results) - ok_count
    print(f"\n{'='*60}")
    print(f"TRIM SUMMARY: {len(offenders)} offenders processed")
    print(f"  Trimmed OK:         {ok_count}")
    if warn_count:
        print(f"  Silence remains:    {warn_count}")
    if errors:
        print(f"  Errors:             {len(errors)}")
    print(f"{'='*60}")

    # Worst-case before/after table (all results, worst trailing first)
    if results:
        results_sorted = sorted(results, key=lambda r: r["orig_trailing_s"], reverse=True)
        print(f"\n  {'FILE':<38} {'BEFORE':>8} {'AFTER':>8}  TRAIL Δ")
        print(f"  {'-'*65}")
        for r in results_sorted:
            delta = r["new_duration"] - r["orig_duration"]
            print(
                f"  {r['file']:<38} "
                f"{r['orig_duration']:>8.1f} "
                f"{r['new_duration']:>8.1f}  "
                f"{r['orig_trailing_s']:.1f}s→{r['new_trailing_s']:.1f}s  "
                f"({delta:+.1f}s)"
            )
    print()

    if errors:
        print(f"ERRORS:")
        for e in errors:
            print(f"  {e['file']}: {e['error']}")


if __name__ == "__main__":
    main()
