#!/usr/bin/env python3
"""Stream countdown -> live-gated ident -> complete show over one RTMP connection."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
import uuid


class Restart(Exception):
    pass


class Stop(Exception):
    pass


def read_json(path):
    try:
        with open(path) as source:
            return json.load(source)
    except (OSError, ValueError):
        return None


def write_json(path, value):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        temporary = out.name
        json.dump(value, out)
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve_show(directory):
    latest = directory / "LATEST"
    if latest.is_file():
        show = directory / latest.read_text().strip()
        if show.is_file():
            return show
    manifests = sorted(directory.glob("show-*.opening.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    return next((p.with_name(p.name.removesuffix(".opening.json") + ".mkv")
                 for p in manifests
                 if p.with_name(p.name.removesuffix(".opening.json") + ".mkv").is_file()), None)


def opening_parts(show):
    manifest = read_json(show.with_suffix(".opening.json"))
    if not isinstance(manifest, dict):
        raise ValueError(f"missing opening manifest for {show.name}; bake before playout")
    parts = {}
    for name in ("countdown", "hold", "ident"):
        part = manifest[name]
        path = show.parent / part["file"]
        duration = float(part["duration_sec"])
        if not path.is_file() or duration <= 0:
            raise ValueError(f"invalid opening segment: {name}")
        parts[name] = (path, duration)
    return parts


def stop_process(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait()


class Playout:
    def __init__(self):
        self.shows = Path(os.environ.get("SHOWS_DIR", "/data/shows"))
        self.state = Path(os.environ.get("PLAYOUT_STATE", "/tmp/playout_state.json"))
        self.release = Path(os.environ.get("PLAYOUT_RELEASE", "/tmp/playout_release.json"))
        self.restart = Path(os.environ.get("PLAYOUT_RESTART", "/tmp/playout_restart.json"))
        self.heartbeat = Path(os.environ.get("PLAYOUT_HEARTBEAT", "/tmp/playout_heartbeat"))
        self.ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
        self.window = shlex.split(os.environ.get(
            "WINDOW_CMD", f"python3 {Path(__file__).with_name('window.py')}"))
        self.interval = float(os.environ.get("SUPERVISE_INTERVAL", "15"))
        self.output = os.environ.get("OUTPUT") or (
            os.environ.get("YOUTUBE_URL", "rtmp://a.rtmp.youtube.com/live2").rstrip("/")
            + "/" + os.environ["YOUTUBE_STREAM_KEY"])
        self.master = self.producer = None
        self.run_id = None
        self.next_check = 0.0
        self.next_window = 0.0
        self.next_heartbeat = 0.0

    def in_window(self):
        return subprocess.run(self.window, stdin=subprocess.DEVNULL).returncode == 0

    def phase(self, phase):
        write_json(self.state, {"run_id": self.run_id, "phase": phase})
        print(f"[playout] {phase}", flush=True)

    def matches(self, path):
        command = read_json(path)
        return isinstance(command, dict) and command.get("run_id") == self.run_id

    def check(self):
        if self.master.poll() is not None:
            raise Restart("output ffmpeg exited")
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + 0.5
        if self.matches(self.restart):
            raise Restart("monitor requested a fresh opening")
        if now >= self.next_window:
            self.next_window = now + self.interval
            if not self.in_window():
                raise Restart("window closed")
        if now >= self.next_heartbeat:
            self.next_heartbeat = now + 5
            self.heartbeat.touch()

    def segment(self, path, offset):
        # Each source gets a continuous timestamp range. MPEG-TS carries in-band
        # codec headers across the joins; the master owns pacing and the RTMP
        # connection, so switching to the ident/show never reconnects YouTube.
        # Only the first source has negative decoder timestamps. Shifting that
        # source independently would overlap it with the next timestamp range.
        self.producer = subprocess.Popen([
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
            "-bsf:v", "h264_mp4toannexb", "-output_ts_offset", f"{offset:.6f}",
            "-avoid_negative_ts", "disabled", "-mpegts_copyts", "1",
            "-mpegts_flags", "+initial_discontinuity",
            "-muxdelay", "0", "-f", "mpegts", "pipe:1",
        ], stdin=subprocess.DEVNULL, stdout=self.master.stdin)
        while self.producer.poll() is None:
            self.check()
            time.sleep(0.1)
        self.check()
        if self.producer.returncode != 0:
            raise Restart(f"segment ffmpeg exited: {path.name}")
        self.producer = None

    def play(self, show, parts):
        self.run_id = str(uuid.uuid4())
        self.next_check = self.next_window = self.next_heartbeat = 0.0
        # Publish the new generation before bringing up transport. Commands from
        # an earlier connection cannot release this opening.
        self.phase("countdown")
        self.master = subprocess.Popen([
            self.ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "warning",
            "-re", "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "flv", self.output,
        ], stdin=subprocess.PIPE)
        offset = 0.0
        try:
            path, duration = parts["countdown"]
            self.segment(path, offset)
            offset += duration
            self.phase("waiting")
            # Always show zero once. If YouTube is late, continue supplying
            # silent video instead of consuming any of the ident or first song.
            while True:
                path, duration = parts["hold"]
                self.segment(path, offset)
                offset += duration
                if self.matches(self.release):
                    break
            self.phase("ident")
            path, duration = parts["ident"]
            self.segment(path, offset)
            offset += duration
            self.phase("show")
            self.segment(show, offset)
            self.master.stdin.close()
            while self.master.poll() is None:
                # EOF needs to drain the muxer's final packets before replay.
                if not self.in_window():
                    break
                self.heartbeat.touch()
                time.sleep(0.5)
        finally:
            stop_process(self.producer)
            self.producer = None
            if self.master.stdin and not self.master.stdin.closed:
                self.master.stdin.close()
            stop_process(self.master)
            self.master = None
            self.phase("idle")

    def run(self):
        while True:
            self.phase("idle")
            if not self.in_window():
                time.sleep(self.interval)
                continue
            show = resolve_show(self.shows)
            try:
                if show is None:
                    raise ValueError("no show available")
                parts = opening_parts(show)
                print(f"[playout] opening for {show.name}", flush=True)
                self.play(show, parts)
            except (KeyError, TypeError, ValueError, OSError, Restart) as exc:
                print(f"[playout] {exc}; retrying", flush=True)
            time.sleep(3)


def main():
    def stop(signum, frame):
        raise Stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    playout = Playout()
    try:
        playout.run()
    except Stop:
        pass
    finally:
        playout.phase("idle")


if __name__ == "__main__":
    main()
