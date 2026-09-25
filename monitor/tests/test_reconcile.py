# monitor/tests/test_reconcile.py
import os, sys, datetime
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reconcile
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 6, 19, 13, 0, tzinfo=UTC)
RUN_ID = "10000000-0000-4000-8000-000000000001"

def b(bid, life, bound, sst="2026-06-19T12:59:30Z"):  # recent by default
    return {"id": bid, "status": {"lifeCycleStatus": life},
            "contentDetails": {"boundStreamId": bound},
            "snippet": {"scheduledStartTime": sst}}

def plan(**kw):
    base = dict(now=NOW, in_op=True, in_consumer=True, stream_id="S1",
                broadcasts=[], stream_active=False, current_redirect_vid=None,
                opening={"run_id": RUN_ID, "phase": "countdown"})
    base.update(kw)
    return reconcile.plan_actions(**base)

# THE headline regression: out of window, a FOREIGN live broadcast must be untouched
def test_out_of_window_foreign_live_is_noop():
    acts = plan(in_op=False, in_consumer=False,
                broadcasts=[b("foreign", "live", "S_OTHER")])
    assert ("end_broadcast", "foreign") not in acts
    assert all(a[0] != "end_broadcast" for a in acts)

def test_out_of_window_owned_live_is_ended():
    acts = plan(in_op=False, in_consumer=False,
                broadcasts=[b("mine", "live", "S1")])
    assert ("end_broadcast", "mine") in acts

def test_in_window_no_broadcast_creates():
    assert ("create_broadcast",) in plan(broadcasts=[])

@pytest.mark.parametrize("phase", ["countdown", "waiting"])
def test_in_window_recent_pending_goes_live_during_opening(phase):
    acts = plan(broadcasts=[b("mine", "ready", "S1")], stream_active=True,
                opening={"run_id": RUN_ID, "phase": phase})
    assert ("go_live", "mine") in acts

def test_in_window_pending_waits_when_stream_inactive():
    acts = plan(broadcasts=[b("mine", "ready", "S1")], stream_active=False)
    assert all(a[0] not in ("go_live", "create_broadcast") for a in acts)

def test_redirect_online_only_in_consumer_with_live_active():
    acts = plan(broadcasts=[b("mine", "live", "S1")], stream_active=True)
    assert ("redirect_online", "mine") in acts

def test_redirect_offline_when_outside_consumer():
    acts = plan(in_op=True, in_consumer=False, broadcasts=[b("mine", "live", "S1")],
                stream_active=True, current_redirect_vid="mine")
    assert ("redirect_offline",) in acts

def test_redirect_online_not_repeated_when_already_pointed():
    acts = plan(broadcasts=[b("mine", "live", "S1")], stream_active=True,
                current_redirect_vid="mine")
    assert all(a[0] != "redirect_online" for a in acts)

def test_in_window_stale_pending_deleted():
    acts = plan(broadcasts=[b("old", "ready", "S1", sst="2026-06-19T12:00:00Z")])  # 60 min old
    assert ("delete_broadcast", "old") in acts


@pytest.mark.parametrize("opening", [None, {}, {"phase": "waiting"},
                                    {"run_id": RUN_ID, "phase": "unknown"},
                                    {"run_id": "not-a-uuid", "phase": "waiting"},
                                    {"run_id": RUN_ID, "phase": "idle"},
                                    {"run_id": RUN_ID, "phase": "idle", "released": True}])
@pytest.mark.parametrize("broadcasts", [[], [b("mine", "ready", "S1")]])
def test_waits_when_box_opening_is_unavailable(opening, broadcasts):
    acts = plan(broadcasts=broadcasts, stream_active=True, opening=opening)
    assert acts == []


@pytest.mark.parametrize("phase", ["ident", "show"])
@pytest.mark.parametrize("broadcasts", [[], [b("mine", "ready", "S1")]])
def test_prior_run_restarts_before_new_broadcast_or_go_live(phase, broadcasts):
    acts = plan(broadcasts=broadcasts, stream_active=True,
                opening={"run_id": RUN_ID, "phase": phase})
    assert acts == [("restart_opening", RUN_ID)]


def test_late_readiness_waits_for_listed_live_before_release():
    opening = {"run_id": RUN_ID, "phase": "waiting"}
    pending = [b("mine", "ready", "S1")]
    assert plan(broadcasts=pending, opening=opening) == []
    assert plan(broadcasts=pending, stream_active=True, opening=opening) == [
        ("go_live", "mine")]
    assert plan(broadcasts=[b("mine", "testing", "S1")], stream_active=True,
                opening=opening) == [("go_live", "mine")]
    assert plan(broadcasts=[b("mine", "live", "S1")], stream_active=True,
                opening=opening) == [("release_opening", RUN_ID), ("redirect_online", "mine")]


@pytest.mark.parametrize("phase", ["countdown", "waiting"])
def test_observed_live_releases_even_before_consumer_window(phase):
    acts = plan(in_consumer=False, broadcasts=[b("mine", "live", "S1")],
                stream_active=True, opening={"run_id": RUN_ID, "phase": phase})
    assert acts == [("release_opening", RUN_ID)]


def test_live_but_inactive_does_not_release():
    assert plan(broadcasts=[b("mine", "live", "S1")]) == []


def test_foreign_live_does_not_release_owned_pending():
    acts = plan(broadcasts=[b("foreign", "live", "S_OTHER"), b("mine", "ready", "S1")],
                stream_active=True)
    assert acts == [("go_live", "mine")]


def test_out_of_window_never_controls_opening():
    acts = plan(in_op=False, broadcasts=[b("mine", "live", "S1")], stream_active=True)
    assert acts == [("end_broadcast", "mine")]


@pytest.mark.parametrize("phase", ["countdown", "waiting"])
def test_released_opening_cannot_be_reused_by_new_broadcast(phase):
    acts = plan(broadcasts=[b("replacement", "ready", "S1")], stream_active=True,
                opening={"run_id": RUN_ID, "phase": phase, "released": True})
    assert acts == [("restart_opening", RUN_ID)]


