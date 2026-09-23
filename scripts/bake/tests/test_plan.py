import json
import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plan

def test_title_from_path():
    assert plan.title_from_path("/x/ele-1005.mp3") == "Ele 1005"
    assert plan.title_from_path("never_havin_kids.mp3") == "Never Havin Kids"

def test_shuffle_deterministic_and_total():
    a = plan.shuffle(list(range(20)), seed=7)
    b = plan.shuffle(list(range(20)), seed=7)
    c = plan.shuffle(list(range(20)), seed=8)
    assert a == b and a != c
    assert sorted(a) == list(range(20))  # permutation, nothing lost

def test_shuffle_no_repeat_join():
    # no element equal to its predecessor (best-effort across a single shuffle)
    out = plan.shuffle(["a", "b", "c", "d"], seed=3, no_repeat=True)
    assert all(out[i] != out[i-1] for i in range(1, len(out)))

def test_music_timeline_fills_target():
    ordered = [{"file": f"{i}.mp3", "title": str(i), "dur": 100.0} for i in range(10)]
    tl = plan.build_music_timeline(ordered, target_dur=250.0)
    assert len(tl) == 3                      # 100+100+100 >= 250, stops at 3
    assert tl[0]["start"] == 0.0 and tl[0]["end"] == 100.0
    assert tl[1]["start"] == 100.0 and tl[2]["end"] == 300.0

def test_song_boundaries():
    tl = [{"start": 0.0, "end": 100.0}, {"start": 100.0, "end": 250.0}, {"start": 250.0, "end": 400.0}]
    assert plan.song_boundaries(tl) == [100.0, 250.0]   # boundaries are song STARTS, excluding 0

def test_subtitles_one_per_song():
    tl = [{"title": "A", "start": 0.0, "end": 100.0}, {"title": "B", "start": 100.0, "end": 250.0}]
    subs = plan.build_subtitles(tl)
    assert subs == [{"text": "A", "start": 0.0, "end": 100.0}, {"text": "B", "start": 100.0, "end": 250.0}]


def _piece(src, dur): return {"src": src, "dur": dur}

def test_edl_cuts_at_first_boundary_after_dwell():
    pieces = [_piece("p0.mp4", 1200.0), _piece("p1.mp4", 1200.0)]
    idents = [_piece("id.mp4", 12.0)]
    boundaries = [180.0, 360.0, 540.0, 720.0]   # songs every 180s
    edl = plan.build_edl(pieces, idents, boundaries, show_dur=2000.0, dwell=300.0, straddle=True,
                         rng=random.Random(0))
    # first piece: dwell 300 -> first boundary >=300 is 360; ident straddles 360
    assert edl[0]["kind"] == "piece" and edl[0]["tl_start"] == 0.0
    assert edl[0]["out"] == 354.0                 # 360 - 12/2 - 0
    assert edl[1]["kind"] == "ident" and edl[1]["tl_start"] == 354.0 and edl[1]["out"] == 12.0
    assert edl[2]["kind"] == "piece" and edl[2]["tl_start"] == 366.0   # 360 + 6

def test_edl_final_piece_runs_to_show_end_without_ident():
    pieces = [_piece("p0.mp4", 5000.0)]
    idents = [_piece("id.mp4", 12.0)]
    edl = plan.build_edl(pieces, idents, boundaries=[], show_dur=400.0, dwell=300.0, straddle=True,
                         rng=random.Random(0))
    assert len(edl) == 1 and edl[0]["kind"] == "piece"
    assert edl[0]["tl_start"] == 0.0 and edl[0]["out"] == 400.0

def test_edl_covers_full_show():
    pieces = [_piece(f"p{i}.mp4", 1200.0) for i in range(5)]
    idents = [_piece("id.mp4", 12.0)]
    boundaries = [i * 180.0 for i in range(1, 80)]
    edl = plan.build_edl(pieces, idents, boundaries, show_dur=3600.0, dwell=600.0, straddle=True,
                         rng=random.Random(0))
    last = edl[-1]
    assert abs((last["tl_start"] + last["out"]) - 3600.0) < 0.05    # timeline reaches show end
    # piece segments stay inside their source, and don't all open on frame 0
    for seg in edl:
        if seg["kind"] == "piece":
            assert seg["in"] >= 0.0 and seg["in"] + seg["out"] <= 1200.0 + 1e-6
    assert any(seg["in"] > 0.0 for seg in edl if seg["kind"] == "piece")

def test_build_plan_fills_show_longer_than_library():
    # library is 5 x 200 s = 1000 s; the show must still reach its target by
    # re-shuffling, never playing the same song twice in a row.
    catalog = {"pieces": [{"file": "/c/p0.mp4", "dur": 1200.0}],
               "idents": [{"file": "/c/id.mp4", "dur": 12.0}]}
    library = [{"file": f"/m/s{i}.mp3", "title": f"S{i}", "dur": 200.0} for i in range(5)]
    cfg = {"seed": 7, "show_dur_sec": 3600.0, "dwell_sec": 600.0,
           "fps": 60, "resolution": "1280x720", "straddle": True}
    p = plan.build_plan(catalog, library, cfg)
    assert p["show_dur_sec"] >= 3600.0
    files = [m["file"] for m in p["music"]]
    assert all(a != b for a, b in zip(files, files[1:]))

def test_build_plan_deterministic_and_constraint():
    catalog = {"pieces": [{"file": f"/c/p{i}.mp4", "dur": 1200.0} for i in range(4)],
               "idents": [{"file": "/c/id.mp4", "dur": 12.0}]}
    library = [{"file": f"/m/s{i}.mp3", "title": f"S{i}", "dur": 200.0} for i in range(50)]
    cfg = {"seed": 42, "show_dur_sec": 3600.0, "dwell_sec": 600.0,
           "fps": 60, "resolution": "1280x720", "straddle": True}
    a = plan.build_plan(catalog, library, cfg)
    b = plan.build_plan(catalog, library, cfg)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)   # deterministic
    assert a["resolution"] == "1280x720" and a["fps"] == 60
    assert a["music"] and a["edl"] and a["subtitles"]
    # DWELL constraint asserted: dwell + max_song <= min piece dur
    assert cfg["dwell_sec"] + 200.0 <= 1200.0
    assert a["excluded_songs"] == []


def test_build_plan_excludes_songs_that_bust_the_dwell_budget():
    catalog = {"pieces": [{"file": "/c/p0.mp4", "dur": 300.0}],
               "idents": [{"file": "/c/id.mp4", "dur": 12.0}]}
    library = [{"file": "/m/ok.mp3", "title": "Ok", "dur": 200.0},
               {"file": "/m/epic.mp3", "title": "Epic", "dur": 540.0}]
    cfg = {"seed": 1, "show_dur_sec": 600.0, "dwell_sec": 30.0,
           "fps": 60, "resolution": "1280x720", "straddle": True}
    p = plan.build_plan(catalog, library, cfg)
    assert p["excluded_songs"] == ["/m/epic.mp3"]           # 30 + 540 > 300
    assert {m["file"] for m in p["music"]} == {"/m/ok.mp3"}  # only the fitting song
    for seg in p["edl"]:
        if seg["kind"] == "piece":
            assert seg["out"] <= 300.0 + 1e-6


def test_build_plan_raises_when_no_song_fits():
    catalog = {"pieces": [{"file": "/c/p0.mp4", "dur": 100.0}],
               "idents": [{"file": "/c/id.mp4", "dur": 12.0}]}
    library = [{"file": "/m/long.mp3", "title": "Long", "dur": 400.0}]
    cfg = {"seed": 1, "show_dur_sec": 600.0, "dwell_sec": 30.0,
           "fps": 60, "resolution": "1280x720", "straddle": True}
    try:
        plan.build_plan(catalog, library, cfg)
    except ValueError as e:
        assert "no songs fit" in str(e)
    else:
        assert False, "expected ValueError when every song busts the budget"


def test_resolve_media_prefers_sibling_of_catalog(tmp_path):
    # catalog.json records the render host's path; the bake machine has the mp4
    # next to catalog.json instead.
    local = tmp_path / "piece.mp4"
    local.write_bytes(b"x")
    assert plan.resolve_media("/home/renderbox/out/piece.mp4", str(tmp_path)) == str(local)
    # nothing local -> keep the recorded path (single-machine case)
    assert plan.resolve_media("/elsewhere/gone.mp4", str(tmp_path)) == "/elsewhere/gone.mp4"
