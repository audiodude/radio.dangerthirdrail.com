import json, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import plan, assemble, validate

assemble.PRESET = "ultrafast"          # fixtures are tiny; the real bake uses slow


def _probe(path, stream, entry):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _mean_db(path, start, dur, af="volumedetect"):
    vol = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-i", str(path), "-t", str(dur),
                          "-map", "0:a", "-af", af + (",volumedetect" if af != "volumedetect" else ""),
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    mean = next(l.split("mean_volume:")[1] for l in vol.splitlines() if "mean_volume:" in l)
    return float(mean.replace("dB", "").strip())


def _bake(tmp_path, idents=None, seed=1):
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    if idents is not None:
        catalog["idents"] = idents(fx)
    lib = plan.load_library(str(fx / "music"), set())
    p = plan.build_set(catalog, lib, {"seed": seed, "swap_at": "2026-11-13T06:00:00-08:00",
                                      "fps": 60, "resolution": "320x180"})
    set_dir = fx / "set"
    set_dir.mkdir()
    (set_dir / "plan.json").write_text(json.dumps(p))
    assemble.assemble(str(set_dir / "plan.json"), str(set_dir))
    return p, set_dir


def _loud_ident(fx):
    """An ident with its own 1500 Hz sound, at another size and frame rate (like
    the hand-made 720p idents inside a 1080p set)."""
    ident = fx / "catalog" / "loud_ident.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=yellow:s=640x360:r=30:d=3",
                    "-f", "lavfi", "-i", "sine=f=1500:d=3:sample_rate=48000",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s24le",
                    "-shortest", str(ident)], check=True)
    return [{"src": str(ident), "dur": plan.ffprobe_duration(str(ident)), "weight": 1.0,
             "audio": plan.ffprobe_has_audio(str(ident))}]


def test_every_vod_is_an_upload_ready_mp4_matching_its_plan(tmp_path):
    p, set_dir = _bake(tmp_path)
    assert len(p["vods"]) >= 2
    for v in p["vods"]:
        out = set_dir / v["file"]
        assert abs(float(_probe(str(out), "v:0", "format=duration")) - v["dur"]) < 0.5
        assert _probe(str(out), "a:0", "stream=sample_rate") == "48000"
    assert validate.validate(str(set_dir / "plan.json"), str(set_dir)) == []
    assert not [f for f in os.listdir(set_dir) if f.startswith(".work-")]   # no leftovers


def test_offspec_audio_inputs_still_land_in_their_slots(tmp_path):
    """A 48 kHz track and an AIFF-named-.mp3 must be audible in their slots;
    fed straight to the concat demuxer these once dropped out entirely."""
    p, set_dir = _bake(tmp_path, seed=3)
    for name in ("song-five", "song-four"):
        v = next(v for v in p["vods"] if any(name in m["file"] for m in v["music"]))
        m = next(m for m in v["music"] if name in m["file"])
        mid = (m["start"] + m["end"]) / 2.0
        assert _mean_db(set_dir / v["file"], mid - 1.0, 2.0) > -60.0, f"{name}: slot is silent"


def test_audio_ident_is_preroll_with_its_own_sound_even_at_another_size(tmp_path):
    p, set_dir = _bake(tmp_path, idents=_loud_ident)
    v = p["vods"][0]
    out = set_dir / v["file"]
    assert _probe(str(out), "v:0", "stream=width") == "320"
    # the ident's 1500 Hz tone is on air before the music, then song 1 plays
    assert _mean_db(out, 1.0, 1.0, af="bandpass=f=1500:w=100") > -45.0
    assert _mean_db(out, v["music"][0]["start"] + 0.5, 1.0) > -60.0


def test_a_rerun_skips_finished_vods(tmp_path):
    p, set_dir = _bake(tmp_path)
    first = set_dir / p["vods"][0]["file"]
    before = first.stat().st_mtime_ns
    (set_dir / p["vods"][1]["file"]).unlink()
    assemble.assemble(str(set_dir / "plan.json"), str(set_dir))
    assert first.stat().st_mtime_ns == before
    assert validate.validate(str(set_dir / "plan.json"), str(set_dir)) == []
