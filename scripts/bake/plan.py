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


class Bag:
    """Marble bag: `weight` marbles per child, shuffled, drawn without
    replacement. A child is an item or another Bag (a marble that opens a
    sub-bag). A draw takes the first marble whose child can satisfy `ok`;
    marbles that can't stay for later draws. When none left can, the rest are
    dropped and the bag refills. Zero-weight children never come out."""

    def __init__(self, children, rng):
        self.children = [(c, w) for c, w in children if w > 0]
        self.rng = rng
        self.marbles = []
        self.leaves = [x for c, _ in self.children
                       for x in (c.leaves if isinstance(c, Bag) else [c])]

    def can(self, ok) -> bool:
        return any(ok(x) for x in self.leaves)

    def draw(self, ok=lambda x: True):
        """An item satisfying `ok`, or None if no item ever can."""
        if not self.can(ok):
            return None
        for _ in range(2):
            for k, c in enumerate(self.marbles):
                if c.can(ok) if isinstance(c, Bag) else ok(c):
                    del self.marbles[k]
                    return c.draw(ok) if isinstance(c, Bag) else c
            self.marbles = [c for c, w in self.children for _ in range(w)]
            self.rng.shuffle(self.marbles)


def pick_idents(n: int, idents: list[dict], rng) -> list[dict]:
    """One ident per VOD, from a marble bag of `weight` marbles each. Never the
    same ident on consecutive VODs, including last -> first around the loop,
    except a `filler`, which may repeat."""
    if not idents:
        raise ValueError("the catalog has no idents")
    bag = Bag([(i, int(i.get("weight", 1))) for i in idents], rng)
    out = []
    for k in range(n):
        avoid = {out[-1]["src"]} if out else set()
        if k == n - 1 and n > 1:
            avoid.add(out[0]["src"])
        out.append(bag.draw(lambda i: i.get("filler") or i["src"] not in avoid) or bag.draw())
    return out


def _weight(node: dict, where: str) -> int:
    w = node.get("weight", 1) if isinstance(node, dict) else node
    if not isinstance(w, int) or isinstance(w, bool) or w < 0:
        raise ValueError(f"weights {where}: weight must be a whole number >= 0, got {w!r}")
    return w


def piece_bag(pieces: list[dict], weights: dict, rng) -> Bag:
    """Nested bags: class (e.g. hydra vs godot) -> model -> preset -> piece.
    `weights` (weights.json): {class: {weight, default?, models: {model:
    {weight, presets: {preset: weight}}}}}. Unlisted models join the `default`
    class; any weight left out is 1."""
    default = [c for c, v in weights.items() if v.get("default")]
    if len(default) != 1:
        raise ValueError(f"weights: need exactly one default class, got {default}")
    home = {}
    for c, v in weights.items():
        for m in v.get("models", {}):
            if m in home:
                raise ValueError(f"weights: model {m} is in both {home[m]} and {c}")
            home[m] = c
    tree = {}
    for p in sorted(pieces, key=lambda p: p["src"]):
        c = home.get(p["model"], default[0])
        tree.setdefault(c, {}).setdefault(p["model"], {}).setdefault(p.get("preset", ""), []).append(p)
    classes = []
    for c in sorted(tree):
        cw = weights.get(c, {})
        models = []
        for m in sorted(tree[c]):
            mw = cw.get("models", {}).get(m, {})
            presets = [(Bag([(p, 1) for p in tree[c][m][s]], rng),
                        _weight(mw.get("presets", {}).get(s, 1), f"{c}/{m}/{s}"))
                       for s in sorted(tree[c][m])]
            models.append((Bag(presets, rng), _weight(mw, f"{c}/{m}")))
        classes.append((Bag(models, rng), _weight(cw, c)))
    bag = Bag(classes, rng)
    if not bag.leaves:
        raise ValueError("weights leave no piece to play")
    return bag


class Rotation:
    """Pieces from the nested marble bag; never the piece just shown."""

    def __init__(self, bag: Bag):
        self.bag = bag
        self.last = None
        self.longest = max(p["dur"] for p in bag.leaves)

    def take(self, length, avoid=()):
        """A piece covering `length`, not the last one shown nor `avoid` if
        possible; None if no piece is long enough."""
        def fits(p):
            return p["dur"] >= length - 1e-6
        piece = (self.bag.draw(lambda p: fits(p) and p["src"] != self.last and p["src"] not in avoid)
                 or self.bag.draw(lambda p: fits(p) and p["src"] != self.last)
                 or self.bag.draw(fits))
        if piece is not None:
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
    """`Song A / Song B (Render 1, Render 2)`. Every song title stays; the render
    list is best effort, truncated with `...` or dropped. Songs alone over the
    limit truncate the last song. (Not " - ": "Foo - Bar" reads as song Foo by Bar.)"""
    base = " / ".join(songs)
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


def _visuals(music, span, synced, rot, rng, avoid_last=()) -> list[dict]:
    """Visuals for VOD time `span`. A song with a song render (`synced`, keyed
    by file name) plays it from the song's first frame to its last, unbroken
    (exempt from MAX_SEG: the render IS the song); every run of other songs
    gets pieces from the rotation, as `build_visuals` lays them out."""
    vis_start, end = span
    starts = [m["start"] for m in music[1:]]
    segs, run = [], None                     # run: start of pending unsynced songs
    for m in music:
        r = synced.get(os.path.basename(m["file"]))
        if r is None:
            if run is None:
                run = max(m["start"], vis_start)
            continue
        if run is not None:
            segs += build_visuals((run, m["start"]), starts, rot, rng)
            run = None
        a = max(m["start"], vis_start)       # a silent ident covers its opening
        if m["end"] - a > 1e-6:
            segs.append({"kind": "piece", "src": r["src"], "in": round(a - m["start"], 3),
                         "out": round(m["end"] - a, 3), "tl_start": round(a, 3),
                         "synced": True})
    if run is not None:
        segs += build_visuals((run, end), starts, rot, rng, avoid_last=avoid_last)
    return segs


def build_vod(songs, ident, rot, rng, avoid_last=(), synced=None) -> dict:
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
    edl = [ident_seg] + _visuals(music, (vis_start, end), synced or {}, rot, rng,
                                 avoid_last=avoid_last)
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


def match_song_renders(renders: list[dict], library: list[dict]) -> dict:
    """Song renders usable this set, keyed by song file name: the song is in the
    library, its bytes (sha256) are the ones the render was made from, and the
    render covers the whole song. Of several, the newest wins. A stale render
    would drift off the music, so it is dropped (logged) and the song gets
    rotation pieces."""
    import sys
    songs = {os.path.basename(s["file"]): s for s in library}
    out = {}
    for r in sorted(renders, key=lambda r: r.get("rendered_at", "")):
        s = songs.get(r["song"])
        if s is None:
            continue
        if r.get("song_sha256") != s.get("sha256"):
            print(f"[plan] stale song render {r['src']}: {r['song']} changed since", file=sys.stderr)
        elif float(r["dur"]) < float(s["dur"]) - 1e-3:
            print(f"[plan] short song render {r['src']}: {r['dur']}s < {s['dur']}s", file=sys.stderr)
        else:
            out[r["song"]] = r
    return out


def build_set(catalog: dict, library: list[dict], config: dict) -> dict:
    """`config`: seed, swap_at, fps, resolution, blurb (optional),
    uncut_share (optional), weights (optional, see `piece_bag`)."""
    seed = int(config["seed"])
    blurb = config.get("blurb", "")
    uncut_share = float(config.get("uncut_share", UNCUT_SHARE))

    songs = sorted(library, key=lambda s: s["file"])
    random.Random(f"music:{seed}").shuffle(songs)
    groups = stretches(songs, random.Random(f"stretch:{seed}"))
    idents = pick_idents(len(groups), catalog["idents"], random.Random(f"ident:{seed}"))

    pieces = catalog["pieces"]
    weights = config.get("weights") or {"all": {"default": True}}
    rot = Rotation(piece_bag(pieces, weights, random.Random(f"pieces:{seed}")))
    rng = random.Random(f"edl:{seed}")
    synced = match_song_renders(catalog.get("songs", []), library)
    names = {p["src"]: render_name(p) for p in pieces + list(synced.values())}

    vods = []
    for k, (group, ident) in enumerate(zip(groups, idents)):
        first = vods[0]["edl"][1:2] if vods else []
        avoid = {first[0]["src"]} if k == len(groups) - 1 and first else ()
        v = build_vod(group, ident, rot, rng, avoid_last=avoid, synced=synced)
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
    """Pieces keep `model`, `preset` and an optional `title` (their credit).
    Idents may carry `weight` (marbles in the ident bag, a whole number,
    default 1) and `filler` (may open two VODs in a row). Whether an ident has
    audio is probed. Piece weights live in weights.json, not sidecars."""
    with open(path) as f:
        data = json.load(f)
    here = os.path.dirname(os.path.abspath(path))
    out = {"pieces": [], "idents": [], "songs": []}
    for e in data["pieces"]:
        key = {"piece": "pieces", "ident": "idents", "song": "songs"}.get(e["kind"])
        if key is None:
            continue
        f_path = resolve_media(e["file"], here)
        item = {"src": f_path, "dur": ffprobe_duration(f_path)}
        if key in ("pieces", "songs"):
            item.update(model=e.get("model", os.path.splitext(os.path.basename(f_path))[0]))
            if e.get("title"):
                item["title"] = e["title"]
            if key == "songs":
                item.update(song=e["song"], song_sha256=e["song_sha256"],
                            rendered_at=e.get("rendered_at", ""))
        else:
            weight = e.get("weight", 1)
            if weight != int(weight) or weight <= 0:
                raise ValueError(f"ident {e['file']}: weight must be a whole number > 0, got {weight}")
            item.update(weight=int(weight), filler=bool(e.get("filler", False)),
                        audio=ffprobe_has_audio(f_path))
        out[key].append(item)
    return out


DENYLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "song_denylist.txt")
WEIGHTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "weights.json")


def load_denylist(path: str) -> set[str]:
    """Song filenames (one per line, `#` comments) never planned into a set."""
    with open(path) as f:
        return {line.split("#", 1)[0].strip() for line in f} - {""}


def sha256_file(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_library(library_dir: str, denied: set[str]) -> list[dict]:
    """`sha256` lets a song render prove it was made from these exact bytes."""
    import glob
    paths = sorted(glob.glob(os.path.join(library_dir, "*.mp3")))
    return [{"file": p, "title": title_from_path(p), "dur": ffprobe_duration(p),
             "slug": os.path.splitext(os.path.basename(p))[0], "sha256": sha256_file(p)}
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
    ap.add_argument("--weights", default=WEIGHTS, help="piece marble-bag weights")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    denied = load_denylist(a.denylist)
    library = load_library(a.library_dir, denied)
    blurb = open(a.blurb_file).read() if a.blurb_file and os.path.exists(a.blurb_file) else ""
    with open(a.weights) as f:
        weights = json.load(f)
    cfg = {"seed": a.seed, "swap_at": a.swap_at, "fps": a.fps,
           "resolution": a.resolution, "blurb": blurb, "weights": weights}
    plan_doc = build_set(_load_catalog(a.catalog), library, cfg)
    with open(a.out, "w") as f:
        json.dump(plan_doc, f, indent=2)
    synced = sum(1 for v in plan_doc["vods"] for s in v["edl"] if s.get("synced"))
    print(f"[plan] wrote {a.out}: {len(plan_doc['vods'])} VODs, {len(library)} songs "
          f"({synced} with song renders), loop={plan_doc['loop_sec']:.0f}s")


if __name__ == "__main__":
    main()
