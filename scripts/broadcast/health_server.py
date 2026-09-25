#!/usr/bin/env python3
"""Token-authed health and opening control for pre-baked playout.

GET /health preserves process/heartbeat health and adds the current opening.
POST /opening/release or /opening/restart accepts {"run_id": "<current UUID>"}.
Missing/invalid/empty bearer tokens fail closed. Binds loopback behind cloudflared.
Operational window mirrors monitor/windows.py.
"""
import datetime
import hmac
import http.server
import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

HEARTBEAT = os.environ.get("PLAYOUT_HEARTBEAT", "/tmp/playout_heartbeat")
PLAYOUT_STATE = os.environ.get("PLAYOUT_STATE", "/tmp/playout_state.json")
PLAYOUT_RELEASE = os.environ.get("PLAYOUT_RELEASE", "/tmp/playout_release.json")
PLAYOUT_RESTART = os.environ.get("PLAYOUT_RESTART", "/tmp/playout_restart.json")
TOKEN = os.environ.get("BOX_HEALTH_TOKEN", "")
PORT = int(os.environ.get("BOX_HEALTH_PORT", "8088"))
FRESH_MAX_AGE = float(os.environ.get("PLAYOUT_HEARTBEAT_MAX_AGE", "30"))
ACTIVE_TZ = ZoneInfo("America/Los_Angeles")
MAX_COMMAND_BYTES = 1024
OPENING_PHASES = frozenset(("countdown", "waiting", "ident", "show", "idle"))


def _ffmpeg_alive():
    try:
        return subprocess.run(["pgrep", "-x", "ffmpeg"], capture_output=True).returncode == 0
    except Exception:
        return False


def _heartbeat_age_s():
    try:
        return int(time.time() - os.path.getmtime(HEARTBEAT))
    except OSError:
        return -1


def _in_window(now=None):
    now = now or datetime.datetime.now(ACTIVE_TZ)
    return (11, 45) <= (now.hour, now.minute) < (18, 5)



def _valid_run_id(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _opening_state():
    try:
        with open(PLAYOUT_STATE) as f:
            state = json.load(f)
        if (isinstance(state, dict) and _valid_run_id(state.get("run_id"))
                and state.get("phase") in OPENING_PHASES):
            released = False
            try:
                with open(PLAYOUT_RELEASE) as f:
                    command = json.load(f)
                released = isinstance(command, dict) and command.get("run_id") == state["run_id"]
            except (OSError, ValueError):
                pass
            return {"run_id": state["run_id"], "phase": state["phase"], "released": released}
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_command(path, run_id):
    # The runtime also checks the UUID, so a transport rollover during this
    # write cannot release or restart the replacement run.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path) or ".",
                                         prefix=".opening-", delete=False) as f:
            temporary = f.name
            json.dump({"run_id": run_id}, f)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)

def health_payload(ffmpeg_alive_fn=_ffmpeg_alive, heartbeat_age_fn=_heartbeat_age_s,
                   in_window_fn=_in_window, opening_fn=_opening_state):
    ff = ffmpeg_alive_fn()
    age = heartbeat_age_fn()
    fresh = 0 <= age < FRESH_MAX_AGE
    # playout_alive: doing its job — streaming-and-fresh in-window, or idle out-of-window.
    playout_alive = (ff and fresh) if in_window_fn() else True
    opening = opening_fn() if ff and fresh else None
    return {"playout_alive": playout_alive, "ffmpeg_alive": ff,
            "heartbeat_age_s": age, "opening": opening}


class H(http.server.BaseHTTPRequestHandler):
    def _respond(self, status, payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authenticated(self):
        supplied = self.headers.get("Authorization", "")
        if not TOKEN or not hmac.compare_digest(supplied.encode(), f"Bearer {TOKEN}".encode()):
            self._respond(401)
            return False
        return True

    def do_GET(self):
        if self.path != "/health":
            self._respond(404)
            return
        if self._authenticated():
            self._respond(200, health_payload())

    def do_POST(self):
        if self.path not in ("/opening/release", "/opening/restart"):
            self._respond(404)
            return
        if not self._authenticated():
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._respond(400)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._respond(411)
            return
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._respond(400)
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self._respond(400)
            return
        if length > MAX_COMMAND_BYTES:
            self._respond(413)
            return
        try:
            self.connection.settimeout(5)
            body = self.rfile.read(length)
        except (OSError, socket.timeout):
            self._respond(408)
            return
        try:
            payload = json.loads(body) if len(body) == length else None
        except (ValueError, UnicodeError):
            payload = None
        if not isinstance(payload, dict) or not _valid_run_id(payload.get("run_id")):
            self._respond(400)
            return
        opening = health_payload()["opening"]
        if opening is None or opening["phase"] == "idle":
            self._respond(503)
            return
        if payload["run_id"] != opening["run_id"]:
            self._respond(409)
            return
        release = self.path == "/opening/release"
        if release:
            should_write = opening["phase"] in ("countdown", "waiting")
        else:
            should_write = opening["phase"] in ("ident", "show") or opening["released"]
        if should_write:
            try:
                _write_command(PLAYOUT_RELEASE if release else PLAYOUT_RESTART, opening["run_id"])
            except OSError:
                self._respond(503)
                return
        self._respond(200, opening)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    http.server.HTTPServer(("127.0.0.1", PORT), H).serve_forever()
