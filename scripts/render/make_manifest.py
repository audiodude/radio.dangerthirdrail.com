#!/usr/bin/env python3
"""make_manifest.py <out_dir>

Scans <out_dir> for *.json sidecar files (written by render_piece.sh alongside
each mp4) and emits catalog.json in that directory.
"""
import json
import sys
import os
from datetime import datetime, timezone

def main():
    if len(sys.argv) < 2:
        print("usage: make_manifest.py <out_dir>", file=sys.stderr)
        sys.exit(1)

    out_dir = sys.argv[1]
    if not os.path.isdir(out_dir):
        print(f"not a directory: {out_dir}", file=sys.stderr)
        sys.exit(1)

    pieces = []
    for fname in sorted(os.listdir(out_dir)):
        if fname == "catalog.json":
            continue
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(out_dir, fname)
        with open(fpath) as f:
            try:
                entry = json.load(f)
            except json.JSONDecodeError as e:
                print(f"warning: skipping invalid JSON {fpath}: {e}", file=sys.stderr)
                continue
        # Require expected fields
        required = {"id", "kind", "model", "preset", "seed", "duration_sec"}
        if not required.issubset(entry.keys()):
            print(f"warning: skipping {fpath} (missing fields: {required - entry.keys()})", file=sys.stderr)
            continue
        pieces.append(entry)

    if not pieces:
        print("[make_manifest] ERROR: no valid sidecars found — refusing to write an empty catalog", file=sys.stderr)
        sys.exit(1)

    catalog = {
        "pieces": pieces,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    catalog_path = os.path.join(out_dir, "catalog.json")
    with open(catalog_path, "w") as f:
        json.dump(catalog, f, indent=2)
        f.write("\n")

    n_pieces = sum(1 for p in pieces if p.get("kind") == "piece")
    n_idents = sum(1 for p in pieces if p.get("kind") == "ident")
    n_songs = sum(1 for p in pieces if p.get("kind") == "song")
    print(f"[make_manifest] wrote {catalog_path}: {n_pieces} pieces, {n_idents} idents, "
          f"{n_songs} song renders")

if __name__ == "__main__":
    main()
