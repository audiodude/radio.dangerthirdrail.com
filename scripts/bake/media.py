#!/usr/bin/env python3
"""Which catalog mp4s the box needs, so a bake fetches only those from S3.

S3 holds every piece and song render; the box plans from catalog.json alone
(pieces and song renders carry `duration_sec`) and keeps on disk only the
idents (probed while planning) and the mp4s this week's plan uses.

  media.py idents CATALOG_JSON         ident file names, one per line
  media.py used PLAN_JSON              every mp4 the plan's EDLs play
  media.py prune DIR NAME...           delete DIR/*.mp4 not named
  media.py check CATALOG_JSON NAME...  each named mp4 is in DIR and as long
                                       as catalog.json says (catches a
                                       truncated download or a stale entry)
"""
import json
import os
import subprocess
import sys

from plan import ffprobe_duration


def idents(catalog: dict) -> list[str]:
    return sorted({os.path.basename(e["file"]) for e in catalog["pieces"] if e["kind"] == "ident"})


def used(plan: dict) -> list[str]:
    return sorted({os.path.basename(s["src"]) for v in plan["vods"] for s in v["edl"]})


def prune(directory: str, keep: set[str]) -> list[str]:
    gone = sorted(f for f in os.listdir(directory) if f.endswith(".mp4") and f not in keep)
    for f in gone:
        os.remove(os.path.join(directory, f))
    return gone


def check(catalog_path: str, names: list[str]) -> list[str]:
    """Problems with the named mp4s (empty: all good)."""
    here = os.path.dirname(os.path.abspath(catalog_path))
    with open(catalog_path) as f:
        durs = {os.path.basename(e["file"]): e.get("duration_sec")
                for e in json.load(f)["pieces"]}
    bad = []
    for n in names:
        path = os.path.join(here, n)
        if not os.path.exists(path):
            bad.append(f"{n}: missing")
            continue
        want = durs.get(n)
        if want is None:
            continue
        try:
            got = ffprobe_duration(path)
        except (subprocess.CalledProcessError, ValueError):
            bad.append(f"{n}: unreadable")
            continue
        if abs(got - float(want)) > 1e-3:
            bad.append(f"{n}: {got:.3f}s on disk, catalog says {want}s")
    return bad


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "idents":
        with open(args[0]) as f:
            print("\n".join(idents(json.load(f))))
    elif cmd == "used":
        with open(args[0]) as f:
            print("\n".join(used(json.load(f))))
    elif cmd == "prune":
        for f in prune(args[0], set(args[1:])):
            print(f"[media] removed {f}", file=sys.stderr)
    elif cmd == "check":
        bad = check(args[0], args[1:])
        for b in bad:
            print(f"[media] {b}", file=sys.stderr)
        sys.exit(1 if bad else 0)
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    main()
