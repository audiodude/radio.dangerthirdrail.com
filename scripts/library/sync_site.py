#!/usr/bin/env python3
"""Mirror songs.travisbriggs.com into the station's S3 music library.

The site's public catalog (/songs.json, every visible song) is the source of
truth. Dry run by default; pass --apply to change S3.

- Upload: songs on the site, missing from S3, and not in the bake denylist
  (scripts/bake/song_denylist.txt). Each song is processed first: mastered-in
  silence is trimmed (same thresholds as audit.py/trim.py) and loudness is
  normalized with plain gain (normalize.py), in one MP3 encode.
- Re-process: songs already in S3 whose `library-process` object metadata is
  not the current normalize.PROCESS_TAG (uploaded before normalization, by
  another tool, or under older targets). They are re-made from the site's
  file, the same way as uploads, and overwritten in place.
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
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "bake"))
import audit  # noqa: E402
import normalize  # noqa: E402
import trim  # noqa: E402
from plan import DENYLIST, load_denylist  # noqa: E402

SITE_CATALOG = "https://songs.travisbriggs.com/songs.json"
PROCESS_KEY = "library-process"   # S3 user metadata (x-amz-meta-library-process)


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


def process_tag(bucket: str, name: str) -> str | None:
    """The library-process metadata of one S3 song (None if it has none)."""
    bucket_name, _, prefix = bucket[len("s3://"):].partition("/")
    out = subprocess.run(["aws", "s3api", "head-object", "--bucket", bucket_name,
                          "--key", prefix + name, "--output", "json"],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout).get("Metadata", {}).get(PROCESS_KEY)


def stale(bucket: str, names: list[str]) -> list[str]:
    """The songs among names whose S3 copy was not made by the current processing."""
    with ThreadPoolExecutor(16) as pool:
        tags = pool.map(lambda n: process_tag(bucket, n), names)
        return [n for n, tag in zip(names, tags) if tag != normalize.PROCESS_TAG]


def prepare(name: str, url: str, work: str) -> tuple[str, dict]:
    """Download one song; trim mastered-in silence and normalize its loudness in
    one encode. Returns (path to upload, normalize.normalize() result)."""
    os.makedirs(os.path.join(work, "src"), exist_ok=True)
    src = os.path.join(work, "src", os.path.basename(url))
    req = urllib.request.Request(url, headers={"User-Agent": "DangerThirdRailLibrarySync/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(src, "wb") as f:
        f.write(resp.read())
    rec = audit.audit_file(src)
    trimmed = rec["trailing_s"] > trim.TRAILING_THRESHOLD_S or rec["leading_s"] > trim.LEADING_THRESHOLD_S
    window = trim.trim_window(rec) if trimmed else (0.0, None)
    dst = os.path.join(work, name)
    result = normalize.normalize(src, dst, window)
    if trimmed:
        print(f"  trimmed {name}: {rec['duration']:.1f}s -> {trim.get_duration(dst):.1f}s")
        leading, trailing = trim.measure_silence(dst)
        if trailing > trim.TRAILING_THRESHOLD_S or leading > trim.LEADING_THRESHOLD_S:
            print(f"  WARNING: {name} still has silence after trimming")
    os.remove(src)
    print(f"  normalized {name}: {normalize.describe(result)}")
    return dst, result


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
    to_redo = stale(bucket, sorted((have & set(site)) - denied))
    to_remove = sorted(have - set(site))

    print(f"site: {len(site)} songs   S3: {len(have)} songs   denylisted: {len(denied)}")
    print(f"processing: {normalize.PROCESS_TAG}")
    for name in to_add:
        print(f"  + {name}")
    for name in to_redo:
        print(f"  ~ {name}")
    for name in to_remove:
        print(f"  - {name}")
    if not to_add and not to_redo and not to_remove:
        print("in sync")
        return
    print(f"{len(to_add)} to upload, {len(to_redo)} to re-process (~), {len(to_remove)} to remove")
    if not a.apply:
        print("dry run; re-run with --apply to change S3")
        return

    capped = []
    with tempfile.TemporaryDirectory(prefix="library_sync_") as work:
        for name in to_add + to_redo:
            path, result = prepare(name, site[name], work)
            if result["capped"]:
                capped.append(f"{name} ({result['output_i']:.1f} LUFS)")
            # The marker rides in the same PUT as the audio: a song is either
            # replaced and tagged, or untouched and redone by the next run.
            subprocess.run(["aws", "s3", "cp", "--no-progress", "--only-show-errors",
                            "--content-type", "audio/mpeg",
                            "--metadata", json.dumps({PROCESS_KEY: normalize.PROCESS_TAG}),
                            path, bucket + name], check=True)
            os.remove(path)
            print(f"  {'replaced' if name in have else 'uploaded'} {name}")
    for name in to_remove:
        subprocess.run(["aws", "s3", "rm", "--only-show-errors", bucket + name], check=True)
        print(f"  removed {name}")
    if capped:
        print(f"gain capped by true peak, under {normalize.TARGET_I:g} LUFS: {', '.join(capped)}")


if __name__ == "__main__":
    main()
