"""HTTP server for the capability test page. Standard library only.

Every route is fixed: page assets come from the package, media only from the generated clip
folder, and nothing else on disk can be read.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

MAX_SPEED_MB = 256
MAX_REPORT_BYTES = 256 * 1024
CHUNK = 1024 * 1024

ASSETS = {
    "/caps": ("caps.html", "text/html; charset=utf-8"),
    "/caps/": ("caps.html", "text/html; charset=utf-8"),
    "/caps/caps.js": ("caps.js", "text/javascript; charset=utf-8"),
    "/caps/hls.min.js": ("hls.min.js", "text/javascript; charset=utf-8"),
}

MIME = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".mp4": "video/mp4",
    ".m4s": "video/iso.segment",
    ".ts": "video/mp2t",
    ".vtt": "text/vtt; charset=utf-8",
    ".json": "application/json",
}

_MEDIA_PATH = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9]+)?(/[A-Za-z0-9_-]+\.[A-Za-z0-9]+)?$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | str | None:
    """Parse a single `bytes=` range. Returns (start, end) inclusive, None for no range,
    or "invalid" when the range cannot be satisfied."""
    if not header:
        return None
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if not m or (not m.group(1) and not m.group(2)):
        return None  # unsupported form (for example several ranges): send the whole file
    if size == 0:
        return "invalid"
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:  # suffix range: the last N bytes
        n = int(m.group(2))
        if n == 0:
            return "invalid"
        start, end = max(0, size - n), size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return "invalid"
    return start, end


def media_file(root: Path, rel: str) -> Path | None:
    """Resolve a media path inside `root`, or None if it is not allowed."""
    if not _MEDIA_PATH.match(rel):
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def summarize(report: dict[str, Any]) -> list[str]:
    """Short lines for the terminal from one report sent by the page."""
    lines: list[str] = []
    for test in report.get("playback", []) or []:
        lines.append(
            f"  {test.get('name', '?'):<28} {test.get('result', '?')}"
            + (f"  ({test['detail']})" if test.get("detail") else "")
        )
    for key in ("subtitles", "audio", "speed", "keys"):
        val = report.get(key)
        if val:
            lines.append(f"  {key:<28} {json.dumps(val)[:200]}")
    return lines


@dataclass
class CapsState:
    clip_dir: Path
    results_path: Path
    log: Callable[[str], None] = print
    reports: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    block: bytes = b""

    def record(self, client: str, user_agent: str, report: dict[str, Any]) -> None:
        sid = str(report.get("session", "unknown"))[:64]
        with self.lock:
            entry = self.reports.setdefault(sid, {"client": client, "user_agent": user_agent})
            entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            entry.update({k: v for k, v in report.items() if k != "session"})
            self.results_path.write_text(json.dumps(self.reports, indent=2) + "\n")
        stage = report.get("stage", "")
        self.log(f"report from {client} ({stage}), saved to {self.results_path}")
        for line in summarize(report):
            self.log(line)


def make_handler(state: CapsState) -> type[BaseHTTPRequestHandler]:
    if not state.block:
        # Random-looking bytes so nothing on the path can compress the speed test.
        import os

        state.block = os.urandom(CHUNK)

    class Handler(BaseHTTPRequestHandler):
        server_version = "vpstream-caps"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass  # quiet; reports are logged instead

        def _send_bytes(
            self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _not_found(self) -> None:
            self._send_bytes(404, b"not found\n", "text/plain; charset=utf-8")

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            if path == "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/caps")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ASSETS:
                name, ctype = ASSETS[path]
                body = resources.files("vpstream.assets").joinpath(name).read_bytes()
                self._send_bytes(200, body, ctype)
                return
            if path == "/caps/clips.json":
                manifest = state.clip_dir / "clips.json"
                self._send_bytes(200, manifest.read_bytes(), MIME[".json"])
                return
            if path == "/caps/speed":
                self._speed(url.query)
                return
            if path.startswith("/caps/media/"):
                f = media_file(state.clip_dir, path[len("/caps/media/") :])
                if f is None:
                    self._not_found()
                    return
                self._file(f)
                return
            self._not_found()

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/caps/report":
                self._not_found()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_REPORT_BYTES:
                self._send_bytes(413, b"too large\n", "text/plain; charset=utf-8")
                return
            raw = self.rfile.read(length)
            try:
                report = json.loads(raw)
                if not isinstance(report, dict):
                    raise ValueError
            except ValueError:
                self._send_bytes(400, b"bad json\n", "text/plain; charset=utf-8")
                return
            state.record(self.client_address[0], self.headers.get("User-Agent", ""), report)
            self._send_bytes(200, b'{"ok":true}', MIME[".json"])

        def _speed(self, query: str) -> None:
            try:
                mb = int(parse_qs(query).get("mb", ["8"])[0])
            except ValueError:
                mb = 8
            mb = max(1, min(mb, MAX_SPEED_MB))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(mb * CHUNK))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                for _ in range(mb):
                    self.wfile.write(state.block)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _file(self, f: Path) -> None:
            size = f.stat().st_size
            ctype = MIME.get(f.suffix.lower(), "application/octet-stream")
            rng = parse_range(self.headers.get("Range"), size)
            if rng == "invalid":
                self._send_bytes(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
                return
            start, end = (0, size - 1) if rng is None or isinstance(rng, str) else rng
            length = end - start + 1 if size else 0
            self.send_response(206 if rng is not None else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if rng is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD" or not length:
                return
            try:
                with f.open("rb") as fh:
                    fh.seek(start)
                    left = length
                    while left > 0:
                        chunk = fh.read(min(CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def serve(state: CapsState, host: str, port: int, ttl_seconds: float | None) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    timer: threading.Timer | None = None
    if ttl_seconds:

        def stop() -> None:
            state.log(f"time limit of {ttl_seconds / 60:g} minutes reached, stopping")
            httpd.shutdown()

        timer = threading.Timer(ttl_seconds, stop)
        timer.daemon = True
        timer.start()
    try:
        httpd.serve_forever()
    finally:
        if timer:
            timer.cancel()
        httpd.server_close()
