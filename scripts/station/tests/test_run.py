import os

import pytest

from conftest import CHUNK
from fake_youtube import Crash

FRI_SWAP = "2026-11-13T06:00:00-08:00"


def msgs_with(world, needle):
    return [m for m in world.sent if needle in m]


def week_two(world, n=3):
    """Set 20261106 live since last Friday; set 20261113 registered Wednesday."""
    world.clock.set("2026-11-06T06:00:00-08:00")
    old = world.bring_live("20261106", n=2)
    world.clock.set("2026-11-11T12:00:00-08:00")
    world.register("20261113", n=n, swap_at=FRI_SWAP)
    return old


def uploaded(world, seed):
    return [v["video_id"] for v in world.state()["sets"][seed]["vods"]]


# --- playlist ---

def test_first_run_creates_a_public_playlist_and_announces_it_once(world):
    assert world.run() == 0
    pid = world.state()["playlist_id"]
    pl = world.fake.playlists[pid]
    assert pl["status"]["privacyStatus"] == "public"
    assert pl["snippet"] == {"title": "Test Station", "description": "Shocking visuals, all week."}
    assert len(msgs_with(world, f"https://www.youtube.com/playlist?list={pid}")) == 1
    assert world.run() == 0
    assert len(world.fake.playlists) == 1
    assert len(msgs_with(world, "playlist?list=")) == 1
    assert all(m.startswith("radio station:") for m in world.sent)


# --- upload, process, swap ---

def test_set_uploads_unlisted_then_swaps_in_at_swap_time(world):
    world.register("20261113", n=3, swap_at=FRI_SWAP)
    assert world.run() == 0
    ids = uploaded(world, "20261113")
    assert all(ids)
    for i, vid in enumerate(ids):
        v = world.fake.videos[vid]
        assert v["title"] == f"Song 20261113-{i}" and v["description"] == f"desc {i}"
        assert v["privacyStatus"] == "unlisted" and v["categoryId"] == "10"
        assert v["madeForKids"] is False
        with open(os.path.join(world.dir, "set-20261113", f"vod-{i:03d}.mp4"), "rb") as f:
            assert v["data"] == f.read()
    assert all(v["upload_url"] is None for v in world.state()["sets"]["20261113"]["vods"])

    world.clock.set("2026-11-12T03:00:00-08:00")
    world.run()
    assert all(v["processed"] for v in world.state()["sets"]["20261113"]["vods"])
    world.clock.set("2026-11-13T05:59:00-08:00")
    world.run()
    assert world.playlist() == []
    world.clock.set("2026-11-13T06:00:00-08:00")
    world.run()
    assert world.playlist() == ids
    s = world.state()["sets"]["20261113"]
    assert s["status"] == "live" and s["swapped_on"] == "2026-11-13"
    assert len(msgs_with(world, "swapped in set 20261113 (3 VODs)")) == 1
    assert world.fake.count("POST", "upload") == 3        # no duplicate uploads


def test_crash_mid_upload_resumes_the_same_session(world):
    world.register("20261113", n=1, swap_at=FRI_SWAP, size=2 * CHUNK + 1000)
    world.fake.crash_on_chunk = 2
    with pytest.raises(Crash):
        world.run()
    vod = world.state()["sets"]["20261113"]["vods"][0]
    assert vod["upload_url"] and vod["video_id"] is None

    assert world.run() == 0
    vod = world.state()["sets"]["20261113"]["vods"][0]
    assert vod["video_id"] in world.fake.videos and vod["upload_url"] is None
    assert len(world.fake.videos) == 1 and world.fake.count("POST", "upload") == 1
    assert [c[1] for c in world.fake.chunks] == [0, CHUNK, 2 * CHUNK]   # nothing resent
    with open(os.path.join(world.dir, "set-20261113", "vod-000.mp4"), "rb") as f:
        assert world.fake.videos[vod["video_id"]]["data"] == f.read()


def test_expired_upload_session_starts_over(world):
    world.register("20261113", n=1, swap_at=FRI_SWAP, size=2 * CHUNK)
    world.fake.crash_on_chunk = 1
    with pytest.raises(Crash):
        world.run()
    for s in world.fake.sessions.values():
        s["gone"] = True
    assert world.run() == 0
    assert all(uploaded(world, "20261113"))
    assert len(world.fake.videos) == 1 and world.fake.count("POST", "upload") == 2


def test_upload_cap_pauses_uploads_for_24h_then_resumes(world):
    world.clock.set("2026-11-11T12:00:00-08:00")
    world.register("20261113", n=3, swap_at=FRI_SWAP)
    world.fake.uploads_left = 1
    assert world.run() == 0
    ids = uploaded(world, "20261113")
    assert ids[0] and ids[1:] == [None, None]
    assert world.state()["upload_until"] == "2026-11-12T12:00:00-08:00"
    assert len(msgs_with(world, "upload cap")) == 1

    world.fake.uploads_left = None
    world.clock.set("2026-11-12T11:00:00-08:00")
    assert world.run() == 0
    assert world.fake.count("POST", "upload") == 2        # paused: no new attempts
    assert len(msgs_with(world, "upload cap")) == 1

    world.clock.set("2026-11-12T12:00:00-08:00")
    assert world.run() == 0
    assert all(uploaded(world, "20261113"))
    assert world.state()["upload_until"] is None


@pytest.mark.parametrize("outcome", ["failed", "rejected", "vanished"])
def test_failed_processing_deletes_and_reuploads(world, outcome):
    world.register("20261113", n=2, swap_at=FRI_SWAP)
    if outcome != "vanished":
        world.fake.processing_result = outcome
    world.run()
    first = uploaded(world, "20261113")
    if outcome == "vanished":
        del world.fake.videos[first[1]]
    else:
        world.fake.processing_result = "processed"
        world.fake.videos[first[0]]["uploadStatus"] = "processed"
    world.clock.advance(hours=1)
    assert world.run() == 0
    vods = world.state()["sets"]["20261113"]["vods"]
    assert vods[0]["video_id"] == first[0] and vods[0]["processed"] and vods[0]["attempts"] == 0
    assert first[1] not in world.fake.videos                     # bad one deleted
    assert vods[1]["video_id"] not in (None, first[1])           # and re-uploaded
    assert vods[1]["attempts"] == 1 and not vods[1]["processed"]
    world.clock.advance(hours=1)
    world.run()
    assert all(v["processed"] for v in world.state()["sets"]["20261113"]["vods"])


def test_repeated_processing_failure_alerts_once_at_three_attempts(world):
    world.register("20261113", n=1, swap_at=FRI_SWAP)
    world.fake.processing_result = "rejected"
    world.run()
    for _ in range(4):
        world.clock.advance(hours=1)
        world.run()
    assert world.state()["sets"]["20261113"]["vods"][0]["attempts"] == 4
    assert len(msgs_with(world, "failed 3 times")) == 1


def test_swap_appends_new_set_then_removes_old_and_strays(world):
    old = week_two(world)
    pid = world.state()["playlist_id"]
    stray = world.fake.add_video()
    world.fake.add_item(pid, stray)
    world.run()
    new = uploaded(world, "20261113")
    world.clock.set("2026-11-12T03:00:00-08:00")
    world.run()
    assert world.playlist() == old + [stray]

    world.clock.set(FRI_SWAP)
    mark = len(world.fake.calls)
    assert world.run() == 0
    assert world.playlist() == new
    edits = [c for c in world.fake.calls[mark:] if c[1] == "playlistItems" and c[0] != "GET"]
    assert edits == [("POST", "playlistItems")] * 3 + [("DELETE", "playlistItems")] * 3
    sets = world.state()["sets"]
    assert sets["20261106"]["status"] == "old" and sets["20261106"]["retire_on"] == "2026-11-14"
    assert sets["20261113"]["status"] == "live"


def test_swap_refuses_a_playlist_with_the_new_set_out_of_order(world):
    old = week_two(world)
    world.run()
    world.clock.set("2026-11-12T03:00:00-08:00")
    world.run()
    new = uploaded(world, "20261113")
    pid = world.state()["playlist_id"]
    world.fake.add_item(pid, new[1])
    world.fake.add_item(pid, new[0])
    before = list(world.fake.items)

    world.clock.set(FRI_SWAP)
    assert world.run() == 0
    assert world.fake.items == before
    assert world.state()["sets"]["20261113"]["status"] == "pending"
    assert len(msgs_with(world, "playlist order unexpected")) == 1
    world.clock.advance(hours=1)
    world.run()
    assert world.fake.items == before
    assert len(msgs_with(world, "playlist order unexpected")) == 1
    assert world.playlist()[:2] == old


def test_half_finished_swap_completes_on_the_next_run(world):
    week_two(world)
    world.run()
    world.clock.set("2026-11-12T03:00:00-08:00")
    world.run()
    new = uploaded(world, "20261113")

    world.clock.set(FRI_SWAP)
    world.fake.crash_before = ("POST", "playlistItems", world.fake.count("POST", "playlistItems") + 2)
    with pytest.raises(Crash):
        world.run()
    assert world.playlist()[-1] == new[0] and world.state()["sets"]["20261113"]["status"] == "pending"

    world.clock.advance(hours=1)
    assert world.run() == 0
    assert world.playlist() == new
    assert world.state()["sets"]["20261113"]["status"] == "live"


def test_quota_stops_until_pacific_midnight_then_finishes_the_swap(world):
    week_two(world)
    world.run()
    world.clock.set("2026-11-12T03:00:00-08:00")
    world.run()
    new = uploaded(world, "20261113")

    world.clock.set(FRI_SWAP)
    world.fake.quota_budget = 3            # list + 2 inserts, then quotaExceeded
    assert world.run() == 0
    assert world.state()["quota_until"] == "2026-11-14T00:00:00-08:00"
    assert len(msgs_with(world, "quota")) == 1

    calls = len(world.fake.calls)
    for t in ("2026-11-13T07:00:00-08:00", "2026-11-13T23:59:00-08:00"):
        world.clock.set(t)
        assert world.run() == 0
    assert len(world.fake.calls) == calls                 # nothing touched YouTube
    assert len(msgs_with(world, "quota")) == 1

    world.fake.quota_budget = None
    world.clock.set("2026-11-14T00:00:00-08:00")
    assert world.run() == 0
    assert world.playlist() == new
    assert world.state()["quota_until"] is None


def test_old_set_videos_are_deleted_the_day_after_the_swap(world):
    old = week_two(world)
    world.run()
    world.clock.set("2026-11-12T03:00:00-08:00")
    world.run()
    world.clock.set(FRI_SWAP)
    world.run()
    world.clock.set("2026-11-13T23:00:00-08:00")
    world.run()
    assert all(v in world.fake.videos for v in old)
    world.clock.set("2026-11-14T00:30:00-08:00")
    assert world.run() == 0
    assert not any(v in world.fake.videos for v in old)
    assert "20261106" not in world.state()["sets"]


def test_swap_prunes_local_and_s3_copies_of_other_sets(world):
    world.clock.set("2026-10-30T06:00:00-07:00")
    world.bring_live("20261030")
    world.clock.set("2026-11-04T12:00:00-08:00")
    (world.dir / "set-20261001").mkdir()                   # stray failed bake
    world.s3 |= {"set-20261001", "2026-06-20"}             # 2026-06-20: not a set; untouched
    world.register("20261106", swap_at="2026-11-06T06:00:00-08:00")
    world.run()
    world.clock.set("2026-11-06T06:00:00-08:00")
    world.cmds.clear()
    assert world.run() == 0

    assert sorted(p.name for p in world.dir.iterdir() if p.name.startswith("set-")) == ["set-20261106"]
    rms = sorted(c[4] for c in world.cmds if c[:3] == ["aws", "s3", "rm"])
    assert rms == ["s3://bucket/shows/set-20261001/", "s3://bucket/shows/set-20261030/"]
    assert world.s3 == {"set-20261106", "2026-06-20"}


def test_s3_prune_failure_alerts_but_the_swap_stands(world):
    world.clock.set("2026-10-30T06:00:00-07:00")
    world.bring_live("20261030")
    world.clock.set("2026-11-04T12:00:00-08:00")
    world.register("20261106", swap_at="2026-11-06T06:00:00-08:00")
    world.run()
    world.s3_fail = True
    world.clock.set("2026-11-06T06:00:00-08:00")
    assert world.run() == 0
    assert world.state()["sets"]["20261106"]["status"] == "live"
    assert len(msgs_with(world, "S3 prune")) == 1
    assert not (world.dir / "set-20261030").exists()


# --- alerts ---

def test_not_ready_thursday_evening_then_overdue_at_swap_time(world):
    old = week_two(world)
    world.fake.processing_result = "uploaded"             # stuck processing
    world.run()
    for t in ("2026-11-12T17:59:00-08:00",):
        world.clock.set(t)
        world.run()
    assert msgs_with(world, "not ready") == []
    world.clock.set("2026-11-12T18:00:00-08:00")
    world.run()
    world.clock.set("2026-11-12T19:00:00-08:00")
    world.run()
    assert msgs_with(world, "not ready") == ["radio station: set 20261113 not ready: 0/3 processed"]
    world.clock.set(FRI_SWAP)
    world.run()
    world.clock.set("2026-11-13T07:00:00-08:00")
    world.run()
    assert len(msgs_with(world, "swap overdue")) == 1
    assert world.playlist() == old                        # last week's keeps playing


def test_launch_set_gets_three_hours_before_overdue_alert(world):
    world.clock.set("2026-11-05T14:00:00-08:00")
    world.fake.processing_result = "uploaded"
    world.register("20261105", swap_at="2026-11-05T14:00:00-08:00")
    for _ in range(3):
        world.run()
        world.clock.advance(hours=1)
    assert msgs_with(world, "not ready") == [] and msgs_with(world, "overdue") == []
    world.run()                                           # 17:00
    assert len(msgs_with(world, "swap overdue")) == 1
    assert msgs_with(world, "not ready: ") == []


def test_daily_health_alerts_once_and_announces_recovery(world):
    world.clock.set("2026-11-06T06:00:00-08:00")
    ids = world.bring_live("20261106", n=3)
    assert msgs_with(world, "health") == []
    pid = world.state()["playlist_id"]
    removed = world.fake.items.pop()                      # someone removed the last VOD
    world.fake.videos[ids[0]]["privacyStatus"] = "public"

    world.clock.set("2026-11-07T01:00:00-08:00")
    world.run()
    world.clock.set("2026-11-07T02:00:00-08:00")
    world.run()
    failed = msgs_with(world, "health check failed")
    assert len(failed) == 1 and "1 missing" in failed[0] and "processed/public" in failed[0]

    world.clock.set("2026-11-08T01:00:00-08:00")
    world.run()                                           # still broken: no repeat
    assert len(msgs_with(world, "health")) == 1

    world.fake.add_item(pid, removed["videoId"])
    world.fake.videos[ids[0]]["privacyStatus"] = "unlisted"
    world.clock.set("2026-11-09T01:00:00-08:00")
    world.run()
    world.clock.set("2026-11-10T01:00:00-08:00")
    world.run()
    assert len(msgs_with(world, "health OK again")) == 1
    assert world.playlist() == ids


def test_health_check_waits_for_playlist_reads_to_settle_after_a_swap(world):
    """playlistItems.list lags fresh inserts (seen on real YouTube); checking in
    the swap's own run reported VODs missing that were actually there."""
    world.fake.list_lag = True
    world.clock.set("2026-11-06T06:00:00-08:00")
    world.bring_live("20261106", n=3)
    assert msgs_with(world, "health") == []
    world.fake.list_lag = False
    world.clock.set("2026-11-06T07:00:00-08:00")
    world.run()
    assert msgs_with(world, "health") == []
    assert world.state()["last_health"] == "2026-11-06"


def test_every_processed_vod_gets_the_thumbnail_once(world):
    world.register("20261113", n=3, swap_at=FRI_SWAP)
    world.run()                                           # uploads; nothing processed yet
    assert world.fake.thumbnails == {}
    world.clock.set("2026-11-12T03:00:00-08:00")
    world.run()
    ids = uploaded(world, "20261113")
    assert set(world.fake.thumbnails) == set(ids)
    assert all(t == ("image/jpeg", world.thumb.read_bytes()) for t in world.fake.thumbnails.values())
    world.clock.set("2026-11-12T04:00:00-08:00")
    world.run()
    assert world.fake.count("POST", "thumbnail") == 3     # never re-set


def test_rejected_thumbnail_alerts_once_and_is_not_retried(world):
    world.fake.fail[("POST", "thumbnail")] = (403, "forbidden")
    world.register("20261113", n=2, swap_at=FRI_SWAP)
    world.run()
    world.clock.set("2026-11-12T03:00:00-08:00")
    assert world.run() == 0
    world.clock.set("2026-11-12T04:00:00-08:00")
    world.run()
    assert world.fake.count("POST", "thumbnail") == 2
    assert len(msgs_with(world, "thumbnail failed")) == 1


def test_unexpected_error_alerts_once_until_a_clean_run(world):
    world.fake.fail[("POST", "playlists")] = (500, "backendError")
    assert world.run() == 1
    assert world.run() == 1
    assert len(msgs_with(world, "run failed")) == 1
    del world.fake.fail[("POST", "playlists")]
    assert world.run() == 0
    assert world.run() == 0
    assert len(msgs_with(world, "recovered")) == 1
    assert not any(k.startswith("error:") for k in world.state()["alerts"])
