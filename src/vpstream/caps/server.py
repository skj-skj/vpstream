"""HTTP server for the capability test page. Standard library only.

Every route is fixed: page assets come from the package, media only from the generated clip
folder, and nothing else on disk can be read.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
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
# One page load is one session, so this is plenty for a test and bounds memory and disk.
MAX_SESSIONS = 50
CHUNK = 1024 * 1024

TEXT = "text/plain; charset=utf-8"
JSON = "application/json"

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
    ".json": JSON,
}

# Fields the server sets itself; a report cannot overwrite them.
SERVER_FIELDS = frozenset({"client", "user_agent", "updated", "session"})

_MEDIA_PATH = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9]+)?(/[A-Za-z0-9_-]+\.[A-Za-z0-9]+)?$")
_RANGE = re.compile(r"\s*bytes=(\d*)-(\d*)\s*")


class RangeNotSatisfiable(ValueError):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single `bytes=` range into (start, end) inclusive.

    Returns None when the whole file should be sent: no header, a form this server does not
    support (several ranges) or a malformed one, which RFC 9110 says to ignore. Raises
    RangeNotSatisfiable when the range is valid but lies outside the file.
    """
    m = _RANGE.fullmatch(header or "")
    if not m or not (m.group(1) or m.group(2)):
        return None
    first, last = m.group(1), m.group(2)
    if first and last and int(last) < int(first):
        return None  # malformed: ignored
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:  # suffix range: the last N bytes
        n = int(last)
        start, end = max(0, size - n), size - 1
        if n == 0:
            raise RangeNotSatisfiable
    if start >= size:
        raise RangeNotSatisfiable
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


def _short(value: Any, width: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return text[:width]


def summarize(report: dict[str, Any]) -> list[str]:
    """Short lines for the terminal from one report sent by the page.

    The report comes from the network, so any shape is accepted.
    """
    lines: list[str] = []
    playback = report.get("playback")
    for test in playback if isinstance(playback, list) else []:
        if not isinstance(test, dict):
            continue
        name, result = _short(test.get("name", "?"), 28), _short(test.get("result", "?"), 20)
        detail = f"  ({_short(test['detail'])})" if test.get("detail") else ""
        lines.append(f"  {name:<28} {result}{detail}")
    for key in ("subtitles", "audio", "speed", "keys"):
        val = report.get(key)
        if val:
            lines.append(f"  {key:<28} {_short(val)}")
    return lines


class TooManySessions(RuntimeError):
    pass


@dataclass
class CapsState:
    clip_dir: Path
    results_path: Path
    log: Callable[[str], None] = print
    reports: dict[str, Any] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    block: bytes = b""

    def record(self, client: str, user_agent: str, report: dict[str, Any]) -> None:
        """Merge one report into its session and save every session to the results file.

        Raises TooManySessions for a new session past MAX_SESSIONS, and OSError when the
        results file cannot be written.
        """
        sid = str(report.get("session", "unknown"))[:64]
        with self.lock:
            if sid not in self.reports and len(self.reports) >= MAX_SESSIONS:
                raise TooManySessions(sid)
            entry = self.reports.setdefault(sid, {})
            entry.update({k: v for k, v in report.items() if k not in SERVER_FIELDS})
            entry.update(
                client=client,
                user_agent=user_agent,
                updated=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            )
            write_atomic(self.results_path, json.dumps(self.reports, indent=2) + "\n")
        stage = _short(report.get("stage", ""), 40)
        self.log(f"report from {client} ({stage}), saved to {self.results_path}")
        for line in summarize(report):
            self.log(line)


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def load_assets() -> dict[str, tuple[bytes, str]]:
    files = resources.files("vpstream.assets")
    return {
        route: (files.joinpath(name).read_bytes(), ctype) for route, (name, ctype) in ASSETS.items()
    }


def make_handler(state: CapsState) -> type[BaseHTTPRequestHandler]:
    if not state.block:
        # Random bytes so nothing on the path can compress the speed test.
        state.block = os.urandom(CHUNK)
    assets = load_assets()

    class Handler(BaseHTTPRequestHandler):
        server_version = "vpstream-caps"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass  # quiet; reports are logged instead

        def _send_head(
            self, status: int, ctype: str, length: int, extra: dict[str, str] | None = None
        ) -> bool:
            """Send the status and headers. Returns True when a body should follow."""
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            return self.command != "HEAD" and length > 0

        def _send_bytes(
            self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None
        ) -> None:
            if self._send_head(status, ctype, len(body), extra):
                self.wfile.write(body)

        def _error(self, status: int, message: str, *, close: bool = False) -> None:
            # A request body that was not read would be taken as the next request.
            self.close_connection = self.close_connection or close
            self._send_bytes(status, f"{message}\n".encode(), TEXT)

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path = url.path
            if path == "/":
                self._send_bytes(HTTPStatus.FOUND, b"", TEXT, {"Location": "/caps"})
            elif path in assets:
                body, ctype = assets[path]
                self._send_bytes(200, body, ctype)
            elif path == "/caps/clips.json":
                self._send_file(state.clip_dir / "clips.json")
            elif path == "/caps/speed":
                self._speed(url.query)
            elif path.startswith("/caps/media/"):
                f = media_file(state.clip_dir, path[len("/caps/media/") :])
                if f is None:
                    self._error(404, "not found")
                else:
                    self._send_file(f)
            else:
                self._error(404, "not found")

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/caps/report":
                self._error(404, "not found", close=True)
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._error(411, "Content-Length required", close=True)
                return
            if length <= 0:
                self._error(400, "empty report", close=True)
                return
            if length > MAX_REPORT_BYTES:
                self._error(413, "too large", close=True)
                return
            try:
                report = json.loads(self.rfile.read(length))
                if not isinstance(report, dict):
                    raise ValueError
            except ValueError:
                self._error(400, "bad json")
                return
            client = self.client_address[0]
            try:
                state.record(client, self.headers.get("User-Agent", ""), report)
            except TooManySessions:
                self._error(429, "too many sessions")
                return
            except OSError as exc:
                state.log(f"cannot save the report from {client}: {exc}")
                self._error(500, "could not save the report")
                return
            self._send_bytes(200, b'{"ok":true}', JSON)

        def _speed(self, query: str) -> None:
            try:
                mb = int(parse_qs(query).get("mb", ["8"])[0])
            except ValueError:
                mb = 8
            mb = max(1, min(mb, MAX_SPEED_MB))
            if not self._send_head(200, "application/octet-stream", mb * CHUNK):
                return
            try:
                for _ in range(mb):
                    self.wfile.write(state.block)
            except ConnectionError:
                pass

        def _send_file(self, f: Path) -> None:
            try:
                size = f.stat().st_size
            except OSError:
                self._error(404, "not found")
                return
            ctype = MIME.get(f.suffix.lower(), "application/octet-stream")
            ranges = {"Accept-Ranges": "bytes"}
            try:
                rng = parse_range(self.headers.get("Range"), size)
            except RangeNotSatisfiable:
                self._send_bytes(416, b"", ctype, {**ranges, "Content-Range": f"bytes */{size}"})
                return
            if rng is None:
                start, end, status = 0, size - 1, 200
            else:
                start, end, status = *rng, 206
                ranges["Content-Range"] = f"bytes {start}-{end}/{size}"
            length = end - start + 1
            if not self._send_head(status, ctype, length, ranges):
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
            except ConnectionError:
                pass

    return Handler


class CapsServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A browser that drops a connection is normal; only report real errors.
        if isinstance(sys.exception(), ConnectionError | TimeoutError):
            return
        super().handle_error(request, client_address)


class CapsServer6(CapsServer):
    """IPv6, and IPv4 too where the host allows one socket for both."""

    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        with contextlib.suppress(AttributeError, OSError):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_server(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> CapsServer:
    cls = CapsServer6 if ":" in host else CapsServer
    return cls((host, port), handler)


def serve(state: CapsState, host: str, port: int, ttl_seconds: float | None) -> None:
    httpd = make_server(host, port, make_handler(state))
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
