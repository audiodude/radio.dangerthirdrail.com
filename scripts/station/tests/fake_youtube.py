"""An in-memory YouTube speaking the HTTP protocol youtube.YouTube uses: OAuth
token, videos (list/delete), playlists (insert), playlistItems
(list/insert/delete), and resumable uploads (init + chunked PUT + status query).
Plug it in as the client's transport."""
import json
import urllib.parse

UNIT = 256 * 1024
SESSION_PREFIX = "https://upload.fake.test/session/"


class Crash(BaseException):
    """Simulates the process dying mid-request (not caught by `except Exception`)."""


def _resp(status, body=None, headers=None):
    data = b"" if body is None else json.dumps(body).encode()
    return status, dict(headers or {}), data


def _err(status, reason):
    return _resp(status, {"error": {"code": status, "message": reason,
                                    "errors": [{"reason": reason}]}})


class FakeYouTube:
    def __init__(self):
        self.videos = {}        # id -> {"uploadStatus", "privacyStatus", "title", "description", "data"}
        self.sessions = {}      # url -> {"total", "meta", "data", "video_id", "gone"}
        self.playlists = {}     # id -> body
        self.items = []         # [{"id", "playlistId", "videoId"}] in position order
        self.calls = []         # (METHOD, endpoint)
        self.chunks = []        # (session_url, first_byte, last_byte) of every data PUT
        self.uploads_left = None        # upload inits allowed before uploadLimitExceeded
        self.quota_budget = None        # API calls left before quotaExceeded (None = unlimited)
        self.processing_result = "processed"
        self.fail = {}                  # (METHOD, endpoint) -> (status, reason)
        self.crash_on_chunk = None      # crash after storing the Nth data PUT (1-based)
        self.crash_before = None        # (METHOD, endpoint, n): crash instead of the nth such call
        self.page_size = 3
        self.list_lag = False           # listings omit the newest item (YouTube's read lag)
        self.thumbnails = {}            # video id -> (content type, bytes)
        self._n = 0

    def _id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    # --- helpers for tests ---
    def playlist_video_ids(self, pid=None):
        return [it["videoId"] for it in self.items if pid is None or it["playlistId"] == pid]

    def count(self, method, endpoint):
        return sum(1 for c in self.calls if c == (method, endpoint))

    def add_video(self, status="processed", privacy="unlisted"):
        vid = self._id("x")
        self.videos[vid] = {"uploadStatus": status, "privacyStatus": privacy,
                            "title": "", "description": "", "data": b""}
        return vid

    def add_item(self, pid, vid):
        self.items.append({"id": self._id("item"), "playlistId": pid, "videoId": vid})

    # --- transport ---
    def __call__(self, method, url, headers, body=None):
        headers = {k.lower(): v for k, v in headers.items()}
        if url == "https://oauth2.googleapis.com/token":
            self.calls.append((method, "token"))
            return _resp(200, {"access_token": "tok", "expires_in": 3600})
        assert headers.get("authorization") == "Bearer tok"
        if url.startswith(SESSION_PREFIX):
            self.calls.append((method, "session"))
            return self._session(url, headers, body)
        parts = urllib.parse.urlsplit(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        if parts.path == "/upload/youtube/v3/videos":
            endpoint = "upload"
        elif parts.path == "/upload/youtube/v3/thumbnails/set":
            endpoint = "thumbnail"
        else:
            assert parts.path.startswith("/youtube/v3/"), url
            endpoint = parts.path[len("/youtube/v3/"):]
        self.calls.append((method, endpoint))
        if self.crash_before and self.crash_before[:2] == (method, endpoint) \
                and self.count(method, endpoint) == self.crash_before[2]:
            self.crash_before = None
            raise Crash(f"died before {method} {endpoint}")
        if (method, endpoint) in self.fail:
            return _err(*self.fail[(method, endpoint)])
        if self.quota_budget is not None:
            if self.quota_budget <= 0:
                return _err(403, "quotaExceeded")
            self.quota_budget -= 1
        if endpoint == "thumbnail":                      # raw image body, not JSON
            vid = q["videoId"]
            if vid not in self.videos:
                return _err(404, "videoNotFound")
            self.thumbnails[vid] = (headers["content-type"], body)
            return _resp(200, {"kind": "youtube#thumbnailSetResponse"})
        doc = json.loads(body) if body else None
        handler = getattr(self, f"_{method.lower()}_{endpoint}")
        return handler(q, headers, doc)

    def _post_upload(self, q, headers, doc):
        assert q["uploadType"] == "resumable" and q["notifySubscribers"] == "false"
        if self.uploads_left is not None:
            if self.uploads_left <= 0:
                return _err(400, "uploadLimitExceeded")
            self.uploads_left -= 1
        url = SESSION_PREFIX + self._id("s")
        self.sessions[url] = {"total": int(headers["x-upload-content-length"]), "meta": doc,
                              "data": bytearray(), "video_id": None, "gone": False}
        return _resp(200, headers={"location": url})

    def _session(self, url, headers, body):
        s = self.sessions.get(url)
        if s is None or s["gone"]:
            return _err(404, "notFound")
        total, have = s["total"], len(s["data"])
        unit, rng = headers["content-range"].split(" ")
        assert unit == "bytes"
        span, size = rng.split("/")
        assert int(size) == total
        if span == "*":
            assert not body
            if s["video_id"]:
                return self._video_resource(s["video_id"], 200)
            return _resp(308, headers={"range": f"bytes=0-{have - 1}"} if have else {})
        first, last = (int(x) for x in span.split("-"))
        assert first == have, f"client resent from {first}, server has {have}"
        assert len(body) == last - first + 1
        if last + 1 < total:
            assert len(body) % UNIT == 0, "non-final chunk must be a multiple of 256 KiB"
        s["data"] += body
        self.chunks.append((url, first, last))
        if self.crash_on_chunk is not None and len(self.chunks) == self.crash_on_chunk:
            self.crash_on_chunk = None
            raise Crash("connection lost")
        if len(s["data"]) < total:
            return _resp(308, headers={"range": f"bytes=0-{len(s['data']) - 1}"})
        vid = self._id("v")
        snip, status = s["meta"]["snippet"], s["meta"]["status"]
        self.videos[vid] = {"uploadStatus": self.processing_result,
                            "privacyStatus": status["privacyStatus"],
                            "title": snip["title"], "description": snip["description"],
                            "categoryId": snip["categoryId"],
                            "madeForKids": status["selfDeclaredMadeForKids"],
                            "data": bytes(s["data"])}
        s["video_id"] = vid
        return self._video_resource(vid, 201)

    def _video_resource(self, vid, code):
        return _resp(code, {"id": vid, "status": {"uploadStatus": "uploaded"}})

    def _get_videos(self, q, headers, doc):
        ids = q["id"].split(",")
        assert len(ids) <= 50
        return _resp(200, {"items": [
            {"id": i, "status": {"uploadStatus": self.videos[i]["uploadStatus"],
                                 "privacyStatus": self.videos[i]["privacyStatus"]}}
            for i in ids if i in self.videos]})

    def _delete_videos(self, q, headers, doc):
        if q["id"] not in self.videos:
            return _err(404, "videoNotFound")
        del self.videos[q["id"]]
        return _resp(204)

    def _post_playlists(self, q, headers, doc):
        pid = self._id("PL")
        self.playlists[pid] = doc
        return _resp(200, {"id": pid})

    def _get_playlistItems(self, q, headers, doc):
        mine = [it for it in self.items if it["playlistId"] == q["playlistId"]]
        if self.list_lag and mine:
            newest = max(mine, key=lambda it: int(it["id"][len("item"):]))
            mine = [it for it in mine if it is not newest]
        start = int(q.get("pageToken", 0))
        size = min(int(q.get("maxResults", 5)), self.page_size)
        page = mine[start:start + size]
        out = {"items": [{"id": it["id"], "snippet": {
            "position": start + n, "resourceId": {"kind": "youtube#video", "videoId": it["videoId"]}}}
            for n, it in enumerate(page)]}
        if start + size < len(mine):
            out["nextPageToken"] = str(start + size)
        return _resp(200, out)

    def _post_playlistItems(self, q, headers, doc):
        snip = doc["snippet"]
        vid = snip["resourceId"]["videoId"]
        if vid not in self.videos:
            return _err(404, "videoNotFound")
        item = {"id": self._id("item"), "playlistId": snip["playlistId"], "videoId": vid}
        self.items.append(item)
        return _resp(200, {"id": item["id"]})

    def _delete_playlistItems(self, q, headers, doc):
        for n, it in enumerate(self.items):
            if it["id"] == q["id"]:
                del self.items[n]
                return _resp(204)
        return _err(404, "playlistItemNotFound")
