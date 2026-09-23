#!/usr/bin/env python3
"""Build a deterministic show plan: music timeline, video EDL, subtitle cues.

Pure and stdlib-only. Durations are passed in as data (so the core is testable
without media); the CLI wrapper fills them via ffprobe.
"""
import json
import math
import os
import random
import subprocess


def title_from_path(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("-", " ").replace("_", " ").title()


def shuffle(items: list, seed: int, no_repeat: bool = False) -> list:
    rng = random.Random(seed)
    out = list(items)
    rng.shuffle(out)
    if no_repeat:
        for i in range(1, len(out)):
            if out[i] == out[i - 1]:
                # swap with the next differing element to break the run
                for j in range(i + 1, len(out)):
                    if out[j] != out[i - 1]:
                        out[i], out[j] = out[j], out[i]
                        break
    return out


def playlist(library: list, seed: int):
    """Endless seeded shuffle: a fresh pass each time the library runs out, so a
    show longer than the library still fills its target (no song twice in a row
    across a pass seam)."""
    rng = random.Random(f"music:{seed}")
    prev = None
    while True:
        order = list(library)
        rng.shuffle(order)
        if len(order) > 1 and order[0] == prev:
            order[0], order[-1] = order[-1], order[0]
        yield from order
        prev = order[-1]


def build_music_timeline(ordered, target_dur: float) -> list[dict]:
    tl = []
    t = 0.0
    for trk in ordered:
        start = t
        end = round(start + float(trk["dur"]), 3)
        tl.append({"file": trk["file"], "title": trk["title"], "start": round(start, 3), "end": end})
        t = end
        if t >= target_dur:
            break
    return tl


def song_boundaries(timeline: list[dict]) -> list[float]:
    return [e["start"] for e in timeline[1:]]


def build_subtitles(timeline: list[dict]) -> list[dict]:
    return [{"text": e["title"], "start": e["start"], "end": e["end"]} for e in timeline]


def ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def build_edl(pieces, idents, boundaries, show_dur, dwell, straddle, rng) -> list[dict]:
    """rng picks each piece segment's in-point, so a piece that recurs through the
    show doesn't always open on the same frames. Idents always play from 0."""
    edl = []
    t = 0.0
    pi = ii = 0
    while t < show_dur - 1e-6:
        piece = pieces[pi % len(pieces)]; pi += 1
        cut_at = next((b for b in boundaries if b >= t + dwell), None)
        if cut_at is None or cut_at >= show_dur:
            edl.append(_piece_seg(piece, show_dur - t, t, rng))
            break
        ident = idents[ii % len(idents)]; ii += 1
        if straddle:
            istart, iend = cut_at - ident["dur"] / 2, cut_at + ident["dur"] / 2
        else:
            istart, iend = cut_at, cut_at + ident["dur"]
        istart = max(istart, t)
        edl.append(_piece_seg(piece, istart - t, t, rng))
        edl.append({"kind": "ident", "src": ident["src"], "in": 0.0,
                    "out": round(min(ident["dur"], show_dur - istart), 3),
                    "tl_start": round(istart, 3)})
        t = iend
    return edl


def _piece_seg(piece, length, tl_start, rng) -> dict:
    slack = max(0.0, float(piece["dur"]) - length)
    start = math.floor(rng.uniform(0.0, slack) * 1000) / 1000   # floor: never past EOF
    return {"kind": "piece", "src": piece["src"], "in": start,
            "out": round(length, 3), "tl_start": round(tl_start, 3)}


def _sha(parts) -> str:
    import hashlib
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode())
    return h.hexdigest()[:16]


def build_plan(catalog: dict, library: list[dict], config: dict) -> dict:
    seed = int(config["seed"])
    show_dur = float(config["show_dur_sec"])
    dwell = float(config["dwell_sec"])
    straddle = bool(config.get("straddle", True))

    # DWELL constraint: a piece segment runs dwell + the rest of the current song,
    # so it must fit within the shortest piece source. Songs too long to satisfy
    # that are dropped from the show rather than failing the bake — the catalog's
    # piece length is the hard limit, and one 9-minute track shouldn't block a
    # night's stream. (Lift the cap by rendering longer pieces.)
    min_piece = min((float(p["dur"]) for p in catalog["pieces"]), default=0.0)
    song_cap = min_piece - dwell
    excluded = [t["file"] for t in library if float(t["dur"]) > song_cap + 1e-6]
    library = [t for t in library if float(t["dur"]) <= song_cap + 1e-6]
    if not library:
        raise ValueError(
            f"no songs fit the DWELL constraint: dwell({dwell}) + every song "
            f"> min_piece({min_piece:.1f}). Lower dwell_sec or use longer pieces.")

    # Music: endless seeded shuffle, timeline built to >= show_dur.
    timeline = build_music_timeline(playlist(library, seed), target_dur=show_dur)
    real_dur = timeline[-1]["end"] if timeline else 0.0   # exact end of the audio
    boundaries = song_boundaries(timeline)
    subs = build_subtitles(timeline)

    pieces = shuffle([{"src": p["file"], "dur": float(p["dur"])} for p in catalog["pieces"]],
                     seed=seed + 1, no_repeat=True)
    idents = shuffle([{"src": i["file"], "dur": float(i["dur"])} for i in catalog["idents"]],
                     seed=seed + 2)
    edl = build_edl(pieces, idents, boundaries, real_dur, dwell, straddle,
                    rng=random.Random(f"edl:{seed}"))

    return {
        "seed": seed,
        "fps": int(config["fps"]),
        "resolution": str(config["resolution"]),
        "show_dur_sec": round(real_dur, 3),
        "music": timeline,
        "edl": edl,
        "subtitles": subs,
        "excluded_songs": excluded,
        "catalog_sha": _sha(sorted(p["file"] for p in catalog["pieces"]) +
                            sorted(i["file"] for i in catalog["idents"])),
        "library_sha": _sha(sorted(t["file"] for t in library)),
    }


def resolve_media(entry_file: str, catalog_dir: str) -> str:
    """catalog.json records the absolute path on the machine that RENDERED the
    piece; the bake runs elsewhere (box: /data/catalog). Prefer the sibling file
    next to catalog.json, fall back to the recorded path."""
    local = os.path.join(catalog_dir, os.path.basename(entry_file))
    return local if os.path.exists(local) else entry_file


def _load_catalog(path: str) -> dict:
    with open(path) as f:
        data = json.load(f)
    here = os.path.dirname(os.path.abspath(path))
    out = {"pieces": [], "idents": []}
    for e in data["pieces"]:
        key = {"piece": "pieces", "ident": "idents"}.get(e["kind"])
        if key is None:
            continue
        f_path = resolve_media(e["file"], here)
        out[key].append({"file": f_path, "dur": ffprobe_duration(f_path)})
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--library-dir", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--show-dur", type=float, required=True)
    ap.add_argument("--dwell", type=float, default=600.0)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--resolution", default="1280x720")
    ap.add_argument("--straddle", action="store_true", default=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import glob
    catalog = _load_catalog(a.catalog)
    library = [{"file": p, "title": title_from_path(p), "dur": ffprobe_duration(p)}
               for p in sorted(glob.glob(os.path.join(a.library_dir, "*.mp3")))]
    cfg = {"seed": a.seed, "show_dur_sec": a.show_dur, "dwell_sec": a.dwell,
           "fps": a.fps, "resolution": a.resolution, "straddle": a.straddle}
    plan_doc = build_plan(catalog, library, cfg)
    with open(a.out, "w") as f:
        json.dump(plan_doc, f, indent=2)
    dropped = plan_doc["excluded_songs"]
    if dropped:
        print(f"[plan] excluded {len(dropped)} song(s) longer than the piece/dwell "
              f"budget: {', '.join(os.path.basename(d) for d in dropped)}")
    print(f"[plan] wrote {a.out}: {len(plan_doc['edl'])} edl segs, "
          f"{len(plan_doc['music'])} songs, show_dur={plan_doc['show_dur_sec']:.1f}s")


if __name__ == "__main__":
    main()
