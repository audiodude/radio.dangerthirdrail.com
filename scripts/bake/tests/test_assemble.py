import json, os, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import plan, assemble

def _probe(path, stream, entry):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream,
        "-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()

def test_assemble_produces_synced_show(tmp_path):
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    import glob
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 1, "show_dur_sec": 15.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    plan_path = fx / "plan.json"
    plan_path.write_text(json.dumps(p))
    out = fx / "show.mkv"
    assemble.assemble(str(plan_path), str(out))

    assert out.exists()
    dur = float(_probe(str(out), "v:0", "format=duration"))
    assert abs(dur - p["show_dur_sec"]) < 0.5            # video length == show
    assert _probe(str(out), "v:0", "stream=codec_name") == "h264"
    assert _probe(str(out), "a:0", "stream=codec_name") == "aac"
    assert _probe(str(out), "v:0", "stream=width") == "320"


def test_offspec_audio_inputs_still_land_in_the_show(tmp_path):
    """A 48 kHz track and an AIFF-named-.mp3 must occupy their planned slots.

    Fed straight to the concat demuxer these dropped out entirely — a whole song
    of dead air in the middle of a baked show."""
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    import glob
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 3, "show_dur_sec": 30.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    assert any("song-five" in m["file"] for m in p["music"]), "fixture not in the timeline"
    plan_path = fx / "plan.json"
    plan_path.write_text(json.dumps(p))
    out = fx / "show.mkv"
    assemble.assemble(str(plan_path), str(out))

    # every song slot is filled: total audio matches the plan, nothing dropped
    seg = fx / "seg.wav"
    for name in ("song-five", "song-four"):
        m = next(m for m in p["music"] if name in m["file"])
        mid = (m["start"] + m["end"]) / 2.0
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(mid - 1.0), "-i", str(out),
                        "-t", "2", "-map", "0:a", "-c:a", "pcm_s16le", str(seg)], check=True)
        assert float(_probe(str(seg), "a:0", "format=duration")) > 1.5, f"{name}: no audio in slot"
        vol = subprocess.run(["ffmpeg", "-v", "info", "-i", str(seg), "-af", "volumedetect",
                              "-f", "null", "-"], capture_output=True, text=True).stderr
        mean = next(l.split("mean_volume:")[1] for l in vol.splitlines() if "mean_volume:" in l)
        assert float(mean.replace("dB", "").strip()) > -60.0, f"{name}: slot is silent"


def _mean_db(path, start, dur):
    vol = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-i", str(path), "-t", str(dur),
                          "-map", "0:a", "-af", "volumedetect", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    mean = next(l.split("mean_volume:")[1] for l in vol.splitlines() if "mean_volume:" in l)
    return float(mean.replace("dB", "").strip())


def test_audio_ident_plays_its_own_sound_between_songs(tmp_path):
    """An ident with audio pauses the music: its sound is on air in its slot,
    and the following song starts complete after it."""
    fx = tmp_path / "fx"
    subprocess.run([os.path.join(HERE, "make_fixtures.sh"), str(fx)], check=True)
    ident = fx / "catalog" / "loud_ident.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=yellow:s=320x180:r=60:d=3",
                    "-f", "lavfi", "-i", "sine=f=1500:d=3:sample_rate=48000",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "pcm_s24le",
                    "-shortest", str(ident)], check=True)
    catalog = plan._load_catalog(str(fx / "catalog" / "catalog.json"))
    catalog["idents"] = [{"file": str(ident), "dur": plan.ffprobe_duration(str(ident)),
                          "weight": 1.0, "opening": True, "audio": plan.ffprobe_has_audio(str(ident))}]
    assert catalog["idents"][0]["audio"]
    import glob
    lib = [{"file": p, "title": plan.title_from_path(p), "dur": plan.ffprobe_duration(p)}
           for p in sorted(glob.glob(str(fx / "music" / "*.mp3")))]
    cfg = {"seed": 1, "show_dur_sec": 20.0, "dwell_sec": 5.0,
           "fps": 60, "resolution": "320x180", "straddle": True}
    p = plan.build_plan(catalog, lib, cfg)
    brk = next(s for s in p["edl"] if s.get("audio"))
    after = next(m for m in p["music"] if m["start"] >= brk["tl_start"])
    assert after["start"] == round(brk["tl_start"] + brk["out"], 3)   # song waits for the ident
    plan_path = fx / "plan.json"
    plan_path.write_text(json.dumps(p))
    out = fx / "show.mkv"
    assemble.assemble(str(plan_path), str(out))

    dur = float(_probe(str(out), "v:0", "format=duration"))
    assert abs(dur - p["show_dur_sec"]) < 0.5
    # the 1500 Hz ident tone is what's on air during the break, not a song
    seg = fx / "brk.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(brk["tl_start"] + 1.0), "-i", str(out),
                    "-t", "1", "-map", "0:a", "-af", "bandpass=f=1500:w=100", str(seg)], check=True)
    assert _mean_db(seg, 0, 1) > -45.0
    # and the next song is audible right after
    assert _mean_db(out, after["start"] + 0.5, 1.0) > -60.0
