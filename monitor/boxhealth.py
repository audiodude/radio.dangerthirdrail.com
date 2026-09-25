"""Token-authed box health and run-scoped opening control."""
import json
import os
import sys
import urllib.parse
import urllib.request

BOX_HEALTH_URL = os.environ.get("BOX_HEALTH_URL", "")
BOX_HEALTH_TOKEN = os.environ.get("BOX_HEALTH_TOKEN", "")


def _log(msg):
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def _request(url, payload=None):
    if not BOX_HEALTH_URL or not BOX_HEALTH_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {BOX_HEALTH_TOKEN}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode())
        return result if isinstance(result, dict) else None
    except Exception as e:
        _log(f"box request failed: {e}")
        return None


def probe():
    return _request(BOX_HEALTH_URL)


def release_opening(run_id):
    url = urllib.parse.urljoin(BOX_HEALTH_URL, "/opening/release")
    return _request(url, {"run_id": run_id}) is not None


def restart_opening(run_id):
    url = urllib.parse.urljoin(BOX_HEALTH_URL, "/opening/restart")
    return _request(url, {"run_id": run_id}) is not None
