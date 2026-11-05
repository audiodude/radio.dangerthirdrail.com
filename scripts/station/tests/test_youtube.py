import pytest

import youtube
from fake_youtube import FakeYouTube


def client(fake):
    return youtube.YouTube("cid", "secret", "refresh", transport=fake,
                           chunk_size=256 * 1024, clock=lambda: 0.0)


def test_playlist_items_come_back_in_order_across_pages():
    fake = FakeYouTube()
    yt = client(fake)
    pid = yt.create_playlist("t", "d")
    vids = [fake.add_video() for _ in range(8)]
    for v in vids:
        yt.add_playlist_item(pid, v)
    assert [vid for _, vid in yt.playlist_items(pid)] == vids
    assert fake.count("GET", "playlistItems") == 3        # fake pages by 3


def test_video_status_batches_and_omits_deleted_videos():
    fake = FakeYouTube()
    yt = client(fake)
    vids = [fake.add_video() for _ in range(120)]
    fake.videos[vids[7]]["uploadStatus"] = "rejected"
    del fake.videos[vids[99]]
    st = yt.video_status(vids)
    assert len(st) == 119 and vids[99] not in st
    assert st[vids[7]] == {"uploadStatus": "rejected", "privacyStatus": "unlisted"}
    assert fake.count("GET", "videos") == 3


@pytest.mark.parametrize("status, reason", [(403, "quotaExceeded"), (404, "videoNotFound")])
def test_api_errors_carry_status_and_reason(status, reason):
    fake = FakeYouTube()
    fake.fail[("DELETE", "videos")] = (status, reason)
    with pytest.raises(youtube.YouTubeError) as e:
        client(fake).delete_video("nope")
    assert (e.value.status, e.value.reason) == (status, reason)


def test_missing_credentials_is_an_auth_error():
    yt = youtube.YouTube("", "", "", transport=FakeYouTube())
    with pytest.raises(youtube.AuthError):
        yt.video_status(["a"])


def test_upload_can_be_finished_from_a_session_url_in_a_new_client(tmp_path):
    fake = FakeYouTube()
    path = tmp_path / "v.mp4"
    data = bytes(range(256)) * 3000                        # ~750 KiB: 3 chunks
    path.write_bytes(data)
    url = client(fake).start_upload(str(path), "T", "D")
    assert fake.chunks == []                               # nothing sent before the caller saves the URL
    video = client(fake).finish_upload(url, str(path))
    assert fake.videos[video["id"]]["data"] == data
    again = client(fake).finish_upload(url, str(path))     # finished session: same video
    assert again["id"] == video["id"] and len(fake.videos) == 1


def test_gone_session_raises_session_gone(tmp_path):
    fake = FakeYouTube()
    path = tmp_path / "v.mp4"
    path.write_bytes(b"x" * 1000)
    yt = client(fake)
    url = yt.start_upload(str(path), "T", "D")
    fake.sessions[url]["gone"] = True
    with pytest.raises(youtube.SessionGone):
        yt.finish_upload(url, str(path))
