#!/usr/bin/env python3
"""Plan one station set: the whole library, cut into stretch VODs.

Each VOD is one ident plus 2-3 songs over the visuals, with its burned-in
titles and YouTube title/description. Pure and stdlib-only: durations come in
as data (so the core is testable without media); the CLI fills them via
ffprobe.
"""
import json
import math
import os
import random
import subprocess

UNCUT_SHARE = 0.75   # share of stretches played as one unbroken piece
MAX_SEG = 600.0      # longest any one piece stays on screen, in seconds
TITLE_MAX = 100      # YouTube's title limit
SONG_URL = "https://songs.travisbriggs.com/{slug}/"
VISUALS_URL = "https://github.com/audiodude/vxstory"


def title_from_path(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    return base.replace("-", " ").replace("_", " ").title()


def render_name(piece: dict) -> str:
    """A piece's on-screen credit: its sidecar `title`, else its model in Title Case."""
    return piece.get("title") or piece["model"].replace("_", " ").title()


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


def stretches(songs: list, rng) -> list[list]:
    """Consecutive groups of 2 or 3 songs, drawn at random; the tail is split so
    no group is ever 1 or 4 songs. Needs at least 2 songs."""
    if len(songs) < 2:
        raise ValueError(f"a set needs at least 2 songs, got {len(songs)}")
    out, i = [], 0
    while i < len(songs):
        left = len(songs) - i
        k = left if left <= 3 else rng.choice((2, 3))
        if left - k == 1:            # never strand one song
            k = 5 - k
        out.append(songs[i:i + k])
        i += k
    return out


def pick_idents(n: int, idents: list[dict], rng) -> list[dict]:
    """One ident per VOD, by weight. Never the same ident on consecutive VODs,
    including last -> first around the loop, except a `filler`, which may repeat."""
    if not idents:
        raise ValueError("the catalog has no idents")

    def draw(avoid):
        pool = [i for i in idents if i.get("filler") or i["src"] not in avoid] or idents
        return rng.choices(pool, weights=[float(i.get("weight", 1.0)) for i in pool])[0]

    out = []
    for k in range(n):
        avoid = {out[-1]["src"]} if out else set()
        if k == n - 1 and n > 1:
            avoid.add(out[0]["src"])
        out.append(draw(avoid))
    return out


class Rotation:
    """Pieces least-recently-used first; never the piece just shown."""

    def __init__(self, pieces):
        self.queue = list(pieces)
        self.last = None
        self.longest = max(p["dur"] for p in pieces)

    def take(self, length, avoid=()):
        """LRU piece covering `length`, not the last one shown nor `avoid`;
        None if no piece is long enough."""
        fits = [p for p in self.queue if p["dur"] >= length - 1e-6]
        if not fits:
            return None
        piece = next((p for p in fits if p["src"] != self.last and p["src"] not in avoid),
                     next((p for p in fits if p["src"] != self.last), fits[0]))
        self.queue.remove(piece)
        self.queue.append(piece)
        self.last = piece["src"]
        return piece


def _piece_seg(piece, length, tl_start, rng) -> dict:
    slack = max(0.0, float(piece["dur"]) - length)
    start = math.floor(rng.uniform(0.0, slack) * 1000) / 1000   # floor: never past EOF
    return {"kind": "piece", "src": piece["src"], "in": start,
            "out": round(length, 3), "tl_start": round(tl_start, 3)}


def build_visuals(span, boundaries, rot, rng, uncut_share=UNCUT_SHARE, max_seg=MAX_SEG,
                  avoid_last=()):
    """Piece segments covering VOD time [span[0], span[1]).

    Uncut (`uncut_share`): one piece for the whole span, if the span is at most
    `max_seg` and some piece is that long. Otherwise cut: a new piece at every
    song boundary, and mid-song wherever a segment would outrun `max_seg` or
    the longest piece. `avoid_last` keeps the final segment off those pieces
    (the loop seam)."""
    t, end = span
    if end - t <= 1e-6:
        return []
    if rng.random() < uncut_share and end - t <= max_seg + 1e-6:
        piece = rot.take(end - t, avoid_last)
        if piece is not None:
            return [_piece_seg(piece, end - t, t, rng)]
    edges = [t] + [b for b in boundaries if t + 1e-6 < b < end - 1e-6] + [end]
    cuts = []
    for a, b in zip(edges, edges[1:]):
        while b - a > 1e-6:
            cuts.append((a, min(b - a, rot.longest, max_seg)))
            a += cuts[-1][1]
    segs = []
    for i, (a, length) in enumerate(cuts):
        piece = rot.take(length, avoid_last if i == len(cuts) - 1 else ())
        segs.append(_piece_seg(piece, length, a, rng))
    return segs


def vod_title(songs: list[str], renders: list[str], limit=TITLE_MAX) -> str:
    """`Song A - Song B (Render 1, Render 2)`. Every song title stays; the render
    list is best effort, truncated with `...` or dropped. Songs alone over the
    limit truncate the last song."""
    base = " - ".join(songs)
    if len(base) > limit:
        return base[:limit - 3].rstrip() + "..."
    room = limit - len(base) - 3                 # " (" + ")"
    full = ", ".join(renders)
    if not renders or room < 4:                  # not even "X..."
        return base
    inner = full if len(full) <= room else full[:room - 3].rstrip(" ,") + "..."
    return f"{base} ({inner})"


def vod_description(blurb: str, songs: list[dict], renders: list[str]) -> str:
    lines = [blurb.strip(), ""] if blurb.strip() else []
    lines.append("Songs:")
    lines += [f"{s['title']} - {SONG_URL.format(slug=s['slug'])}" for s in songs]
    lines += ["", f"Visuals: {', '.join(renders)} (made with vxstory: {VISUALS_URL})"]
    return "\n".join(lines)


def _youtube_safe(text: str) -> str:
    return text.replace("<", "").replace(">", "")     # YouTube rejects angle brackets


def build_vod(songs, ident, rot, rng, avoid_last=()) -> dict:
    """One VOD: an audio ident is pre-roll (music starts after it); a silent
    ident plays over the start of song 1. Times are VOD-relative."""
    pre = ident["dur"] if ident.get("audio") else 0.0
    music, t = [], pre
    for s in songs:
        music.append({"file": s["file"], "title": s["title"], "slug": s["slug"],
                      "start": round(t, 3), "end": round(t + float(s["dur"]), 3)})
        t += float(s["dur"])
    end = music[-1]["end"]
    vis_start = min(ident["dur"], end)
    ident_seg = {"kind": "ident", "src": ident["src"], "in": 0.0,
                 "out": round(vis_start, 3), "tl_start": 0.0}
    if ident.get("audio"):
        ident_seg["audio"] = True
    edl = [ident_seg] + build_visuals((vis_start, end), [m["start"] for m in music[1:]],
                                      rot, rng, avoid_last=avoid_last)
    return {"dur": round(end, 3), "ident": {"src": ident["src"], "dur": ident["dur"],
                                            "audio": bool(ident.get("audio"))},
            "music": music, "edl": edl,
            "subtitles": [{"text": m["title"], "start": m["start"], "end": m["end"]}
                          for m in music]}


def _sha(parts) -> str:
    import hashlib
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode())
    return h.hexdigest()[:16]


def build_set(catalog: dict, library: list[dict], config: dict) -> dict:
    """`config`: seed, swap_at, fps, resolution, blurb (optional),
    uncut_share (optional)."""
    seed = int(config["seed"])
    blurb = config.get("blurb", "")
    uncut_share = float(config.get("uncut_share", UNCUT_SHARE))

    songs = sorted(library, key=lambda s: s["file"])
    random.Random(f"music:{seed}").shuffle(songs)
    groups = stretches(songs, random.Random(f"stretch:{seed}"))
    idents = pick_idents(len(groups), catalog["idents"], random.Random(f"ident:{seed}"))

    pieces = sorted(catalog["pieces"], key=lambda p: p["src"])
    random.Random(f"pieces:{seed}").shuffle(pieces)
    rot = Rotation(pieces)
    rng = random.Random(f"edl:{seed}")
    names = {p["src"]: render_name(p) for p in pieces}

    vods = []
    for k, (group, ident) in enumerate(zip(groups, idents)):
        first = vods[0]["edl"][1:2] if vods else []
        avoid = {first[0]["src"]} if k == len(groups) - 1 and first else ()
        v = build_vod(group, ident, rot, rng, avoid_last=avoid)
        renders = list(dict.fromkeys(names[s["src"]] for s in v["edl"] if s["kind"] == "piece"))
        vods.append({"index": k, "file": f"vod-{k:03d}.mp4",
                     "title": _youtube_safe(vod_title([m["title"] for m in v["music"]], renders)),
                     "description": _youtube_safe(vod_description(blurb, v["music"], renders)),
                     **v})

    return {
        "seed": seed,
        "swap_at": config["swap_at"],
        "fps": int(config["fps"]),
        "resolution": str(config["resolution"]),
        "loop_sec": round(sum(v["dur"] for v in vods), 3),
        "vods": vods,
        "catalog_sha": _sha(sorted(p["src"] for p in catalog["pieces"]) +
                            sorted(i["src"] for i in catalog["idents"])),
        "library_sha": _sha(sorted(t["file"] for t in library)),
    }


def resolve_media(entry_file: str, catalog_dir: str) -> str:
    """catalog.json records the absolute path on the machine that RENDERED the
    piece; the bake runs elsewhere (box: /data/catalog). Prefer the sibling file
    next to catalog.json, fall back to the recorded path."""
    local = os.path.join(catalog_dir, os.path.basename(entry_file))
    return local if os.path.exists(local) else entry_file


def _load_catalog(path: str) -> dict:
    """Pieces keep `model` and an optional `title` (their credit). Idents may
    carry `weight` (relative draw odds, default 1) and `filler` (may open two
    VODs in a row). Whether an ident has audio is probed."""
    with open(path) as f:
        data = json.load(f)
    here = os.path.dirname(os.path.abspath(path))
    out = {"pieces": [], "idents": []}
    for e in data["pieces"]:
        key = {"piece": "pieces", "ident": "idents"}.get(e["kind"])
        if key is None:
            continue
        f_path = resolve_media(e["file"], here)
        item = {"src": f_path, "dur": ffprobe_duration(f_path)}
        if key == "pieces":
            item.update(model=e.get("model", os.path.splitext(os.path.basename(f_path))[0]))
            if e.get("title"):
                item["title"] = e["title"]
        else:
            weight = float(e.get("weight", 1.0))
            if weight <= 0:
                raise ValueError(f"ident {e['file']}: weight must be > 0, got {weight}")
            item.update(weight=weight, filler=bool(e.get("filler", False)),
                        audio=ffprobe_has_audio(f_path))
        out[key].append(item)
    return out


DENYLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "song_denylist.txt")


def load_denylist(path: str) -> set[str]:
    """Song filenames (one per line, `#` comments) never planned into a set."""
    with open(path) as f:
        return {line.split("#", 1)[0].strip() for line in f} - {""}


def load_library(library_dir: str, denied: set[str]) -> list[dict]:
    import glob
    paths = sorted(glob.glob(os.path.join(library_dir, "*.mp3")))
    return [{"file": p, "title": title_from_path(p), "dur": ffprobe_duration(p),
             "slug": os.path.splitext(os.path.basename(p))[0]}
            for p in paths if os.path.basename(p) not in denied]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--library-dir", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--swap-at", required=True, help="ISO 8601 time the set goes on air")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--resolution", default="1920x1080")
    ap.add_argument("--blurb-file", default="", help="text opening every VOD description")
    ap.add_argument("--denylist", default=DENYLIST)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    denied = load_denylist(a.denylist)
    library = load_library(a.library_dir, denied)
    blurb = open(a.blurb_file).read() if a.blurb_file and os.path.exists(a.blurb_file) else ""
    cfg = {"seed": a.seed, "swap_at": a.swap_at, "fps": a.fps,
           "resolution": a.resolution, "blurb": blurb}
    plan_doc = build_set(_load_catalog(a.catalog), library, cfg)
    with open(a.out, "w") as f:
        json.dump(plan_doc, f, indent=2)
    print(f"[plan] wrote {a.out}: {len(plan_doc['vods'])} VODs, {len(library)} songs, "
          f"loop={plan_doc['loop_sec']:.0f}s")


if __name__ == "__main__":
    main()
