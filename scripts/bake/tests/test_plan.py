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

def test_bag_draws_exact_shares_per_refill_and_keeps_skipped_marbles():
    bag = plan.Bag([("a", 2), ("b", 6), ("off", 0)], random.Random(3))
    for _ in range(5):
        out = [bag.draw() for _ in range(8)]
        assert sorted(out) == ["a"] * 2 + ["b"] * 6           # every bag of 8: exactly 2 a's
    picks = [bag.draw(lambda x: x == "a") for _ in range(2)] + [bag.draw() for _ in range(6)]
    assert sorted(picks) == ["a"] * 2 + ["b"] * 6             # skipped b's stayed in the bag

WEIGHTS = {"hydra": {"weight": 1, "models": {"hydra": {"presets": {"cells": 3, "fold": 1}}}},
           "godot": {"weight": 2, "default": True,
                     "models": {"fluid_swirl": {"weight": 1}, "metro_rise": {"weight": 3}}}}

def test_piece_bags_nest_class_then_model_then_preset():
    lib = [_song(i, 150.0) for i in range(600)]
    pieces = [_piece("h_cells_1.mp4", 600.0, "hydra", preset="cells"),
              _piece("h_cells_2.mp4", 600.0, "hydra", preset="cells"),
              _piece("h_fold.mp4", 600.0, "hydra", preset="fold"),
              _piece("fs.mp4", 600.0, "fluid_swirl", preset="aurora"),
              _piece("mr.mp4", 600.0, "metro_rise", preset="century"),
              _piece("pc.mp4", 600.0, "peg_cascade", preset="zen")]    # unlisted -> godot
    srcs = _pieces_in_order(_set(lib, pieces, weights=WEIGHTS))
    share = lambda *names: sum(srcs.count(n) for n in names) / len(srcs)
    assert abs(share("h_cells_1.mp4", "h_cells_2.mp4", "h_fold.mp4") - 1 / 3) < 0.04
    assert abs(share("h_fold.mp4") - 1 / 12) < 0.03            # 1/3 hydra x 1/4 fold
    assert abs(share("mr.mp4") - 2 / 3 * 3 / 5) < 0.04         # 2/3 godot x 3/5 metro_rise
    assert abs(share("pc.mp4") - 2 / 3 * 1 / 5) < 0.04         # default class, weight 1
    assert all(a != b for a, b in zip(srcs, srcs[1:] + srcs[:1]))

def test_zero_weight_keeps_pieces_off_air():
    lib = [_song(i, 150.0) for i in range(60)]
    pieces = [_piece("h.mp4", 600.0, "hydra", preset="cells"),
              _piece("a.mp4", 600.0), _piece("b.mp4", 600.0, "metro_rise")]
    w = {"hydra": {"weight": 0, "models": {"hydra": {}}}, "godot": {"default": True}}
    assert "h.mp4" not in _pieces_in_order(_set(lib, pieces, weights=w))

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


def _hashed(i, dur=200.0):
    return {**_song(i, dur), "sha256": f"sha{i}"}

def _song_render(i, dur, sha=None, **kw):
    return {"src": f"hydra_s{i:03d}.mp4", "dur": dur, "model": "hydra", "title": "Hydra Glass",
            "song": f"s{i:03d}.mp3", "song_sha256": sha or f"sha{i}", **kw}


def test_song_renders_play_over_exactly_their_song():
    # every other song has a render, plus one 700 s song (past MAX_SEG)
    lib = [_hashed(i, 150.0 + 11 * (i % 9)) for i in range(40)] + [_hashed(99, 700.0)]
    renders = [_song_render(i, s["dur"] + 0.02) for i, s in enumerate(lib[:40]) if i % 2 == 0]
    renders.append(_song_render(99, 700.02))
    by_song = {r["song"]: r for r in renders}
    pieces = [_piece(f"p{i}.mp4", 600.0) for i in range(4)]
    for idents in ([IDENTS[0]], [IDENTS[3]]):              # audio pre-roll, silent overlay
        p = plan.build_set({"pieces": pieces, "idents": idents, "songs": renders}, lib,
                           {"seed": 20261113, "swap_at": "x", "fps": 60, "resolution": "1920x1080"})
        openings = []
        for v in p["vods"]:
            t = 0.0
            for seg in v["edl"]:                            # still contiguous
                assert abs(seg["tl_start"] - t) < 0.01
                t = seg["tl_start"] + seg["out"]
            assert abs(t - v["dur"]) < 0.01
            vis = v["edl"][0]["out"]
            for m in v["music"]:
                over = [s for s in v["edl"][1:] if s["tl_start"] < m["end"] - 1e-6
                        and s["tl_start"] + s["out"] > m["start"] + 1e-6]
                r = by_song.get(os.path.basename(m["file"]))
                if r is None:
                    assert not any(s.get("synced") or s["src"].startswith("hydra_") for s in over)
                    continue
                assert [s["src"] for s in over] == [r["src"]] and over[0]["synced"]
                a = max(m["start"], vis)
                assert over[0]["tl_start"] == a and over[0]["in"] == round(a - m["start"], 3)
                assert abs(over[0]["tl_start"] + over[0]["out"] - m["end"]) < 1e-6
                openings.append(over[0]["in"])
                assert "Hydra Glass" in v["description"]
        assert len(openings) == len(renders)                # every render aired
        if idents[0].get("audio"):
            assert set(openings) == {0.0}                   # music starts after the ident
        else:
            assert 12.0 in openings                         # ident covers song 1's opening


def test_only_fresh_complete_song_renders_are_used():
    lib = [_hashed(i, 200.0) for i in range(4)]
    renders = [_song_render(0, 200.02, sha="re-trimmed since"),
               _song_render(1, 199.0),                      # doesn't cover the song
               _song_render(2, 200.02),
               _song_render(3, 200.02, src="old.mp4", rendered_at="2026-11-01T00:00:00Z"),
               _song_render(3, 200.02, src="new.mp4", rendered_at="2026-11-07T00:00:00Z"),
               _song_render(9, 300.0)]                      # song not in the library
    synced = plan.match_song_renders(renders, lib)
    assert {k: r["src"] for k, r in synced.items()} == {"s002.mp3": "hydra_s002.mp4",
                                                        "s003.mp3": "new.mp4"}
