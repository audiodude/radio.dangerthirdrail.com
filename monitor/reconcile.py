# monitor/reconcile.py
"""Pure reconcile planner. Given current state, return the list of actions to
take. No I/O — every action is a tuple the executor in monitor.py runs. All
broadcast actions are scoped to the owned stream via youtube.owned_broadcasts."""
import uuid
import youtube


def plan_actions(now, in_op, in_consumer, stream_id, broadcasts, stream_active,
                 current_redirect_vid, opening):
    owned = youtube.owned_broadcasts(broadcasts, stream_id)
    live = next((b for b in owned if youtube.life(b) == "live"), None)
    pending = [b for b in owned if youtube.life(b) in ("created", "ready", "testing")]
    recent_pending = next((b for b in pending if youtube.is_recent(b, now)), None)
    actions = []

    if not in_op:
        for b in owned:
            if youtube.life(b) in ("live", "testing"):
                actions.append(("end_broadcast", b["id"]))
        if current_redirect_vid is not None:
            actions.append(("redirect_offline",))
        return actions

    # in operational window
    for b in pending:
        if not youtube.is_recent(b, now):
            actions.append(("delete_broadcast", b["id"]))

    run_id, phase = None, None
    if isinstance(opening, dict) and isinstance(opening.get("run_id"), str):
        try:
            if str(uuid.UUID(opening["run_id"])) == opening["run_id"]:
                run_id, phase = opening["run_id"], opening.get("phase")
        except ValueError:
            pass

    if live is None:
        if (phase in ("ident", "show")
                or (phase in ("countdown", "waiting") and opening.get("released") is True)):
            # A prior broadcast has spent or reserved this opening. Reset it
            # before creating or publishing another broadcast.
            actions.append(("restart_opening", run_id))
        elif phase in ("countdown", "waiting"):
            if recent_pending is None:
                actions.append(("create_broadcast",))
            elif stream_active:
                actions.append(("go_live", recent_pending["id"]))
    elif stream_active and phase in ("countdown", "waiting"):
        # Only the observed live listing confirms readiness. A successful
        # go_live call (including its testing fallback) does not release music.
        actions.append(("release_opening", run_id))

    target = live["id"] if (in_consumer and live is not None and stream_active) else None
    if target is not None and current_redirect_vid != target:
        actions.append(("redirect_online", target))
    elif target is None and current_redirect_vid is not None:
        actions.append(("redirect_offline",))

    return actions
