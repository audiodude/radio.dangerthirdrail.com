import os
import subprocess
import sys

import pytest

import station

STATION_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "station.py")


def live_state(seed, swapped_on):
    return {"sets": {seed: {"plan": f"/nowhere/set-{seed}/plan.json", "swap_at": "x",
                            "status": "live", "swapped_on": swapped_on, "vods": []}}}


@pytest.mark.parametrize("now, expected", [
    ("2026-11-11T03:00:00-08:00", "20261113 2026-11-13T06:00:00-08:00"),   # Wed bake
    ("2026-11-12T23:59:00-08:00", "20261113 2026-11-13T06:00:00-08:00"),   # Thu
    ("2026-11-13T03:00:00-08:00", "20261120 2026-11-20T06:00:00-08:00"),   # Fri -> next week
    ("2026-11-14T10:00:00-08:00", "20261120 2026-11-20T06:00:00-08:00"),   # Sat
    ("2026-10-28T03:00:00-07:00", "20261030 2026-10-30T06:00:00-07:00"),   # PDT offset
    ("2026-10-30T03:00:00-07:00", "20261106 2026-11-06T06:00:00-08:00"),   # across DST end
])
def test_target_is_the_coming_friday_at_six_pacific(world, now, expected):
    world.clock.set(now)
    assert world.target() == expected


def test_target_skips_while_a_set_is_pending(world):
    world.register("20261113", swap_at="2026-11-13T06:00:00-08:00")
    assert world.target() is None
    assert world.target(now=True) is None


def test_target_skips_a_seed_that_already_exists(world):
    world.write_state(live_state("20261113", "2026-11-06"))
    world.clock.set("2026-11-12T03:00:00-08:00")
    assert world.target() is None


def test_a_set_launched_tuesday_runs_a_full_first_week(world):
    world.write_state(live_state("20261110", "2026-11-10"))
    world.clock.set("2026-11-11T03:00:00-08:00")         # Friday is only 3 days out
    assert world.target() is None
    world.clock.set("2026-11-18T03:00:00-08:00")
    assert world.target() == "20261120 2026-11-20T06:00:00-08:00"


def test_a_set_launched_monday_swaps_that_friday(world):
    world.write_state(live_state("20261109", "2026-11-09"))   # 4 days before Friday
    assert world.target() == "20261113 2026-11-13T06:00:00-08:00"


def test_target_now_is_today_and_swaps_immediately(world):
    world.clock.set("2026-11-05T14:23:07-08:00")
    world.write_state(live_state("20261030", "2026-11-04"))   # recent live set doesn't block
    assert world.target(now=True) == "20261105 2026-11-05T14:23:07-08:00"
    world.write_state(live_state("20261105", "2026-11-05"))
    assert world.target(now=True) is None


def test_register_adds_a_pending_set_and_refuses_a_second(world):
    world.register("20261113", n=2, swap_at="2026-11-13T06:00:00-08:00")
    s = world.state()["sets"]["20261113"]
    assert s["status"] == "pending" and s["swap_at"] == "2026-11-13T06:00:00-08:00"
    assert [v["file"] for v in s["vods"]] == ["vod-000.mp4", "vod-001.mp4"]
    assert all(v["video_id"] is None and v["attempts"] == 0 for v in s["vods"])
    plan = world.make_set("20261120", swap_at="2026-11-20T06:00:00-08:00")
    with pytest.raises(station.StationError):
        station.cmd_register(world.cfg, plan, world.clock())


def test_cli_target_now_prints_seed_and_time_or_nothing(tmp_path):
    env = {**os.environ, "STATION_DIR": str(tmp_path)}
    out = subprocess.run([sys.executable, STATION_PY, "target", "--now"], env=env,
                         capture_output=True, text=True, check=True).stdout
    seed, swap_at = out.split()
    assert len(seed) == 8 and seed.isdigit()
    assert station.parse(swap_at).strftime("%Y%m%d") == seed


def test_cli_alert_never_fails_without_telegram(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    p = subprocess.run([sys.executable, STATION_PY, "alert", "bake failed"], env=env,
                       capture_output=True, text=True)
    assert p.returncode == 0
    assert "radio station: bake failed" in p.stderr
