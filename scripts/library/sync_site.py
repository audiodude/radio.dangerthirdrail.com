#!/usr/bin/env python3
"""Mirror songs.travisbriggs.com into the station's S3 music library.

The site's public catalog (/songs.json, every visible song) is the source of
truth. Dry run by default; pass --apply to change S3.

- Upload: songs on the site, missing from S3, and not in the bake denylist
  (scripts/bake/song_denylist.txt). Mastered-in silence is trimmed first, with
  the same thresholds as audit.py/trim.py.
- Remove: mp3s in S3 that are not on the site.

Denylisted songs already in S3 are left alone; the bake skips them anyway.
Changed audio under an existing slug is not detected.

Requires AWS credentials in the environment and S3_MUSIC_BUCKET
(e.g. s3://bucket/soundtrack/).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "bake"))
import audit  # noqa: E402
import trim  # noqa: E402
from plan import DENYLIST, load_denylist  # noqa: E402

SITE_CATALOG = "https://songs.travisbriggs.com/songs.json"


def site_songs(url: str) -> dict[str, str]:
    """{filename: audio URL} for every visible song on the site."""
    req = urllib.request.Request(url, headers={"User-Agent": "DangerThirdRailLibrarySync/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        songs = json.load(resp)
    return {f"{s['slug']}.mp3": s["src"] for s in songs}


def s3_songs(bucket: str) -> set[str]:
    """mp3 filenames directly under the s3://bucket/prefix/ URI (empty is fine)."""
    name, _, prefix = bucket[len("s3://"):].partition("/")
    out = subprocess.run(["aws", "s3api", "list-objects-v2", "--bucket", name, "--prefix", prefix,
                          "--query", "Contents[].Key", "--output", "json"],
                         capture_output=True, text=True, check=True)
    keys = (k[len(prefix):] for k in json.loads(out.stdout) or [])
    return {k for k in keys if k.endswith(".mp3") and "/" not in k}


def prepare(url: str, work: str) -> str:
    """Download one song; return the path to upload (trimmed if it had silence)."""
    name = os.path.basename(url)
    src = os.path.join(work, name)
    req = urllib.request.Request(url, headers={"User-Agent": "DangerThirdRailLibrarySync/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(src, "wb") as f:
        f.write(resp.read())
    rec = audit.audit_file(src)
    if rec["trailing_s"] <= trim.TRAILING_THRESHOLD_S and rec["leading_s"] <= trim.LEADING_THRESHOLD_S:
        return src
    os.makedirs(os.path.join(work, "trimmed"), exist_ok=True)
    dst = os.path.join(work, "trimmed", name)
    result = trim.trim_file(src, dst, rec)
    print(f"  trimmed {name}: {result['orig_duration']:.1f}s -> {result['new_duration']:.1f}s")
    if not result["ok"]:
        print(f"  WARNING: {name} still has silence after trimming")
    return dst


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="change S3 (default: dry run)")
    ap.add_argument("--site", default=SITE_CATALOG)
    ap.add_argument("--bucket", default=os.environ.get("S3_MUSIC_BUCKET"))
    ap.add_argument("--denylist", default=DENYLIST)
    a = ap.parse_args()
    if not a.bucket:
        sys.exit("S3_MUSIC_BUCKET (or --bucket) is required")
    bucket = a.bucket.rstrip("/") + "/"

    site = site_songs(a.site)
    if not site:
        sys.exit(f"refusing to sync: {a.site} lists no songs")
    have = s3_songs(bucket)
    denied = load_denylist(a.denylist)
    to_add = sorted(set(site) - have - denied)
    to_remove = sorted(have - set(site))

    print(f"site: {len(site)} songs   S3: {len(have)} songs   denylisted: {len(denied)}")
    for name in to_add:
        print(f"  + {name}")
    for name in to_remove:
        print(f"  - {name}")
    if not to_add and not to_remove:
        print("in sync")
        return
    if not a.apply:
        print("dry run; re-run with --apply to change S3")
        return

    with tempfile.TemporaryDirectory(prefix="library_sync_") as work:
        for name in to_add:
            path = prepare(site[name], work)
            subprocess.run(["aws", "s3", "cp", "--no-progress", "--only-show-errors",
                            path, bucket + name], check=True)
            print(f"  uploaded {name}")
    for name in to_remove:
        subprocess.run(["aws", "s3", "rm", "--only-show-errors", bucket + name], check=True)
        print(f"  removed {name}")


if __name__ == "__main__":
    main()
