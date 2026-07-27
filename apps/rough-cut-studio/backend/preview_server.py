"""
preview_server.py

A minimal local HTTP server that streams linked media files to the video
preview player on the Cuts tab. This exists because pywebview's support
for loading local file:// URLs directly into <video> tags is inconsistent
across platforms (WKWebView on macOS, WebView2 on Windows, and WebKitGTK
on Linux all handle it slightly differently), while every one of them
happily plays a plain http:// URL. Serving over HTTP also gets us
byte-range support "for free" via a small custom handler below, which is
what lets the browser's video scrubber actually seek instead of just
playing from the start.

Security notes:
  * Binds ONLY to 127.0.0.1 (loopback) -- never reachable from the network,
    even on the same LAN.
  * Serves exactly the files this app has already linked as source media
    (registered explicitly via `register`), addressed by an opaque random
    token -- never by raw path, so there's no directory traversal surface
    and no way to request an arbitrary file by guessing a path.
  * Read-only: the handler only implements GET, and only ever opens files
    for reading.
  * Started lazily (only when a preview is actually requested), and runs
    as a daemon thread that exits automatically when the app closes.
  * `Access-Control-Allow-Origin: *` IS sent (revised from this server's
    original design, which assumed every consumer was a plain <video src>
    same-origin-equivalent to the webview page). Colorize's WebGL preview
    pipeline (colorize.js) draws the <video> element into a canvas via
    texImage2D every frame -- the WHATWG/WebGL spec taints that canvas
    (and texImage2D throws SecurityError) for ANY cross-origin media
    lacking a CORS-allow response, and this server's random port is
    always a different origin than pywebview's own bottle server hosting
    the page. The wildcard is still safe here: this is a loopback-only,
    read-only, token-gated server (see above) -- CORS only governs
    whether a PAGE may read the response bytes/pixels back into JS, and
    the only page that can ever reach 127.0.0.1's random port at all is
    one already running on this same machine, which could read the file
    directly off disk anyway. It grants no new access, only permission to
    do something (read pixels off a video it's already receiving) that
    was already trivially available another way.
"""

import os
import uuid
import threading
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _RangeRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _resolve_path(self):
        parts = self.path.lstrip("/").split("/")
        if len(parts) != 2 or parts[0] != "media":
            return None
        token = parts[1]
        return self.server.token_map.get(token)

    def do_GET(self):
        path = self._resolve_path()
        if not path or not os.path.exists(path):
            self.send_error(404, "Unknown or missing media")
            return

        file_size = os.path.getsize(path)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        range_header = self.headers.get("Range")

        start, end = 0, file_size - 1
        status = 200
        if range_header and range_header.startswith("bytes="):
            status = 206
            try:
                spec = range_header.split("=", 1)[1]
                start_str, end_str = (spec.split("-") + [""])[:2]
                if start_str == "" and end_str != "":
                    # Suffix range ("bytes=-500" means "last 500 bytes").
                    suffix_len = int(end_str)
                    start = max(0, file_size - suffix_len)
                    end = file_size - 1
                else:
                    start = int(start_str) if start_str else 0
                    end = int(end_str) if end_str else file_size - 1
                end = min(end, file_size - 1)
                if start > end:
                    start, end = 0, file_size - 1
                    status = 200
            except (ValueError, IndexError):
                start, end = 0, file_size - 1
                status = 200

        length = end - start + 1
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Access-Control-Allow-Origin", "*")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
            self.end_headers()

            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk_size = 256 * 1024
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player paused/seeked mid-stream; nothing to do

    def log_message(self, fmt, *args):
        pass  # keep stdout/stderr quiet; this is a background convenience server


class PreviewServer:
    def __init__(self):
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()
        self.token_map = {}
        self._path_to_token = {}

    def _ensure_started(self):
        with self._lock:
            if self._httpd is not None:
                return
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RangeRequestHandler)
            self._httpd.token_map = self.token_map
            self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
            self._thread.start()

    @property
    def port(self) -> int:
        self._ensure_started()
        return self._httpd.server_address[1]

    def url_for(self, path: str) -> str:
        """Registers (or reuses) a token for `path` and returns its preview URL."""
        abspath = os.path.abspath(path)
        token = self._path_to_token.get(abspath)
        if not token:
            token = uuid.uuid4().hex
            self._path_to_token[abspath] = token
            self.token_map[token] = abspath
        return f"http://127.0.0.1:{self.port}/media/{token}"

    def forget(self, path: str):
        """Drops the token registered for `path`, if any, so it's no longer
        servable. Called when a source is removed from the project --
        otherwise a removed source's media stays reachable via its old
        token for the rest of the process's life."""
        abspath = os.path.abspath(path)
        token = self._path_to_token.pop(abspath, None)
        if token:
            self.token_map.pop(token, None)

    def shutdown(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
