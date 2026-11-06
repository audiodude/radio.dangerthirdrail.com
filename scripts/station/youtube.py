"""YouTube Data API v3 client for the station: OAuth refresh-token flow, JSON
calls that raise YouTubeError (with the API's `reason`), resumable chunked
uploads that can be resumed from a persisted session URL, and the playlist and
video operations the station job needs. Stdlib only.

The HTTP transport is injectable: `transport(method, url, headers, body)` →
`(status, headers_lowercased, body_bytes)`. The default uses http.client,
because urllib's redirect handling mishandles the upload protocol's 308."""
import http.client
import json
import os
import time
import urllib.parse

API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = ("https://www.googleapis.com/upload/youtube/v3/videos"
              "?uploadType=resumable&part=snippet,status&notifySubscribers=false")
THUMBNAIL_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
CHUNK_UNIT = 256 * 1024
DEFAULT_CHUNK = 64 * 1024 * 1024
MAX_STALLS = 3


class YouTubeError(Exception):
    """An HTTP error from Google. `reason` is error.errors[0].reason (e.g.
    quotaExceeded, uploadLimitExceeded, videoNotFound), or the OAuth `error`."""

    def __init__(self, status, reason, message=""):
        super().__init__(f"HTTP {status} {reason}: {message}".rstrip(": "))
        self.status = status
        self.reason = reason
        self.message = message


class SessionGone(YouTubeError):
    """The resumable upload session no longer exists; start a new one."""


class AuthError(YouTubeError):
    """No credentials, or the OAuth refresh failed."""


def http_transport(method, url, headers, body=None, timeout=300):
    parts = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.netloc, timeout=timeout)
    try:
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def _error(status, data, cls=YouTubeError):
    reason, message = "", ""
    try:
        doc = json.loads(data)
        err = doc.get("error")
        if isinstance(err, dict):
            message = err.get("message", "")
            errs = err.get("errors") or []
            reason = (errs[0].get("reason", "") if errs else "") or err.get("status", "")
        elif isinstance(err, str):                       # OAuth token endpoint
            reason, message = err, doc.get("error_description", "")
    except (ValueError, AttributeError):
        message = data[:200].decode("utf-8", "replace").strip()
    return cls(status, reason, message)


def _next_offset(headers):
    """Byte offset to send next, from a 308's `Range: bytes=0-N` (absent = 0)."""
    rng = headers.get("range")
    if not rng:
        return 0
    return int(rng.rsplit("-", 1)[1]) + 1


class YouTube:
    def __init__(self, client_id, client_secret, refresh_token,
                 transport=http_transport, chunk_size=DEFAULT_CHUNK, clock=time.time):
        if chunk_size <= 0 or chunk_size % CHUNK_UNIT:
            raise ValueError("chunk_size must be a positive multiple of 256 KiB")
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.transport = transport
        self.chunk_size = chunk_size
        self.clock = clock
        self._token = None
        self._token_expires = 0.0

    @classmethod
    def from_env(cls, **kw):
        return cls(os.environ.get("YOUTUBE_CLIENT_ID", ""),
                   os.environ.get("YOUTUBE_CLIENT_SECRET", ""),
                   os.environ.get("YOUTUBE_REFRESH_TOKEN", ""), **kw)

    # --- auth + transport ---

    def access_token(self):
        if self._token and self.clock() < self._token_expires - 60:
            return self._token
        if not (self.client_id and self.refresh_token):
            raise AuthError(0, "noCredentials",
                            "YOUTUBE_CLIENT_ID / YOUTUBE_REFRESH_TOKEN not set")
        form = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        status, _, data = self.transport(
            "POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, form)
        if status != 200:
            raise _error(status, data, AuthError)
        tokens = json.loads(data)
        self._token = tokens["access_token"]
        self._token_expires = self.clock() + tokens.get("expires_in", 3600)
        return self._token

    def _send(self, method, url, headers=None, body=None):
        """Authenticated request; refreshes the token and retries once on 401."""
        for attempt in (0, 1):
            h = {"Authorization": f"Bearer {self.access_token()}", **(headers or {})}
            status, rh, data = self.transport(method, url, h, body)
            if status == 401 and attempt == 0:
                self._token = None
                continue
            return status, rh, data

    def _api(self, method, path, params, body=None):
        url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
        headers, data = {}, None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        status, _, resp = self._send(method, url, headers, data)
        if status >= 400:
            raise _error(status, resp)
        return json.loads(resp) if resp.strip() else {}

    # --- uploads ---

    def start_upload(self, path, title, description):
        """Open a resumable upload session for `path`; returns the session URL.
        Persist it before calling finish_upload so a crash resumes."""
        size = os.path.getsize(path)
        if size == 0:
            raise ValueError(f"{path} is empty")
        meta = {
            "snippet": {"title": title, "description": description, "categoryId": "10"},
            "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False,
                       "embeddable": True, "license": "youtube"},
        }
        status, headers, data = self._send("POST", UPLOAD_URL, {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        }, json.dumps(meta).encode())
        if status != 200 or "location" not in headers:
            raise _error(status, data)
        return headers["location"]

    def _put(self, session_url, content_range, body):
        status, headers, data = self._send(
            "PUT", session_url, {"Content-Range": content_range}, body)
        if status in (200, 201):
            return json.loads(data), None
        if status == 308:
            return None, _next_offset(headers)
        if status in (404, 410):
            raise _error(status, data, SessionGone)
        raise _error(status, data)

    def finish_upload(self, session_url, path):
        """Send whatever the session still lacks; returns the video resource.
        Raises SessionGone if the session expired (open a new one)."""
        size = os.path.getsize(path)
        video, offset = self._put(session_url, f"bytes */{size}", b"")
        stalls = 0
        with open(path, "rb") as f:
            while video is None:
                f.seek(offset)
                chunk = f.read(min(self.chunk_size, size - offset))
                if not chunk:
                    raise YouTubeError(308, "uploadIncomplete",
                                       f"server has {offset}/{size} bytes but wants more")
                end = offset + len(chunk) - 1
                video, new_offset = self._put(session_url, f"bytes {offset}-{end}/{size}", chunk)
                if video is None:
                    stalls = stalls + 1 if new_offset <= offset else 0
                    if stalls >= MAX_STALLS:
                        raise YouTubeError(308, "uploadStalled", f"stuck at {offset}/{size} bytes")
                    offset = new_offset
        return video

    # --- videos ---

    def video_status(self, ids):
        """{id: {"uploadStatus", "privacyStatus"}}; ids YouTube doesn't return
        (deleted) are absent."""
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            r = self._api("GET", "videos",
                          {"part": "status", "id": ",".join(batch), "maxResults": 50})
            for item in r.get("items", []):
                st = item.get("status", {})
                out[item["id"]] = {"uploadStatus": st.get("uploadStatus", ""),
                                   "privacyStatus": st.get("privacyStatus", "")}
        return out

    def delete_video(self, video_id):
        self._api("DELETE", "videos", {"id": video_id})

    def set_thumbnail(self, video_id, path):
        """Custom thumbnail (JPEG/PNG, ≤ 2 MB, 16:9); 50 quota units."""
        with open(path, "rb") as f:
            image = f.read()
        ctype = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        status, _, data = self._send(
            "POST", f"{THUMBNAIL_URL}?{urllib.parse.urlencode({'videoId': video_id})}",
            {"Content-Type": ctype}, image)
        if status >= 400:
            raise _error(status, data)

    # --- playlists ---

    def create_playlist(self, title, description, privacy="public"):
        r = self._api("POST", "playlists", {"part": "snippet,status"}, {
            "snippet": {"title": title, "description": description},
            "status": {"privacyStatus": privacy},
        })
        return r["id"]

    def playlist_items(self, playlist_id):
        """[(item_id, video_id)] in playlist order."""
        items, token = [], None
        while True:
            params = {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}
            if token:
                params["pageToken"] = token
            r = self._api("GET", "playlistItems", params)
            items.extend(r.get("items", []))
            token = r.get("nextPageToken")
            if not token:
                break
        items.sort(key=lambda it: it.get("snippet", {}).get("position", 0))
        return [(it["id"], it["snippet"]["resourceId"]["videoId"]) for it in items]

    def add_playlist_item(self, playlist_id, video_id):
        """Append `video_id` to the end of the playlist; returns the item id."""
        r = self._api("POST", "playlistItems", {"part": "snippet"}, {
            "snippet": {"playlistId": playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id}},
        })
        return r["id"]

    def delete_playlist_item(self, item_id):
        self._api("DELETE", "playlistItems", {"id": item_id})
