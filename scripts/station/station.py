#!/usr/bin/env python3
"""YouTube Station job: picks the weekly target, registers baked sets, and (hourly)
uploads them, waits for processing, swaps them into the station playlist, deletes
retired sets, checks health, and alerts. See
docs/superpowers/specs/2026-11-05-youtube-station-design.md.

    station.py target [--now]   -> "SEED SWAP_AT" on stdout, or nothing to skip
    station.py register PLAN    -> add the baked set as pending
    station.py run              -> hourly reconcile (exit 1 on unexpected error)
    station.py alert MESSAGE    -> Telegram message (never fails)
    station.py status           -> human summary of the state

Every `run` step is idempotent and safe to resume after a crash or quota stop.
All times are America/Los_Angeles; state stores ISO strings with offsets."""
import argparse
import contextlib
import datetime as dt
import fcntl
import glob
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import youtube  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
PREFIX = "radio station:"
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
MAX_ATTEMPTS = 3
MIN_FIRST_WEEK_DAYS = 4
NOT_READY_LEAD = dt.timedelta(hours=12)
IMMEDIATE_GRACE = dt.timedelta(hours=3)
UPLOAD_PAUSE = dt.timedelta(hours=24)
HEALTH_SETTLE = dt.timedelta(minutes=30)   # playlistItems.list lags inserts by a minute or more
FRIDAY = 4
SWAP_TIME = dt.time(6, 0)
DEFAULT_TITLE = "Danger Third Rail radio // Shocking Visuals!"


def log(msg):
    print(f"[station] {msg}", file=sys.stderr, flush=True)


def now_pt():
    return dt.datetime.now(PT).replace(microsecond=0)


def iso(t):
    return t.isoformat(timespec="seconds")


def parse(s):
    return dt.datetime.fromisoformat(s).astimezone(PT)


def next_midnight(t):
    return dt.datetime.combine(t.date() + dt.timedelta(days=1), dt.time(0), tzinfo=PT)


def send_telegram(text):
    """True if delivered (or Telegram is unconfigured and it was logged)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log(f"(no telegram) {text}")
        return True
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        urllib.request.urlopen(req, timeout=10).close()
        return True
    except Exception as e:
        log(f"Telegram send failed: {e}")
        return False


def run_cmd(args):
    """(returncode, stdout, stderr); a missing binary or timeout is a failure."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=3600)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


class StationError(Exception):
    pass


@dataclass
class Config:
    station_dir: str
    state_path: str
    blurb_file: str
    playlist_title: str
    s3_uri: str

    @classmethod
    def from_env(cls):
        station_dir = os.environ.get("STATION_DIR") or "/data/station"
        return cls(
            station_dir=station_dir,
            state_path=os.environ.get("STATION_STATE") or os.path.join(station_dir, "state.json"),
            blurb_file=os.environ.get("STATION_BLURB_FILE") or "/etc/radio-blurb.txt",
            playlist_title=os.environ.get("STATION_PLAYLIST_TITLE") or DEFAULT_TITLE,
            s3_uri=(os.environ.get("S3_SHOWS_URI") or "").rstrip("/"),
        )


# --- state file ---

def empty_state():
    return {"playlist_id": None, "sets": {}, "upload_until": None, "quota_until": None,
            "alerts": {}, "last_health": None}


class StateFile:
    """JSON state, written atomically (temp + rename) under an exclusive flock
    on `<state>.lock`, so `register` and `run` never interleave."""

    def __init__(self, path):
        self.path = path

    @contextlib.contextmanager
    def locked(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield self.load()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def load(self):
        try:
            with open(self.path) as f:
                state = json.load(f)
        except FileNotFoundError:
            return empty_state()
        return {**empty_state(), **state}

    def save(self, state):
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise


def find_set(state, status):
    for seed, s in state["sets"].items():
        if s["status"] == status:
            return seed, s
    return None, None


# --- target / register ---

def target(state, now, immediate=False):
    """(seed, swap_at_iso) for the next bake, or (None, reason) to skip."""
    _, pending = find_set(state, "pending")
    if pending:
        return None, "a set is already pending"
    if immediate:
        seed = now.strftime("%Y%m%d")
        if seed in state["sets"]:
            return None, f"set {seed} already exists"
        return seed, iso(now)
    days = (FRIDAY - now.weekday()) % 7 or 7
    friday = now.date() + dt.timedelta(days=days)
    seed = friday.strftime("%Y%m%d")
    if seed in state["sets"]:
        return None, f"set {seed} already exists"
    live_seed, live = find_set(state, "live")
    if live and live.get("swapped_on"):
        gap = (friday - dt.date.fromisoformat(live["swapped_on"])).days
        if gap < MIN_FIRST_WEEK_DAYS:
            return None, (f"live set {live_seed} swapped in {gap} days before {friday}; "
                          f"letting it run a full week")
    return seed, iso(dt.datetime.combine(friday, SWAP_TIME, tzinfo=PT))


def register(state, plan_path, now):
    plan_path = os.path.abspath(plan_path)
    with open(plan_path) as f:
        plan = json.load(f)
    seed = str(plan["seed"])
    existing = state["sets"].get(seed)
    if existing:
        if existing["status"] == "pending" and existing["plan"] == plan_path:
            return seed                                   # already registered
        raise StationError(f"set {seed} already exists ({existing['status']})")
    pending_seed, _ = find_set(state, "pending")
    if pending_seed:
        raise StationError(f"set {pending_seed} is already pending")
    set_dir = os.path.dirname(plan_path)
    vods = sorted(plan["vods"], key=lambda v: v["index"])
    missing = [v["file"] for v in vods if not os.path.isfile(os.path.join(set_dir, v["file"]))]
    if missing:
        raise StationError(f"set {seed}: missing VOD files {missing[:5]}")
    parse(plan["swap_at"])                                # must be a valid ISO time
    state["sets"][seed] = {
        "plan": plan_path, "swap_at": plan["swap_at"], "status": "pending",
        "swapped_on": None, "registered_at": iso(now),
        "vods": [{"file": v["file"], "video_id": None, "upload_url": None,
                  "processed": False, "attempts": 0} for v in vods],
    }
    return seed


# --- the hourly run ---

class QuotaStop(Exception):
    pass


class Station:
    def __init__(self, cfg, state, save, yt, now=now_pt, notify=send_telegram, cmd=run_cmd):
        self.cfg = cfg
        self.state = state
        self._save = save
        self.yt = yt
        self.now = now
        self.notify = notify
        self.cmd = cmd

    def save(self):
        self._save(self.state)

    # alerts are edge-triggered: a key is sent once until something clears it
    def alert_once(self, key, msg):
        alerts = self.state["alerts"]
        if key in alerts:
            return
        log(f"ALERT {key}: {msg}")
        if self.notify(f"{PREFIX} {msg}"):
            alerts[key] = iso(self.now())

    def say(self, msg):
        log(msg)
        return self.notify(f"{PREFIX} {msg}")

    def _forget_set_alerts(self, seed):
        for k in [k for k in self.state["alerts"] if k.endswith(f":{seed}") or f":{seed}:" in k]:
            del self.state["alerts"][k]

    def _api(self, fn, *args, missing_ok=False):
        """Call YouTube; quota errors stop the run, `missing_ok` swallows 404s."""
        try:
            return fn(*args)
        except youtube.YouTubeError as e:
            if e.reason in QUOTA_REASONS:
                raise QuotaStop(str(e)) from e
            if missing_ok and e.status == 404:
                return None
            raise

    def run(self):
        st = self.state
        now = self.now()
        if st.get("quota_until") and parse(st["quota_until"]) > now:
            log(f"quota exhausted until {st['quota_until']}; nothing to do")
            return 0
        st["quota_until"] = None
        try:
            self.ensure_playlist()
            self.check_processing()
            self.swap()
            self.readiness_alerts()
            self.retire_old_sets()
            self.health_check()
            self.upload()
        except QuotaStop as e:
            now = self.now()
            st["quota_until"] = iso(next_midnight(now))
            today = now.date().isoformat()
            for k in [k for k in st["alerts"] if k.startswith("quota:") and k != f"quota:{today}"]:
                del st["alerts"][k]
            self.alert_once(f"quota:{today}",
                            f"YouTube quota exhausted ({e}); pausing until {st['quota_until']}")
            self.save()
            return 0
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:300]
            log(traceback.format_exc())
            self.alert_once(f"error:{msg}", f"run failed: {msg}")
            self.save()
            return 1
        errors = [k for k in st["alerts"] if k.startswith("error:")]
        if errors:
            if self.say("recovered: run completed cleanly"):
                for k in errors:
                    del st["alerts"][k]
        self.save()
        return 0

    # 1. playlist
    def ensure_playlist(self):
        if self.state.get("playlist_id"):
            return
        try:
            with open(self.cfg.blurb_file) as f:
                blurb = f.read().strip()
        except OSError as e:
            log(f"no playlist blurb ({e}); creating with an empty description")
            blurb = ""
        pid = self._api(self.yt.create_playlist, self.cfg.playlist_title, blurb, "public")
        self.state["playlist_id"] = pid
        self.save()
        self.alert_once("playlist-created",
                        f"created the station playlist https://www.youtube.com/playlist?list={pid} "
                        f"— bind the station to it in YouTube Studio (Start Station)")

    def _count_attempt(self, seed, vod, why):
        vod["attempts"] += 1
        log(f"set {seed} {vod['file']}: attempt {vod['attempts']} failed: {why}")
        if vod["attempts"] >= MAX_ATTEMPTS:
            self.alert_once(f"attempts:{seed}:{vod['file']}",
                            f"set {seed} {vod['file']} failed {vod['attempts']} times "
                            f"(latest: {why}); still retrying hourly")

    # 2. processing
    def check_processing(self):
        seed, s = find_set(self.state, "pending")
        if not s:
            return
        todo = [v for v in s["vods"] if v["video_id"] and not v["processed"]]
        if not todo:
            return
        statuses = self._api(self.yt.video_status, [v["video_id"] for v in todo])
        for v in todo:
            info = statuses.get(v["video_id"])
            status = info["uploadStatus"] if info else "deleted"
            if status == "processed":
                v["processed"] = True
            elif status in ("failed", "rejected", "deleted"):
                self._api(self.yt.delete_video, v["video_id"], missing_ok=True)
                v["video_id"] = None
                v["upload_url"] = None
                self._count_attempt(seed, v, f"processing {status}")
            self.save()

    # 3. swap
    def swap(self):
        seed, s = find_set(self.state, "pending")
        if not s or not all(v["processed"] for v in s["vods"]):
            return
        now = self.now()
        if now < parse(s["swap_at"]):
            return
        pid = self.state["playlist_id"]
        new_ids = [v["video_id"] for v in s["vods"]]
        wanted = set(new_ids)
        items = self._api(self.yt.playlist_items, pid)
        present = [vid for _, vid in items if vid in wanted]
        if present != new_ids[:len(present)]:
            self.alert_once(f"swap-order:{seed}",
                            f"playlist order unexpected: set {seed} is partly in the playlist "
                            f"out of order; not swapping, playlist left untouched")
            return
        for vid in new_ids[len(present):]:
            self._api(self.yt.add_playlist_item, pid, vid)
        for item_id, vid in items:
            if vid not in wanted:
                self._api(self.yt.delete_playlist_item, item_id, missing_ok=True)
        today = self.now().date()
        old_seed, old = find_set(self.state, "live")
        if old:
            old["status"] = "old"
            old["retire_on"] = (today + dt.timedelta(days=1)).isoformat()
        s["status"] = "live"
        s["swapped_on"] = today.isoformat()
        s["swapped_at"] = iso(self.now())
        self.state["last_health"] = None                  # check the new set today
        self.save()
        self.say(f"swapped in set {seed} ({len(new_ids)} VODs)"
                 + (f"; set {old_seed} retires {old['retire_on']}" if old else ""))
        self.prune()

    def prune(self):
        """Delete local + S3 copies of every set that isn't live or pending."""
        sets = self.state["sets"]
        keep = {seed for seed, s in sets.items() if s["status"] in ("live", "pending")}
        dirs = {}
        for seed, s in sets.items():
            if seed not in keep:
                dirs[seed] = os.path.dirname(s["plan"])
        for d in glob.glob(os.path.join(self.cfg.station_dir, "set-*")):
            m = re.fullmatch(r"set-(\d+)", os.path.basename(d))
            if m and m.group(1) not in keep:
                dirs.setdefault(m.group(1), d)
        for seed, d in dirs.items():
            if os.path.basename(d) == f"set-{seed}" and os.path.isdir(d):
                log(f"pruning {d}")
                shutil.rmtree(d)
        if not self.cfg.s3_uri:
            return
        seeds = set(dirs)
        rc, out, err = self.cmd(["aws", "s3", "ls", f"{self.cfg.s3_uri}/"])
        if rc != 0:
            self.alert_once("s3-list", f"S3 prune: listing {self.cfg.s3_uri}/ failed: {err.strip()[:200]}")
        else:
            self.state["alerts"].pop("s3-list", None)
            seeds |= set(re.findall(r"PRE set-(\d+)/", out))
        for seed in sorted(seeds - keep):
            uri = f"{self.cfg.s3_uri}/set-{seed}/"
            rc, _, err = self.cmd(["aws", "s3", "rm", "--recursive", uri])
            if rc != 0:
                self.alert_once(f"s3-prune:{seed}", f"S3 prune of {uri} failed: {err.strip()[:200]}")
            else:
                self.state["alerts"].pop(f"s3-prune:{seed}", None)

    # 4. readiness alerts
    def readiness_alerts(self):
        seed, s = find_set(self.state, "pending")
        if not s:
            return
        done = sum(v["processed"] for v in s["vods"])
        if done == len(s["vods"]):
            return
        now = self.now()
        swap_at = parse(s["swap_at"])
        registered = parse(s["registered_at"]) if s.get("registered_at") else None
        if registered and swap_at <= registered and now < registered + IMMEDIATE_GRACE:
            return                                        # --now set, still uploading
        progress = f"{done}/{len(s['vods'])} processed"
        if now >= swap_at:
            self.alert_once(f"overdue:{seed}",
                            f"swap overdue: set {seed} not ready ({progress}); "
                            f"the current set keeps playing")
        elif now >= swap_at - NOT_READY_LEAD:
            self.alert_once(f"not-ready:{seed}", f"set {seed} not ready: {progress}")

    # 5. retire old sets
    def retire_old_sets(self):
        today = self.now().date()
        for seed, s in list(self.state["sets"].items()):
            if s["status"] != "old" or dt.date.fromisoformat(s["retire_on"]) > today:
                continue
            for v in s["vods"]:
                if v["video_id"]:
                    self._api(self.yt.delete_video, v["video_id"], missing_ok=True)
                    v["video_id"] = None
                    self.save()
            del self.state["sets"][seed]
            self._forget_set_alerts(seed)
            self.save()
            log(f"set {seed}: videos deleted, set removed")

    # 6. daily health
    def health_check(self):
        seed, live = find_set(self.state, "live")
        if not live:
            return
        now = self.now()
        today = now.date().isoformat()
        if self.state.get("last_health") == today:
            return
        _, pending = find_set(self.state, "pending")
        if pending and now >= parse(pending["swap_at"]):
            return                                        # swap due; playlist in flux
        if live.get("swapped_at") and now < parse(live["swapped_at"]) + HEALTH_SETTLE:
            return                                        # playlist reads lag fresh edits
        ids = [v["video_id"] for v in live["vods"]]
        problems = []
        got = [vid for _, vid in self._api(self.yt.playlist_items, self.state["playlist_id"])]
        if got != ids:
            missing = [i for i in ids if i not in got]
            extra = [i for i in got if i not in set(ids)]
            parts = [f"playlist has {len(got)} items, set {seed} has {len(ids)}"]
            if missing:
                parts.append(f"{len(missing)} missing")
            if extra:
                parts.append(f"{len(extra)} extra")
            if not missing and not extra:
                parts.append("order differs")
            problems.append(", ".join(parts))
        statuses = self._api(self.yt.video_status, [i for i in ids if i])
        for v in live["vods"]:
            info = statuses.get(v["video_id"]) if v["video_id"] else None
            if not info:
                problems.append(f"{v['file']} video {v['video_id']} is gone")
            elif info["uploadStatus"] != "processed" or info["privacyStatus"] != "unlisted":
                problems.append(f"{v['file']} video {v['video_id']} is "
                                f"{info['uploadStatus']}/{info['privacyStatus']}")
        self.state["last_health"] = today
        if problems:
            shown = "; ".join(problems[:5]) + (f"; +{len(problems) - 5} more" if len(problems) > 5 else "")
            self.alert_once("health", f"health check failed for set {seed}: {shown}")
        elif "health" in self.state["alerts"]:
            if self.say(f"health OK again (set {seed})"):
                del self.state["alerts"]["health"]
        self.save()

    # 7. uploads (last: the slow part, after the time-sensitive steps)
    def upload(self):
        seed, s = find_set(self.state, "pending")
        if not s:
            return
        st = self.state
        if st.get("upload_until"):
            if parse(st["upload_until"]) > self.now():
                log(f"uploads paused until {st['upload_until']}")
                return
            st["upload_until"] = None
            for k in [k for k in st["alerts"] if k.startswith("upload-limit:")]:
                del st["alerts"][k]
            self.save()
        set_dir = os.path.dirname(s["plan"])
        with open(s["plan"]) as f:
            meta = {v["file"]: v for v in json.load(f)["vods"]}
        for v in s["vods"]:
            if v["video_id"]:
                continue
            m = meta[v["file"]]
            try:
                vid = self._upload_one(v, os.path.join(set_dir, v["file"]),
                                       m["title"], m.get("description", ""))
            except youtube.YouTubeError as e:
                if isinstance(e, youtube.AuthError):
                    raise                                 # config problem, not this VOD
                if e.reason in QUOTA_REASONS:
                    raise QuotaStop(str(e)) from e
                if e.reason == "uploadLimitExceeded":
                    until = iso(self.now() + UPLOAD_PAUSE)
                    st["upload_until"] = until
                    uploaded = sum(1 for x in s["vods"] if x["video_id"])
                    self.alert_once(f"upload-limit:{until}",
                                    f"YouTube upload cap hit at {uploaded}/{len(s['vods'])} "
                                    f"of set {seed}; uploads paused until {until}")
                    self.save()
                    return
                if 400 <= e.status < 500:
                    v["upload_url"] = None                # session unusable; start fresh
                self._count_attempt(seed, v, str(e))
                self.save()
                continue
            except (OSError, http.client.HTTPException) as e:
                self._count_attempt(seed, v, f"{type(e).__name__}: {e}")
                self.save()
                continue
            v["video_id"] = vid
            v["upload_url"] = None
            v["processed"] = False
            self.save()
            log(f"set {seed} {v['file']}: uploaded as {vid}")

    def _upload_one(self, vod, path, title, description):
        if vod["upload_url"]:
            try:
                return self.yt.finish_upload(vod["upload_url"], path)["id"]
            except youtube.SessionGone:
                log(f"{vod['file']}: upload session expired; starting over")
                vod["upload_url"] = None
                self.save()
        vod["upload_url"] = self.yt.start_upload(path, title, description)
        self.save()                                       # before any bytes go out
        return self.yt.finish_upload(vod["upload_url"], path)["id"]


# --- commands (the CLI's bodies; tests drive these) ---

def cmd_target(cfg, now, immediate=False):
    store = StateFile(cfg.state_path)
    with store.locked() as state:
        seed, info = target(state, now, immediate)
    if seed is None:
        log(f"skip: {info}")
        return None
    return f"{seed} {info}"


def cmd_register(cfg, plan_path, now):
    store = StateFile(cfg.state_path)
    with store.locked() as state:
        seed = register(state, plan_path, now)
        store.save(state)
    log(f"registered set {seed} (pending)")
    return seed


def cmd_run(cfg, yt, now=now_pt, notify=send_telegram, cmd=run_cmd):
    store = StateFile(cfg.state_path)
    with store.locked() as state:
        return Station(cfg, state, store.save, yt, now, notify, cmd).run()


def status_text(state):
    lines = [f"playlist: {state.get('playlist_id') or '(none yet)'}"]
    for key in ("quota_until", "upload_until", "last_health"):
        if state.get(key):
            lines.append(f"{key}: {state[key]}")
    for seed, s in sorted(state["sets"].items()):
        vods = s["vods"]
        up = sum(1 for v in vods if v["video_id"])
        done = sum(1 for v in vods if v["processed"])
        extra = {"pending": f"swap_at {s['swap_at']}",
                 "live": f"swapped_on {s.get('swapped_on')}",
                 "old": f"retire_on {s.get('retire_on')}"}.get(s["status"], "")
        lines.append(f"set {seed}: {s['status']}, {len(vods)} VODs, {up} uploaded, "
                     f"{done} processed, {extra}")
        failing = [f"{v['file']}x{v['attempts']}" for v in vods if v["attempts"]]
        if failing:
            lines.append(f"  retries: {', '.join(failing)}")
    for k, t in sorted(state["alerts"].items()):
        lines.append(f"alert {k} @ {t}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("target", help="print 'SEED SWAP_AT' for the next bake, or nothing")
    t.add_argument("--now", action="store_true", help="launch: seed today, swap immediately")
    r = sub.add_parser("register", help="register a baked set as pending")
    r.add_argument("plan")
    sub.add_parser("run", help="hourly reconcile")
    a = sub.add_parser("alert", help="send a Telegram message")
    a.add_argument("message")
    sub.add_parser("status", help="summarize the state")
    args = ap.parse_args(argv)

    if args.cmd == "alert":
        send_telegram(f"{PREFIX} {args.message}")
        return 0
    cfg = Config.from_env()
    if args.cmd == "target":
        line = cmd_target(cfg, now_pt(), args.now)
        if line:
            print(line)
        return 0
    if args.cmd == "register":
        try:
            cmd_register(cfg, args.plan, now_pt())
        except (StationError, OSError, KeyError, ValueError) as e:
            log(f"register failed: {e}")
            return 1
        return 0
    if args.cmd == "run":
        return cmd_run(cfg, youtube.YouTube.from_env())
    if args.cmd == "status":
        store = StateFile(cfg.state_path)
        with store.locked() as state:
            print(status_text(state))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
