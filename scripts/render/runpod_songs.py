#!/usr/bin/env python3
"""runpod_songs.py --music-dir DIR --out-dir DIR [--pods N] [--jobs K] ...

Renders the library's missing or stale song renders on rented RunPod GPUs.

The pending songs are `render_songs.sh --dry-run` run locally; with none, no pod
is created. They are split by duration over min(N, pending) community pods
(`radio-songs-<n>`), one thread per pod. Each pod gets the LOCAL vxstory
`hydra/` (so it renders exactly the local sketches), this repo's
`scripts/render/` and the bake's denylist, and only its own mp3s; it runs
`render_songs.sh --jobs K --only <its songs>`, and the finished mp4s + sidecars
are pulled back into --out-dir. make_manifest.py then rebuilds catalog.json
locally. Nothing is uploaded and no AWS credentials go to the pods: community
hosts are third-party machines. Upload with upload_catalog.sh afterwards.

Every pod this run creates is deleted by id on success, error, Ctrl-C and
SIGTERM/SIGHUP. --keep-on-failure keeps the pods whose shard failed (ids
printed) so they can be inspected; delete them by hand afterwards.

Env: RUNPOD_API_KEY (required), VXSTORY_DIR (default for --vxstory).
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.runpod.io/v2"  # v1 (rest.runpod.io) is gone: NXDOMAIN since Nov 2026
USER_AGENT = "radio-songs/1"  # urllib's default UA gets a Cloudflare 403 (1010)
IMAGE = "runpod/base:2.0.0-ubuntu2404"  # has ffmpeg + git
GPUS = ["NVIDIA GeForce RTX 3090", "NVIDIA RTX A4000", "NVIDIA RTX A5000",
        "NVIDIA GeForce RTX 4090"]
REPO = Path(__file__).resolve().parents[2]
RENDER_DIR = REPO / "scripts" / "render"
DENYLIST = REPO / "scripts" / "bake" / "song_denylist.txt"

# Remote layout
R_VXSTORY, R_REPO, R_MUSIC, R_OUT = "/root/vxstory", "/root/radio", "/root/music", "/root/out"
R_ONLY, R_LOG, R_RC, R_PID = "/root/songs.txt", "/root/render.log", "/root/render.rc", "/root/render.pid"

SETUP_BASE = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq rsync jq ca-certificates curl >/dev/null
curl -fsSL https://deb.nodesource.com/setup_24.x | bash - >/dev/null
apt-get install -y -qq nodejs >/dev/null
# Vulkan ICD fix: the host-mounted GLX ICD fails headless (render.mjs would fall
# back to SwiftShader and refuse); load the same driver through EGL instead.
api="$(jq -r .ICD.api_version /etc/vulkan/icd.d/nvidia_icd.json)"
mkdir -p /usr/share/vulkan/icd.d
printf '{"file_format_version":"1.0.1","ICD":{"library_path":"libEGL_nvidia.so.0","api_version":"%s"}}\n' \
    "$api" > /usr/share/vulkan/icd.d/nvidia_egl_icd.json
mkdir -p /root/vxstory/hydra /root/radio/scripts/bake /root/music /root/out
echo "node $(node --version), vulkan api $api, $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader)"
"""
SETUP_HYDRA = f"""
set -euo pipefail
cd {R_VXSTORY}/hydra
npm ci --no-audit --no-fund
npx playwright install --with-deps chromium-headless-shell >/dev/null
"""

STOP = threading.Event()
PROCS = set()
LOCK = threading.Lock()     # log lines and PROCS
REWRITE = threading.Lock()  # sidecar path rewrites in --out-dir


def log(msg, tag="runpod_songs"):
    with LOCK:
        print(f"[{tag}] {msg}", flush=True)


class Aborted(Exception):
    pass


def api(method, path, body=None, timeout=60):
    req = urllib.request.Request(
        API + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["RUNPOD_API_KEY"],
                 "User-Agent": USER_AGENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return json.loads(data) if data else None


def run(cmd, tag, check=True, prefix_output=True, **kw):
    """Run cmd, echoing its output prefixed by tag; killable via STOP."""
    if STOP.is_set():
        raise Aborted()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", start_new_session=True, **kw)
    with LOCK:
        PROCS.add(p)
    out = []
    try:
        for line in p.stdout:
            out.append(line)
            if prefix_output:
                log(line.rstrip("\n"), tag)
        rc = p.wait()
    finally:
        with LOCK:
            PROCS.discard(p)
    if STOP.is_set():
        raise Aborted()
    if check and rc != 0:
        raise RuntimeError(f"exit {rc}: {shlex.join(cmd)[:200]}")
    return rc, "".join(out)


def kill_procs():
    with LOCK:
        procs = list(PROCS)
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class Pod:
    def __init__(self, n, songs, args):
        self.n, self.songs, self.args = n, songs, args
        self.name = f"radio-songs-{n}"
        self.tag = self.name
        self.id = None
        self.cost_per_hr = 0.0
        self.gpu = "?"
        self.created = self.deleted = None
        self.host = self.port = None
        self.error = None
        self.rendered = 0
        self.pulled = False

    # --- lifecycle -----------------------------------------------------------
    def create(self, disk_gb, pubkey):
        # v2 takes one GPU type per create: walk the preference list until one has capacity.
        base = {
            "name": self.name, "image": IMAGE, "cloud": "COMMUNITY", "disk": disk_gb,
            "ports": ["22/tcp"],
            "env": {"NVIDIA_DRIVER_CAPABILITIES": "all", "PUBLIC_KEY": pubkey},
        }
        # Block SIGINT/SIGTERM/SIGHUP across the POST so the id is always recorded
        # before an interrupt can unwind (it is delivered right after).
        sigs = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        errors = []
        # Community capacity flaps second to second ("no longer any instances"), so
        # walk the list a few times before giving up.
        for gpu in [g for _ in range(4) for g in self.args.gpu]:
            if errors and gpu == self.args.gpu[0]:
                time.sleep(15)
            signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
            try:
                try:
                    rec = api("POST", "/pods", {**base, "gpu": {"id": gpu, "count": 1}},
                              timeout=120)
                except urllib.error.HTTPError as e:
                    errors.append(f"{gpu}: HTTP {e.code}: {e.read().decode()[:200]}")
                    continue
                except OSError as e:
                    raise RuntimeError(f"create {self.name}: {e}; the request may still have "
                                       f"made a pod: check the RunPod console for a stray "
                                       f"{self.name}")
                self.id = rec["id"]
                self.created = time.time()
                self.cost_per_hr = float(rec.get("cost") or 0)
                self.gpu = (rec.get("gpu") or {}).get("id") or gpu
                break
            finally:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, sigs)
        if self.id is None:
            raise RuntimeError(f"create {self.name}: no GPU type had capacity in 4 tries; last: "
                               + "; ".join(errors[-len(self.args.gpu):]))
        log(f"created {self.name}: id {self.id}, {self.gpu}, ${self.cost_per_hr:.2f}/h, "
            f"{len(self.songs)} songs", self.tag)

    def delete(self):
        """DELETE by id until the API says it's gone; True on success."""
        for attempt in range(5):
            try:
                api("DELETE", f"/pods/{self.id}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    log(f"delete {self.id}: HTTP {e.code} (attempt {attempt + 1})", self.tag)
                    time.sleep(3)
                    continue
            except OSError as e:
                log(f"delete {self.id}: {e} (attempt {attempt + 1})", self.tag)
                time.sleep(3)
                continue
            try:
                api("GET", f"/pods/{self.id}")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    self.deleted = time.time()
                    log(f"deleted {self.name} ({self.id}): GET now 404", self.tag)
                    return True
            except OSError:
                pass
            time.sleep(3)
        log(f"COULD NOT DELETE {self.name} ({self.id}): delete it in the RunPod console NOW",
            self.tag)
        return False

    # --- work ----------------------------------------------------------------
    def ssh_cmd(self):
        return ["ssh", "-i", self.args.ssh_key, "-p", str(self.port),
                "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=6", f"root@{self.host}"]

    def ssh(self, script, check=True, prefix_output=True):
        # Plain `bash -c`: a login shell's banners would skew render()'s line count
        return run(self.ssh_cmd() + ["bash", "-c", shlex.quote(script)], self.tag,
                   check=check, prefix_output=prefix_output)

    def rsync(self, *args):
        e = shlex.join(self.ssh_cmd()[:-1])
        return run(["rsync", "-a", "--partial", "-e", e, *args], self.tag)

    def remote(self, path):
        return f"root@{self.host}:{path}"

    def wait_ssh(self):
        deadline = time.time() + 20 * 60
        while True:
            if STOP.is_set():
                raise Aborted()
            if time.time() > deadline:
                raise RuntimeError("no public IP / SSH port after 20 min")
            try:
                rec = api("GET", f"/pods/{self.id}")
            except (OSError, ValueError) as e:
                log(f"poll: {e}", self.tag)
                rec = {}
            if rec.get("cost"):
                self.cost_per_hr = float(rec["cost"])
            gpu = (rec.get("gpu") or {}).get("id")
            if gpu:
                self.gpu = gpu
            direct = (rec.get("ssh") or {}).get("direct") or {}
            port22 = next((p for p in (rec.get("runtime") or {}).get("ports") or []
                           if p.get("private") == 22 and p.get("public") and p.get("ip")), None)
            if rec.get("status") == "RUNNING" and (direct.get("host") or port22):
                self.host = direct.get("host") or port22["ip"]
                self.port = int(direct.get("port") or port22["public"])
                rc, _ = self.ssh("true", check=False, prefix_output=False)
                if rc == 0:
                    log(f"ssh up: root@{self.host} -p {self.port} ({self.gpu})", self.tag)
                    return
            time.sleep(10)

    def work(self):
        a = self.args
        self.wait_ssh()
        log("setup: apt, node 24, Vulkan ICD fix", self.tag)
        self.ssh(SETUP_BASE)
        log("sync: hydra, render scripts, denylist, mp3s", self.tag)
        self.rsync("--exclude", "node_modules/", f"{a.vxstory}/hydra/",
                   self.remote(f"{R_VXSTORY}/hydra/"))
        self.rsync(f"{RENDER_DIR}/", self.remote(f"{R_REPO}/scripts/render/"))
        self.rsync(str(DENYLIST), self.remote(f"{R_REPO}/scripts/bake/song_denylist.txt"))
        self.push_music()
        log("setup: npm ci, playwright chromium", self.tag)
        self.ssh(SETUP_HYDRA)
        rc = self.render()
        log("pull: mp4s + sidecars", self.tag)
        self.pull()
        if rc != 0:
            raise RuntimeError(f"render_songs.sh exited {rc} (failed songs are listed above)")

    def push_music(self):
        lst = Path(self.args.tmp) / f"{self.name}.songs.txt"
        lst.write_text("\n".join(self.songs) + "\n")
        self.rsync(str(lst), self.remote(R_ONLY))
        self.rsync(f"--files-from={lst}", f"{self.args.music_dir}/", self.remote(f"{R_MUSIC}/"))

    def render(self):
        """Start render_songs.sh detached (an SSH drop can't kill it), follow its
        log, and return its exit code."""
        job = (f"echo $$ > {R_PID}; cd {R_REPO} && VXSTORY_DIR={R_VXSTORY} "
               f"scripts/render/render_songs.sh {R_MUSIC} {R_OUT} --jobs {self.args.jobs} "
               f"--only {R_ONLY} > {R_LOG} 2>&1; echo $? > {R_RC}")
        self.ssh(f"rm -f {R_RC} {R_PID}; touch {R_LOG}; "
                 f"nohup setsid bash -c {shlex.quote(job)} >/dev/null 2>&1 < /dev/null & "
                 f"for i in $(seq 100); do [ -s {R_PID} ] && exit 0; sleep 0.1; done; exit 1")
        log(f"rendering {len(self.songs)} songs, --jobs {self.args.jobs}", self.tag)
        seen = 0
        while True:
            # tail exits once the render's shell does; resume where it left off
            # after an SSH drop
            _, out = self.ssh(f"tail -n +{seen + 1} -F --pid=$(cat {R_PID}) {R_LOG} 2>/dev/null",
                              check=False)
            seen += out.count("\n")
            rc, out = self.ssh(f"cat {R_RC}", check=False, prefix_output=False)
            if rc == 0 and out.strip():
                # Lines written between tail's last read and its exit
                self.ssh(f"tail -n +{seen + 1} {R_LOG}", check=False)
                try:
                    return int(out.strip())
                except ValueError:
                    return 1
            log("lost the log stream; reconnecting", self.tag)
            time.sleep(10)

    def pull(self):
        out = self.args.out_dir
        # First match wins: catalog.json and half-written *.mp4.tmp stay behind
        self.rsync("--exclude", "catalog.json", "--include", "*.mp4", "--include", "*.json",
                   "--exclude", "*", self.remote(f"{R_OUT}/"), f"{out}/")
        self.pulled = True
        # Sidecars name the mp4 by its pod path; point them at the local copy
        with REWRITE:
            for f in Path(out).glob("*.json"):
                if f.name == "catalog.json":
                    continue
                try:
                    d = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                if isinstance(d, dict) and str(d.get("file", "")).startswith(R_OUT + "/"):
                    d["file"] = str(Path(out) / Path(d["file"]).name)
                    f.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")

    def thread(self):
        try:
            self.work()
        except Aborted:
            self.error = "aborted"
        except Exception as e:  # noqa: BLE001 -- a shard's failure is reported, not raised
            self.error = str(e)
            if not STOP.is_set():
                log(f"SHARD FAILED: {e}", self.tag)
                # Still pull whatever finished before the failure
                if self.host and not self.pulled:
                    try:
                        self.pull()
                    except Exception as e2:  # noqa: BLE001
                        log(f"pull after failure: {e2}", self.tag)
        else:
            log("shard done", self.tag)


def pending_songs(music_dir, out_dir):
    r = subprocess.run([str(RENDER_DIR / "render_songs.sh"), music_dir, out_dir, "--dry-run"],
                       stdout=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.exit(f"render_songs.sh --dry-run failed (exit {r.returncode})")
    return [s for s in r.stdout.splitlines() if s]


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], stdout=subprocess.PIPE, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        sys.exit(f"ffprobe failed on {path}")


def split(songs, durs, n):
    """Longest first onto the least-loaded pod: shards of about equal length."""
    shards = [[] for _ in range(n)]
    load = [0.0] * n
    for s in sorted(songs, key=lambda s: -durs[s]):
        i = load.index(min(load))
        shards[i].append(s)
        load[i] += durs[s]
    return shards, load


def default_key():
    for pub in sorted(Path.home().glob(".ssh/*.pub")):
        if pub.with_suffix("").is_file():
            return str(pub.with_suffix(""))
    return None


def raise_interrupt(signum, _frame):
    raise KeyboardInterrupt(signal.Signals(signum).name)


def main():
    ap = argparse.ArgumentParser(
        description="Render missing/stale song renders on RunPod GPUs (never uploads).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--music-dir", required=True, help="the aired mp3s (mirror of S3_MUSIC_BUCKET)")
    ap.add_argument("--out-dir", required=True, help="catalog dir; renders land here")
    ap.add_argument("--pods", type=int, default=1, help="pods to rent (default 1)")
    ap.add_argument("--jobs", type=int, default=4, help="renders at once per pod (default 4)")
    ap.add_argument("--gpu", nargs="+", default=GPUS, metavar="ID",
                    help="RunPod GPU type ids in preference order (default: %(default)s)")
    ap.add_argument("--vxstory", default=os.environ.get("VXSTORY_DIR", str(REPO.parent / "vxstory")),
                    help="local vxstory checkout whose hydra/ is rendered (default: $VXSTORY_DIR "
                         "or ../vxstory)")
    ap.add_argument("--disk-gb", type=int, help="container disk per pod (default: sized to "
                    "--jobs concurrent AVIs of the longest song plus the shard's mp4s)")
    ap.add_argument("--ssh-key", default=default_key(),
                    help="private key; its .pub goes to the pods (default: first ~/.ssh/*.pub pair)")
    ap.add_argument("--keep-on-failure", action="store_true",
                    help="keep pods whose shard failed (ids printed); delete them yourself")
    a = ap.parse_args()
    if a.pods < 1 or a.jobs < 1:
        ap.error("--pods and --jobs must be >= 1")
    a.music_dir = str(Path(a.music_dir).resolve())
    a.out_dir = str(Path(a.out_dir).resolve())
    a.vxstory = str(Path(a.vxstory).resolve())
    if not (Path(a.vxstory) / "hydra" / "render.mjs").is_file():
        ap.error(f"no hydra/render.mjs under --vxstory {a.vxstory}")
    if not a.ssh_key or not Path(a.ssh_key + ".pub").is_file():
        ap.error("--ssh-key: need a private key with a .pub next to it")
    if not os.environ.get("RUNPOD_API_KEY"):
        ap.error("RUNPOD_API_KEY is not set (. ~/.secrets)")

    pending = pending_songs(a.music_dir, a.out_dir)
    if not pending:
        log("nothing to render: every song render is up to date")
        return 0
    durs = {s: duration(os.path.join(a.music_dir, s)) for s in pending}
    shards, load = split(pending, durs, min(a.pods, len(pending)))
    if a.disk_gb is None:
        # AVI ~1 GB per minute of 1080p60; mp4s budgeted at 0.25 GB/min; 20 GB system
        longest = max(durs.values()) / 60
        a.disk_gb = int(20 + a.jobs * longest * 1.1 + max(load) / 60 * 0.25) + 1
    log(f"{len(pending)} songs pending ({sum(durs.values()) / 3600:.2f} h of audio) over "
        f"{len(shards)} pod(s), --jobs {a.jobs}, {a.disk_gb} GB disk each")
    os.makedirs(a.out_dir, exist_ok=True)
    a.tmp = tempfile.mkdtemp(prefix="runpod_songs.")  # per-pod --only lists
    pubkey = Path(a.ssh_key + ".pub").read_text().strip()

    pods = [Pod(i + 1, shard, a) for i, shard in enumerate(shards)]
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, raise_interrupt)
    interrupted = False
    try:
        for p in pods:
            p.create(a.disk_gb, pubkey)
        threads = [threading.Thread(target=p.thread, daemon=True) for p in pods]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(1)
    except KeyboardInterrupt as e:
        interrupted = True
        log(f"interrupted ({str(e) or 'SIGINT'}): stopping and deleting every pod this run created")
    except Exception as e:  # noqa: BLE001 -- e.g. a failed create; the finally still cleans up
        for p in pods:
            if p.error is None:
                p.error = f"not run: {e}" if p.id else str(e)
        log(f"ERROR: {e}")
    finally:
        STOP.set()
        kill_procs()
        # Ignore further interrupts: cleanup must finish
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        for p in pods:
            if p.id is None:
                continue
            # Only a pod that got as far as SSH is worth keeping for inspection
            if p.error and p.host and a.keep_on_failure and not interrupted:
                log(f"KEPT (--keep-on-failure): {p.name} id {p.id} root@{p.host} -p {p.port}; "
                    f"delete it with: curl -X DELETE {API}/pods/{p.id} "
                    f"-H 'Authorization: Bearer $RUNPOD_API_KEY'", p.tag)
            else:
                p.delete()
        shutil.rmtree(a.tmp, ignore_errors=True)

    if interrupted:
        return 130

    # Manifest + summary
    subprocess.run([sys.executable, str(RENDER_DIR / "make_manifest.py"), a.out_dir])
    still = set(pending_songs(a.music_dir, a.out_dir))
    log("summary")
    total_h = total_cost = 0.0
    for p in pods:
        p.rendered = sum(1 for s in p.songs if s not in still)
        hours = ((p.deleted or time.time()) - p.created) / 3600 if p.created else 0.0
        total_h += hours
        total_cost += hours * p.cost_per_hr
        state = "ok" if not p.error else f"FAILED: {p.error}"
        print(f"  {p.name} {p.id or '(not created)'} {p.gpu} ${p.cost_per_hr:.2f}/h: "
              f"{p.rendered}/{len(p.songs)} songs, {hours:.2f} pod-h, ~${hours * p.cost_per_hr:.2f}"
              f" [{state}]")
    print(f"  total: {len(pending) - len(still)}/{len(pending)} songs, {total_h:.2f} pod-hours, "
          f"~${total_cost:.2f}")
    if still:
        print(f"  still pending ({len(still)}): {' '.join(sorted(still))}")
    print(f"next: scripts/render/upload_catalog.sh {shlex.quote(a.out_dir)} \"$S3_CATALOG_URI\"")
    return 1 if still or any(p.error for p in pods) else 0


if __name__ == "__main__":
    sys.exit(main())
