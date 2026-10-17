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


def ffprobe_has_audio(path: str) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True)
    return bool(out.stdout.strip())


def build_edl(pieces, idents, boundaries, show_dur, dwell, straddle, rng) -> list[dict]:
    """rng picks each piece segment's in-point, so a piece that recurs through the
    show doesn't always open on the same frames, and draws each transition's ident
    by catalog weight. Idents always play from 0.

    `boundaries` and `show_dur` are music-only times. A silent ident straddles its
    song boundary with music underneath; an ident with audio is a break that
    sits between the two songs and delays the rest of the show by its duration.
    The returned EDL is in show time (breaks included)."""
    edl = []
    t = 0.0
    pi = 0
    shift = 0.0                     # total break time inserted so far
    weights = [float(i.get("weight", 1.0)) for i in idents]
    while t < show_dur + shift - 1e-6:
        piece = pieces[pi % len(pieces)]; pi += 1
        cut_at = next((b + shift for b in boundaries if b + shift >= t + dwell), None)
        if cut_at is None or cut_at >= show_dur + shift:
            edl.append(_piece_seg(piece, show_dur + shift - t, t, rng))
            break
        ident = rng.choices(idents, weights=weights)[0]
        if ident.get("audio"):
            istart, iend = cut_at, cut_at + ident["dur"]
            shift += ident["dur"]
        elif straddle:
            istart, iend = cut_at - ident["dur"] / 2, cut_at + ident["dur"] / 2
        else:
            istart, iend = cut_at, cut_at + ident["dur"]
        istart = max(istart, t)
        edl.append(_piece_seg(piece, istart - t, t, rng))
        seg = {"kind": "ident", "src": ident["src"], "in": 0.0,
               "out": round(min(ident["dur"], show_dur + shift - istart), 3),
               "tl_start": round(istart, 3)}
        if ident.get("audio"):
            seg["audio"] = True
        edl.append(seg)
        t = iend
    return edl


def apply_breaks(timeline: list[dict], edl: list[dict]) -> list[dict]:
    """Shift each song past the audio-ident breaks the EDL placed before it."""
    breaks = [(s["tl_start"], s["out"]) for s in edl if s.get("audio")]
    out, shift, bi = [], 0.0, 0
    for song in timeline:
        while bi < len(breaks) and breaks[bi][0] <= song["start"] + shift + 0.01:
            shift += breaks[bi][1]; bi += 1
        out.append({**song, "start": round(song["start"] + shift, 3),
                    "end": round(song["end"] + shift, 3)})
    return out


def pick_opening_ident(idents: list[dict], rng) -> dict:
    """Idents flagged `opening` in the catalog open the broadcast; with none
    flagged, any ident may. Drawn by weight among the eligible."""
    eligible = [i for i in idents if i.get("opening")] or idents
    return rng.choices(eligible, weights=[float(i.get("weight", 1.0)) for i in eligible])[0]


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
    music_dur = timeline[-1]["end"] if timeline else 0.0   # exact end of the audio
    boundaries = song_boundaries(timeline)

    pieces = shuffle([{"src": p["file"], "dur": float(p["dur"])} for p in catalog["pieces"]],
                     seed=seed + 1, no_repeat=True)
    idents = [{"src": i["file"], "dur": float(i["dur"]), "weight": float(i.get("weight", 1.0)),
               "audio": bool(i.get("audio")), "opening": bool(i.get("opening"))}
              for i in catalog["idents"]]
    edl = build_edl(pieces, idents, boundaries, music_dur, dwell, straddle,
                    rng=random.Random(f"edl:{seed}"))
    # Audio-ident breaks push later songs back; the show runs long by their total.
    timeline = apply_breaks(timeline, edl)
    real_dur = timeline[-1]["end"] if timeline else 0.0
    opening_ident = pick_opening_ident(idents, random.Random(f"opening:{seed}"))

    return {
        "seed": seed,
        "fps": int(config["fps"]),
        "resolution": str(config["resolution"]),
        "show_dur_sec": round(real_dur, 3),
        "music": timeline,
        "edl": edl,
        "opening_ident": {"src": opening_ident["src"], "dur": opening_ident["dur"]},
        "subtitles": build_subtitles(timeline),
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
    """Idents may carry `weight` (relative draw odds, default 1) and `opening`
    (eligible to open the broadcast). Whether an ident has audio is probed."""
    with open(path) as f:
        data = json.load(f)
    here = os.path.dirname(os.path.abspath(path))
    out = {"pieces": [], "idents": []}
    for e in data["pieces"]:
        key = {"piece": "pieces", "ident": "idents"}.get(e["kind"])
        if key is None:
            continue
        f_path = resolve_media(e["file"], here)
        item = {"file": f_path, "dur": ffprobe_duration(f_path)}
        if key == "idents":
            weight = float(e.get("weight", 1.0))
            if weight <= 0:
                raise ValueError(f"ident {e['file']}: weight must be > 0, got {weight}")
            item.update(weight=weight, opening=bool(e.get("opening", False)),
                        audio=ffprobe_has_audio(f_path))
        out[key].append(item)
    return out


DENYLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "song_denylist.txt")


def load_denylist(path: str) -> set[str]:
    """Song filenames (one per line, `#` comments) never planned into a show."""
    with open(path) as f:
        return {line.split("#", 1)[0].strip() for line in f} - {""}


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
    ap.add_argument("--denylist", default=DENYLIST)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    import glob
    catalog = _load_catalog(a.catalog)
    denied = load_denylist(a.denylist)
    paths = sorted(glob.glob(os.path.join(a.library_dir, "*.mp3")))
    skipped = [p for p in paths if os.path.basename(p) in denied]
    library = [{"file": p, "title": title_from_path(p), "dur": ffprobe_duration(p)}
               for p in paths if os.path.basename(p) not in denied]
    if skipped:
        print(f"[plan] denylisted {len(skipped)} song(s): "
              f"{', '.join(os.path.basename(p) for p in skipped)}")
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
