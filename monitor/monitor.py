#!/usr/bin/env python3
"""vxstory monitor: observe the Hetzner box, own the YouTube broadcast lifecycle
(scoped to our stream), drive the radio redirect, ride out drops, alert.

Loop: gather state (windows + broadcasts + stream status + box health) -> pure
reconcile.plan_actions -> execute actions -> update alerter -> sleep to the next
poll tick or window edge."""
import datetime
import os
import time

import alerts
import boxhealth
import reconcile
import redirect
import windows
import youtube

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
DEGRADED_GRACE_POLLS = int(os.environ.get("DEGRADED_GRACE_POLLS", "3"))
UTC = datetime.timezone.utc

STREAM_ID = None  # resolved once at first successful poll


def _log(msg):
    import sys
    print(f"[monitor] {msg}", file=sys.stderr, flush=True)


def execute(action):
    kind = action[0]
    if kind == "create_broadcast":
        youtube.ensure_broadcast(STREAM_ID)
    elif kind == "go_live":
        youtube.go_live(action[1])
    elif kind == "end_broadcast":
        youtube.end_broadcast(action[1])
    elif kind == "delete_broadcast":
        youtube.delete_broadcast(action[1])
    elif kind == "redirect_online":
        redirect.set_radio_online(action[1])
    elif kind == "redirect_offline":
        redirect.set_radio_offline()
    elif kind == "release_opening":
        boxhealth.release_opening(action[1])
    elif kind == "restart_opening":
        boxhealth.restart_opening(action[1])


def classify(in_op, in_consumer, live, stream_active, degraded_polls):
    if not in_op:
        return "OFF"
    if live is not None and stream_active:
        return "LIVE"
    if degraded_polls >= DEGRADED_GRACE_POLLS:
        return "DEGRADED"
    return "WAITING"


def main():
    global STREAM_ID
    _log(f"vxstory monitor starting, poll every {POLL_INTERVAL}s")
    alerter = alerts.Alerter()
    degraded_polls = 0
    first = True
    while True:
        if not first:
            time.sleep(max(1, min(POLL_INTERVAL, windows.seconds_until_next_boundary() + 1)))
        first = False

        now = datetime.datetime.now(UTC)
        in_op = windows.in_operational_window()
        in_consumer = windows.in_consumer_window()
        box = boxhealth.probe() if in_op else None
        opening = box.get("opening") if box is not None else None

        STREAM_ID = youtube.get_owned_stream_id()
        if STREAM_ID is None:
            _log("cannot resolve owned stream; skipping poll")
            alerter.update("DEGRADED" if in_op else "OFF", "owned stream unresolved")
            continue

        broadcasts = youtube.list_broadcasts()
        owned = youtube.owned_broadcasts(broadcasts, STREAM_ID)
        live = next((b for b in owned if youtube.life(b) == "live"), None)
        ss, _health = youtube.stream_status(STREAM_ID)
        stream_active = ss == "active"
        cur_vid = redirect.current_video_id()

        actions = reconcile.plan_actions(now, in_op, in_consumer, STREAM_ID,
                                         broadcasts, stream_active, cur_vid, opening)
        for a in actions:
            try:
                execute(a)
            except Exception as e:
                _log(f"action {a} failed: {e}")

        # alert state
        if in_op and live is None and not stream_active:
            degraded_polls += 1
        else:
            degraded_polls = 0
        state = classify(in_op, in_consumer, live, stream_active, degraded_polls)
        reason = f"stream={ss or 'none'}, live={'yes' if live else 'no'}"
        if state == "DEGRADED":
            reason += "; box=" + ("unreachable" if box is None else "alive")
        alerter.update(state, reason)


if __name__ == "__main__":
    main()
