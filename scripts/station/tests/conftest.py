import datetime as dt
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import station  # noqa: E402
import youtube  # noqa: E402
from fake_youtube import FakeYouTube  # noqa: E402

CHUNK = 256 * 1024


class Clock:
    def __init__(self, when):
        self.set(when)

    def __call__(self):
        return self.t

    def set(self, when):
        self.t = station.parse(when)

    def advance(self, **kw):
        self.t = self.t + dt.timedelta(**kw)


class World:
    """A temp station dir, the fake YouTube, a frozen Pacific clock, captured
    Telegram messages, and a fake `aws` CLI."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "station"
        self.dir.mkdir()
        blurb = tmp_path / "blurb.txt"
        blurb.write_text("Shocking visuals, all week.\n")
        self.thumb = tmp_path / "thumb.jpg"
        self.thumb.write_bytes(b"\xff\xd8 jpeg bytes")
        self.cfg = station.Config(
            station_dir=str(self.dir), state_path=str(self.dir / "state.json"),
            blurb_file=str(blurb), playlist_title="Test Station", s3_uri="s3://bucket/shows",
            thumbnail=str(self.thumb))
        self.fake = FakeYouTube()
        self.yt = youtube.YouTube("cid", "secret", "refresh", transport=self.fake,
                                  chunk_size=CHUNK, clock=lambda: 0.0)
        self.clock = Clock("2026-11-11T03:00:00-08:00")
        self.sent = []
        self.cmds = []
        self.s3 = set()               # prefixes "present" in the fake bucket
        self.s3_fail = False

    # --- collaborators ---
    def notify(self, text):
        self.sent.append(text)
        return True

    def aws(self, args):
        self.cmds.append(args)
        if args[:3] == ["aws", "s3", "ls"]:
            listing = "".join(f"                           PRE {p}/\n" for p in sorted(self.s3))
            return 0, listing, ""
        if args[:4] == ["aws", "s3", "rm", "--recursive"]:
            if self.s3_fail:
                return 1, "", "AccessDenied"
            m = re.fullmatch(r"s3://bucket/shows/(.+)/", args[4])
            self.s3.discard(m.group(1))
            return 0, "", ""
        raise AssertionError(f"unexpected command {args}")

    # --- driving the station ---
    def run(self):
        return station.cmd_run(self.cfg, self.yt, now=self.clock, notify=self.notify, cmd=self.aws)

    def target(self, now=False):
        return station.cmd_target(self.cfg, self.clock(), now)

    def make_set(self, seed, n=3, swap_at=None, size=1000):
        d = self.dir / f"set-{seed}"
        d.mkdir()
        vods = []
        for i in range(n):
            name = f"vod-{i:03d}.mp4"
            (d / name).write_bytes(bytes((i + j) % 251 for j in range(size)))
            vods.append({"index": i, "file": name, "title": f"Song {seed}-{i}",
                         "description": f"desc {i}", "dur": 300.0})
        plan = {"seed": int(seed), "swap_at": swap_at or station.iso(self.clock()),
                "fps": 60, "resolution": "1920x1080", "vods": vods}
        (d / "plan.json").write_text(json.dumps(plan))
        self.s3.add(f"set-{seed}")
        return str(d / "plan.json")

    def register(self, seed, n=3, swap_at=None, size=1000):
        plan = self.make_set(seed, n, swap_at, size)
        return station.cmd_register(self.cfg, plan, self.clock())

    def bring_live(self, seed, n=2):
        """Register a set swapping now, upload it, then process + swap it in."""
        self.register(seed, n)
        assert self.run() == 0
        assert self.run() == 0
        s = self.state()["sets"][seed]
        assert s["status"] == "live"
        return [v["video_id"] for v in s["vods"]]

    def state(self):
        with open(self.cfg.state_path) as f:
            return json.load(f)

    def write_state(self, state):
        station.StateFile(self.cfg.state_path).save({**station.empty_state(), **state})

    def playlist(self):
        return self.fake.playlist_video_ids(self.state()["playlist_id"])

    def pop_sent(self):
        out, self.sent = self.sent, []
        return out


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)
