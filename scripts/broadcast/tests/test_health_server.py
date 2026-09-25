# scripts/broadcast/tests/test_health_server.py
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health_server as hs
sys.path.insert(0, str(Path(hs.__file__).resolve().parents[2] / "monitor"))
import boxhealth

RUN_ID = "10000000-0000-4000-8000-000000000001"
NEXT_RUN_ID = "10000000-0000-4000-8000-000000000002"


def test_payload_in_window_streaming():
    opening = {"run_id": RUN_ID, "phase": "waiting"}
    p = hs.health_payload(lambda: True, lambda: 2, lambda: True, lambda: opening)
    assert p == {"playout_alive": True, "ffmpeg_alive": True,
                 "heartbeat_age_s": 2, "opening": opening}


@pytest.mark.parametrize("ffmpeg,age", [(False, 2), (True, 30), (True, -1)])
def test_unavailable_playout_cannot_advertise_stale_opening(ffmpeg, age):
    p = hs.health_payload(lambda: ffmpeg, lambda: age, lambda: True,
                          lambda: {"run_id": RUN_ID, "phase": "waiting"})
    assert p["playout_alive"] is False
    assert p["opening"] is None


def test_payload_out_of_window_is_idle_ok():
    p = hs.health_payload(lambda: False, lambda: -1, lambda: False)
    assert p["playout_alive"] is True and p["heartbeat_age_s"] == -1
    assert p["opening"] is None


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(hs, "TOKEN", "secret")
    monkeypatch.setattr(hs, "PLAYOUT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(hs, "PLAYOUT_RELEASE", str(tmp_path / "release.json"))
    monkeypatch.setattr(hs, "PLAYOUT_RESTART", str(tmp_path / "restart.json"))
    Path(hs.PLAYOUT_STATE).write_text(json.dumps({"run_id": RUN_ID, "phase": "waiting"}))
    payload = hs.health_payload
    monkeypatch.setattr(hs, "health_payload", lambda: payload(lambda: True, lambda: 2, lambda: True))
    with http.server.HTTPServer(("127.0.0.1", 0), hs.H) as srv:
        thread = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01))
        thread.start()
        try:
            yield srv
        finally:
            srv.shutdown()
            thread.join()


def request(server, path="/health", auth="Bearer secret", data=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=data,
                                 headers=headers or {})
    if auth is not None:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def command(server, action="release", run_id=RUN_ID, **kwargs):
    return request(server, f"/opening/{action}", data=json.dumps({"run_id": run_id}).encode(), **kwargs)


@pytest.mark.parametrize("path", ["/health", "/opening/release", "/opening/restart"])
@pytest.mark.parametrize("configured,auth", [("secret", None), ("secret", "Bearer nope"), ("", "Bearer ")])
def test_http_authentication_fails_closed(server, monkeypatch, path, configured, auth):
    monkeypatch.setattr(hs, "TOKEN", configured)
    data = json.dumps({"run_id": RUN_ID}).encode() if path != "/health" else None
    assert request(server, path, auth=auth, data=data)[0] == 401
    assert not Path(hs.PLAYOUT_RELEASE).exists()
    assert not Path(hs.PLAYOUT_RESTART).exists()


def test_http_health_exposes_current_opening(server):
    code, body = request(server)
    assert code == 200
    assert json.loads(body)["opening"] == {"run_id": RUN_ID, "phase": "waiting", "released": False}


@pytest.mark.parametrize("state", [None, b"{", b"[]", b'{"run_id":"invalid","phase":"waiting"}',
                                    json.dumps({"run_id": RUN_ID, "phase": "unknown"}).encode()])
def test_missing_or_invalid_state_blocks_commands(server, state):
    path = Path(hs.PLAYOUT_STATE)
    if state is None:
        path.unlink()
    else:
        path.write_bytes(state)
    assert json.loads(request(server)[1])["opening"] is None
    assert command(server)[0] == 503
    assert not Path(hs.PLAYOUT_RELEASE).exists()


@pytest.mark.parametrize("action", ["release", "restart"])
def test_stale_run_cannot_control_replacement(server, action):
    Path(hs.PLAYOUT_STATE).write_text(json.dumps({"run_id": NEXT_RUN_ID, "phase": "waiting"}))
    assert command(server, action)[0] == 409
    assert not Path(hs.PLAYOUT_RELEASE).exists()
    assert not Path(hs.PLAYOUT_RESTART).exists()


@pytest.mark.parametrize("phase", ["countdown", "waiting", "ident", "show"])
def test_commands_are_phase_scoped_and_idempotent(server, phase):
    Path(hs.PLAYOUT_STATE).write_text(json.dumps({"run_id": RUN_ID, "phase": phase}))
    assert command(server, "restart")[0] == 200
    assert command(server, "release")[0] == 200
    expected = hs.PLAYOUT_RELEASE if phase in ("countdown", "waiting") else hs.PLAYOUT_RESTART
    other = hs.PLAYOUT_RESTART if expected == hs.PLAYOUT_RELEASE else hs.PLAYOUT_RELEASE
    assert json.loads(Path(expected).read_text()) == {"run_id": RUN_ID}
    assert not Path(other).exists()


@pytest.mark.parametrize("phase", ["countdown", "waiting"])
def test_released_opening_can_restart_before_ident(server, phase):
    Path(hs.PLAYOUT_STATE).write_text(json.dumps({"run_id": RUN_ID, "phase": phase}))
    assert command(server, "release")[0] == 200
    assert json.loads(request(server)[1])["opening"]["released"] is True
    assert command(server, "restart")[0] == 200
    assert json.loads(Path(hs.PLAYOUT_RESTART).read_text()) == {"run_id": RUN_ID}


def test_stale_release_does_not_reserve_new_opening(server):
    Path(hs.PLAYOUT_RELEASE).write_text(json.dumps({"run_id": NEXT_RUN_ID}))
    assert json.loads(request(server)[1])["opening"]["released"] is False
    assert command(server, "restart")[0] == 200
    assert not Path(hs.PLAYOUT_RESTART).exists()


def test_idle_state_rejects_commands(server):
    Path(hs.PLAYOUT_STATE).write_text(json.dumps({"run_id": RUN_ID, "phase": "idle"}))
    assert command(server)[0] == 503


@pytest.mark.parametrize("body,status", [(b"{", 400), (b"[]", 400), (b"{}", 400),
                                         (b'{"run_id":"bad"}', 400), (b"x" * 1025, 413)])
def test_invalid_or_oversize_commands_do_not_write(server, body, status):
    assert request(server, "/opening/release", data=body)[0] == status
    assert not Path(hs.PLAYOUT_RELEASE).exists()


@pytest.mark.parametrize("headers", [{"Content-Length": "-1"}, {"Content-Length": "bad"},
                                     {"Transfer-Encoding": "chunked"}])
def test_invalid_body_framing_is_rejected(server, headers):
    assert request(server, "/opening/release", data=b"{}", headers=headers)[0] == 400
    assert not Path(hs.PLAYOUT_RELEASE).exists()


def test_failed_atomic_write_preserves_prior_command(server, monkeypatch):
    prior = {"run_id": NEXT_RUN_ID}
    Path(hs.PLAYOUT_RELEASE).write_text(json.dumps(prior))

    def fail_replace(_source, _target):
        raise OSError("unwritable command path")

    monkeypatch.setattr(hs.os, "replace", fail_replace)
    assert command(server)[0] == 503
    assert json.loads(Path(hs.PLAYOUT_RELEASE).read_text()) == prior


def test_monitor_client_controls_only_current_run_over_http(server, monkeypatch):
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_URL", f"http://127.0.0.1:{server.server_port}/health")
    monkeypatch.setattr(boxhealth, "BOX_HEALTH_TOKEN", "secret")
    assert boxhealth.probe()["opening"] == {"run_id": RUN_ID, "phase": "waiting", "released": False}
    assert boxhealth.release_opening(RUN_ID) is True
    assert json.loads(Path(hs.PLAYOUT_RELEASE).read_text()) == {"run_id": RUN_ID}
    Path(hs.PLAYOUT_STATE).write_text(json.dumps({"run_id": NEXT_RUN_ID, "phase": "show"}))
    assert boxhealth.restart_opening(RUN_ID) is False
    assert not Path(hs.PLAYOUT_RESTART).exists()
    assert boxhealth.restart_opening(NEXT_RUN_ID) is True
    assert json.loads(Path(hs.PLAYOUT_RESTART).read_text()) == {"run_id": NEXT_RUN_ID}
