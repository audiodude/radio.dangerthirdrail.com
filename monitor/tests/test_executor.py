# monitor/tests/test_executor.py
import datetime
import os
import sys
from types import SimpleNamespace

import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import monitor


def test_classify_states():
    assert monitor.classify(False, False, None, False, 0) == "OFF"
    assert monitor.classify(True, True, {"id": "B"}, True, 0) == "LIVE"
    assert monitor.classify(True, True, None, False, 0) == "WAITING"
    assert monitor.classify(True, True, None, False, 5) == "DEGRADED"   # past grace


RUN_ID = "10000000-0000-4000-8000-000000000001"
NEXT_RUN_ID = "10000000-0000-4000-8000-000000000002"


def run_monitor(monkeypatch, observations):
    class PollsComplete(Exception):
        pass

    index = 0
    events = []
    probes = []

    def sleep(_seconds):
        nonlocal index
        index += 1
        if index == len(observations):
            raise PollsComplete

    def probe():
        probes.append(index)
        opening = observations[index]["opening"]
        return {"opening": opening} if opening is not None else None

    def broadcasts():
        return [{"id": "B1", "status": {"lifeCycleStatus": observations[index]["life"]},
                 "contentDetails": {"boundStreamId": "S1"},
                 "snippet": {"scheduledStartTime": datetime.datetime.now(
                     datetime.timezone.utc).isoformat()}}]

    monkeypatch.setattr(monitor, "STREAM_ID", None)
    monkeypatch.setattr(monitor.time, "sleep", sleep)
    monkeypatch.setattr(monitor.windows, "in_operational_window", lambda: True)
    monkeypatch.setattr(monitor.windows, "in_consumer_window", lambda: False)
    monkeypatch.setattr(monitor.windows, "seconds_until_next_boundary", lambda: 600)
    monkeypatch.setattr(monitor.youtube, "get_owned_stream_id", lambda: "S1")
    monkeypatch.setattr(monitor.youtube, "list_broadcasts", broadcasts)
    monkeypatch.setattr(monitor.youtube, "stream_status",
                        lambda _sid: ("active" if observations[index]["active"] else "inactive", {}))
    monkeypatch.setattr(monitor.youtube, "go_live",
                        lambda bid: events.append((index, "go_live", bid)) or True)
    monkeypatch.setattr(monitor.redirect, "current_video_id", lambda: None)
    monkeypatch.setattr(monitor.boxhealth, "probe", probe)
    monkeypatch.setattr(monitor.boxhealth, "release_opening",
                        lambda rid: events.append((index, "release", rid)) or True)
    monkeypatch.setattr(monitor.boxhealth, "restart_opening",
                        lambda rid: events.append((index, "restart", rid)) or True)
    monkeypatch.setattr(monitor.alerts, "Alerter",
                        lambda: SimpleNamespace(update=lambda *_args: None))
    with pytest.raises(PollsComplete):
        monitor.main()
    return events, probes


def test_late_box_readiness_and_transition_success_never_release_before_listed_live(monkeypatch):
    waiting = {"run_id": RUN_ID, "phase": "waiting"}
    events, probes = run_monitor(monkeypatch, [
        {"opening": None, "life": "ready", "active": True},
        {"opening": waiting, "life": "ready", "active": False},
        {"opening": waiting, "life": "ready", "active": True},
        {"opening": waiting, "life": "testing", "active": True},
        {"opening": waiting, "life": "live", "active": True},
    ])
    assert events == [(2, "go_live", "B1"), (3, "go_live", "B1"), (4, "release", RUN_ID)]
    assert probes == [0, 1, 2, 3, 4]


def test_restarted_transport_is_observed_before_broadcast_goes_live(monkeypatch):
    events, _ = run_monitor(monkeypatch, [
        {"opening": {"run_id": RUN_ID, "phase": "show"}, "life": "ready", "active": True},
        {"opening": None, "life": "ready", "active": False},
        {"opening": {"run_id": NEXT_RUN_ID, "phase": "countdown"}, "life": "ready", "active": True},
        {"opening": {"run_id": NEXT_RUN_ID, "phase": "countdown"}, "life": "live", "active": True},
    ])
    assert events == [(0, "restart", RUN_ID), (2, "go_live", "B1"), (3, "release", NEXT_RUN_ID)]
