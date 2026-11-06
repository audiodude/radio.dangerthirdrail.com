import json
import os
import random
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plan


def _song(i, dur=200.0):
    return {"file": f"/m/s{i:03d}.mp3", "title": f"S{i}", "slug": f"s{i:03d}", "dur": dur}

def _piece(src, dur, model="fluid_swirl", **kw):
    return {"src": src, "dur": dur, "model": model, **kw}

IDENTS = [{"src": "loud_a.mp4", "dur": 17.0, "weight": 1.0, "audio": True},
          {"src": "loud_b.mp4", "dur": 20.0, "weight": 1.0, "audio": True},
          {"src": "loud_c.mp4", "dur": 16.0, "weight": 1.0, "audio": True},
          {"src": "filler.mp4", "dur": 12.0, "weight": 6.0, "filler": True}]

def _set(library, pieces, idents=IDENTS, seed=20261113, **cfg):
    return plan.build_set({"pieces": pieces, "idents": idents}, library,
                          {"seed": seed, "swap_at": "2026-11-13T06:00:00-08:00",
                           "fps": 60, "resolution": "1920x1080", **cfg})

def _pieces_in_order(p):
    return [s["src"] for v in p["vods"] for s in v["edl"] if s["kind"] == "piece"]


def test_title_from_path():
    assert plan.title_from_path("/x/ele-1005.mp3") == "Ele 1005"
    assert plan.title_from_path("never_havin_kids.mp3") == "Never Havin Kids"

def test_render_name_prefers_sidecar_title():
    assert plan.render_name({"model": "supernova_orbit"}) == "Supernova Orbit"
    assert plan.render_name({"model": "hydra", "title": "Radical Rainbow"}) == "Radical Rainbow"


def test_stretches_are_two_or_three_songs_and_keep_every_song_in_order():
    for n in range(2, 40):
        for seed in range(5):
            groups = plan.stretches(list(range(n)), random.Random(seed))
            assert [x for g in groups for x in g] == list(range(n))
            assert all(len(g) in (2, 3) for g in groups), (n, seed, groups)

def test_stretches_need_two_songs():
    try:
        plan.stretches([1], random.Random(0))
    except ValueError:
        pass
    else:
        assert False


def test_idents_never_repeat_back_to_back_except_filler_even_across_the_seam():
    for seed in range(200):
        out = [i["src"] for i in plan.pick_idents(30, IDENTS, random.Random(seed))]
        ring = list(zip(out, out[1:] + out[:1]))
        assert all(a != b or a == "filler.mp4" for a, b in ring), (seed, out)

def test_ident_weights_set_draw_odds_and_filler_may_repeat():
    out = [i["src"] for i in plan.pick_idents(9000, IDENTS, random.Random(1))]
    share = out.count("filler.mp4") / len(out)
    assert 0.55 < share < 0.72                    # 6 of 9 shares, minus no-repeat losses
    assert any(a == b == "filler.mp4" for a, b in zip(out, out[1:]))


def test_set_plays_every_song_exactly_once():
    lib = [_song(i, 120.0 + i) for i in range(41)]
    p = _set(lib, [_piece(f"p{i}.mp4", 600.0) for i in range(4)])
    files = [m["file"] for v in p["vods"] for m in v["music"]]
    assert sorted(files) == sorted(s["file"] for s in lib)
    assert all(len(v["music"]) in (2, 3) for v in p["vods"])
    assert [v["file"] for v in p["vods"]] == [f"vod-{k:03d}.mp4" for k in range(len(p["vods"]))]

def test_set_is_deterministic_per_seed():
    lib = [_song(i) for i in range(20)]
    pieces = [_piece(f"p{i}.mp4", 600.0) for i in range(4)]
    a, b, c = _set(lib, pieces), _set(lib, pieces), _set(lib, pieces, seed=20261120)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["vods"] != c["vods"]


def test_audio_ident_is_preroll_and_silent_ident_overlays_song_one():
    lib = [_song(0, 100.0), _song(1, 100.0)]
    pieces = [_piece("p0.mp4", 600.0), _piece("p1.mp4", 600.0)]
    loud = _set(lib, pieces, idents=[IDENTS[0]])["vods"][0]
    assert loud["edl"][0] == {"kind": "ident", "src": "loud_a.mp4", "in": 0.0, "out": 17.0,
                              "tl_start": 0.0, "audio": True}
    assert loud["music"][0]["start"] == 17.0 and loud["dur"] == 217.0
    assert loud["subtitles"][0]["start"] == 17.0
    quiet = _set(lib, pieces, idents=[IDENTS[3]])["vods"][0]
    assert quiet["music"][0]["start"] == 0.0 and quiet["dur"] == 200.0
    assert quiet["edl"][0]["out"] == 12.0 and "audio" not in quiet["edl"][0]
    assert quiet["edl"][1]["tl_start"] == 12.0          # visuals take over after it


def test_visuals_cover_each_vod_contiguously():
    lib = [_song(i, 60.0 + 37 * (i % 7)) for i in range(60)]
    p = _set(lib, [_piece(f"s{i}.mp4", 300.0) for i in range(6)] +
             [_piece(f"L{i}.mp4", 600.0) for i in range(3)])
    for v in p["vods"]:
        t = 0.0
        for seg in v["edl"]:
            assert abs(seg["tl_start"] - t) < 0.01
            assert seg["in"] >= 0.0
            t = seg["tl_start"] + seg["out"]
        assert abs(t - v["dur"]) < 0.01

def test_no_piece_stays_on_screen_longer_than_max_seg():
    # 1200 s pieces could cover anything; 3 x 290 s stretches are over the cap,
    # and a 700 s song must cut mid-song
    lib = [_song(i, 290.0) for i in range(30)] + [_song(99, 700.0)]
    p = _set(lib, [_piece(f"p{i}.mp4", 1200.0) for i in range(3)], uncut_share=1.0)
    segs = [s for v in p["vods"] for s in v["edl"] if s["kind"] == "piece"]
    assert max(s["out"] for s in segs) <= 600.0 + 1e-6
    long_vod = next(v for v in p["vods"] if any(m["file"] == "/m/s099.mp3" for m in v["music"]))
    song = next(m for m in long_vod["music"] if m["file"] == "/m/s099.mp3")
    inside = [s for s in long_vod["edl"] if song["start"] + 1 < s["tl_start"] < song["end"] - 1]
    assert inside                                    # a cut inside the 700 s song

def test_pieces_never_repeat_across_any_boundary_including_the_loop_seam():
    lib = [_song(i, 90.0 + 13 * (i % 9)) for i in range(80)]
    for seed in range(20261101, 20261131):
        p = _set(lib, [_piece(f"p{i}.mp4", 600.0) for i in range(3)], seed=seed)
        srcs = _pieces_in_order(p)
        assert all(a != b for a, b in zip(srcs, srcs[1:] + srcs[:1])), seed

def test_most_stretches_play_one_piece_when_they_can():
    lib = [_song(i, 150.0) for i in range(300)]
    p = _set(lib, [_piece(f"p{i}.mp4", 600.0) for i in range(4)])
    uncut = sum(1 for v in p["vods"] if len(v["edl"]) == 2)
    assert 0.65 < uncut / len(p["vods"]) < 0.85

def test_uncut_needs_a_piece_long_enough():
    lib = [_song(i, 200.0) for i in range(20)]
    p = _set(lib, [_piece("p0.mp4", 300.0), _piece("p1.mp4", 300.0)], uncut_share=1.0)
    for v in p["vods"]:
        starts = [m["start"] for m in v["music"][1:]]
        assert all(any(abs(s["tl_start"] - b) < 0.01 for s in v["edl"]) for b in starts)


def test_vod_title_keeps_every_song_and_truncates_renders():
    assert plan.vod_title(["A", "B"], ["Supernova Orbit", "Fluid Swirl"]) == \
        "A / B (Supernova Orbit, Fluid Swirl)"
    songs = ["Song Title Number One", "Another Song Title", "Third Song Here Okay"]
    t = plan.vod_title(songs, ["Supernova Orbit", "Radical Rainbow Insanity"])
    assert len(t) == 100 and t.startswith(" / ".join(songs) + " (")
    assert t.endswith("...)")
    # no room for even one character of a render: songs only
    assert plan.vod_title(["x" * 96], ["Supernova Orbit"]) == "x" * 96

def test_vod_title_truncates_the_last_song_when_songs_alone_overflow():
    t = plan.vod_title(["a" * 60, "b" * 60], ["Supernova Orbit"])
    assert len(t) == 100 and t.startswith("a" * 60 + " / b") and t.endswith("...")

def test_vod_metadata_names_renders_and_links_songs():
    lib = [_song(0, 100.0), _song(1, 100.0)]
    p = _set(lib, [_piece("p0.mp4", 600.0, model="supernova_orbit"),
                   _piece("p1.mp4", 600.0, title="Radical <Rainbow>")],
             idents=[IDENTS[0]], blurb="#chiptune\nMusic by DTR", uncut_share=1.0)
    v = p["vods"][0]
    render = "Supernova Orbit" if v["edl"][1]["src"] == "p0.mp4" else "Radical Rainbow"
    assert v["title"] == f"{v['music'][0]['title']} / {v['music'][1]['title']} ({render})"
    assert v["description"].startswith("#chiptune\nMusic by DTR\n")
    for m in v["music"]:
        assert f"{m['title']} - https://songs.travisbriggs.com/{m['slug']}/" in v["description"]
    assert "<" not in v["description"] + v["title"]


def test_resolve_media_prefers_sibling_of_catalog(tmp_path):
    # catalog.json records the render host's path; the bake machine has the mp4
    # next to catalog.json instead.
    local = tmp_path / "piece.mp4"
    local.write_bytes(b"x")
    assert plan.resolve_media("/home/renderbox/out/piece.mp4", str(tmp_path)) == str(local)
    # nothing local -> keep the recorded path (single-machine case)
    assert plan.resolve_media("/elsewhere/gone.mp4", str(tmp_path)) == "/elsewhere/gone.mp4"
