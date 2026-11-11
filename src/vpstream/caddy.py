"""Caddy, the public server: its configuration, running it, and reading its access log.

Caddy listens on the stream port only while a stream runs. It hands every request to the
daemon's gate over a unix socket. The gate answers either with the page itself (an index, a
playlist, a refusal) or with the number of a video, which Caddy then sends, with range
requests, from a folder that holds one link per video. A number needs no encoding, which
Caddy versions do differently for a path. Nothing about a stream is in Caddy's
configuration, and a change of alias, policy or device applies on the next request without
a reload.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

from vpstream.config import Paths
from vpstream.files import write_atomic
from vpstream.net import normal_ip
from vpstream.tools import Runner, require

# Set by Caddy on the way in, whatever the viewer sent: the address the request came from.
HEADER_IP = "X-Vpstream-Ip"
# Set by the gate on the way back: send the video with this number, with this type.
HEADER_FILE = "X-Vpstream-File"
HEADER_TYPE = "X-Vpstream-Type"

SERVER = "vpstream"
START_SECONDS = 10.0
STOP_SECONDS = 5.0
_SECOND = 1_000_000_000  # Caddy's JSON takes durations in nanoseconds


class CaddyError(RuntimeError):
    pass


def link_videos(folder: Path, videos: Sequence[str]) -> None:
    """Fill `folder` with one link per video, named 1, 2, 3 and so on. What was there goes."""
    unlink_videos(folder)
    folder.mkdir(mode=0o750, parents=True)
    for number, video in enumerate(videos, 1):
        (folder / str(number)).symlink_to(video)


def unlink_videos(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def config(port: int, gate_socket: Path, access_log: Path, videos_dir: Path) -> dict[str, Any]:
    """Caddy's whole configuration, as its native JSON."""
    send_file = [
        {
            "handler": "headers",
            "response": {"set": {"Content-Type": [f"{{http.reverse_proxy.header.{HEADER_TYPE}}}"]}},
        },
        {"handler": "rewrite", "uri": f"/{{http.reverse_proxy.header.{HEADER_FILE}}}"},
        {"handler": "file_server", "root": str(videos_dir)},
    ]
    ask_gate = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"unix/{gate_socket}"}],
        "headers": {
            "request": {"set": {HEADER_IP: ["{http.request.remote.host}"]}},
            "response": {"delete": ["Server"]},  # what the daemon is built with is its own
        },
        "handle_response": [
            {"match": {"headers": {HEADER_FILE: ["*"]}}, "routes": [{"handle": send_file}]}
        ],
    }
    refuse_method = {
        "match": [{"not": [{"method": ["GET", "HEAD"]}]}],
        "handle": [{"handler": "static_response", "status_code": 405}],
        "terminal": True,
    }
    access = f"http.log.access.{SERVER}"
    return {
        # No admin endpoint: nothing changes this configuration while Caddy runs.
        "admin": {"disabled": True, "config": {"persist": False}},
        "logging": {
            "logs": {
                "default": {"exclude": [access]},
                "access": {
                    "include": [access],
                    "encoder": {"format": "json"},
                    "writer": {
                        "output": "file",
                        "filename": str(access_log),
                        "roll_size_mb": 10,
                        "roll_keep": 1,
                    },
                },
            }
        },
        "apps": {
            "http": {
                "servers": {
                    SERVER: {
                        "listen": [f":{port}"],
                        "automatic_https": {"disable": True},
                        "read_header_timeout": 10 * _SECOND,
                        "idle_timeout": 120 * _SECOND,
                        "logs": {"default_logger_name": SERVER},
                        "routes": [refuse_method, {"handle": [ask_gate]}],
                    }
                }
            }
        },
    }


# --- the access log


def log_entry(line: str) -> tuple[str, str, int] | None:
    """The viewer's address and port, and the bytes sent, from one line of the access log.
    Caddy 2.6.2 logs about 512 bytes for a transfer the viewer cut off (see `traffic`)."""
    try:
        data = json.loads(line)
        request = data["request"]
        host, _, port = str(request.get("remote_addr") or "").rpartition(":")
        ip = normal_ip(request.get("remote_ip") or host.strip("[]"))
        port = str(request.get("remote_port") or port)
        size = data["size"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if ip is None or isinstance(size, bool) or not isinstance(size, int):
        return None
    return ip, port, size


class LogTail:
    """Reads what was added to the access log since the last look, across its rotation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._inode = 0
        self._offset = 0
        self._rest = b""

    def read(self) -> Iterator[tuple[str, str, int]]:
        try:
            with self.path.open("rb") as fh:
                st = os.fstat(fh.fileno())
                if st.st_ino != self._inode or st.st_size < self._offset:
                    self._inode, self._offset, self._rest = st.st_ino, 0, b""
                fh.seek(self._offset)
                data = fh.read()
        except OSError:
            return
        self._offset += len(data)
        *lines, self._rest = (self._rest + data).split(b"\n")  # the last line may be half
        for line in lines:
            entry = log_entry(line.decode("utf-8", "replace"))
            if entry:
                yield entry


# --- running it


class Server(Protocol):
    """The public server as the daemon uses it. Tests swap in a fake."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def running(self) -> bool: ...


def _accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


class CaddyServer:
    """Runs `caddy run` as a child of the daemon, so it lives and dies with the service."""

    def __init__(self, runner: Runner, port: int, paths: Paths) -> None:
        self.runner = runner
        self.port = port
        self.gate_socket = paths.gate_socket
        self.work_dir = paths.caddy_dir  # Caddy's own files and its error output
        self.access_log = paths.access_log
        self.videos_dir = paths.videos_dir
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def error_log(self) -> Path:
        return self.work_dir / "caddy.log"

    def _why(self) -> str:
        try:
            text = self.error_log.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        return text[-600:]

    def start(self) -> None:
        if self.running():
            return
        binary = require(self.runner, "caddy", "serving streams")
        if _accepts(self.port):
            raise CaddyError(f"port {self.port} is already used by another program")
        self.work_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.access_log.unlink(missing_ok=True)
        config_file = self.work_dir / "caddy.json"
        write_atomic(
            config_file,
            json.dumps(
                config(self.port, self.gate_socket, self.access_log, self.videos_dir), indent=1
            ),
        )
        # The service user has no home folder; Caddy keeps its files where it is told to.
        env = {
            **os.environ,
            "XDG_DATA_HOME": str(self.work_dir / "data"),
            "XDG_CONFIG_HOME": str(self.work_dir / "config"),
        }
        with self.error_log.open("wb") as errors:
            proc = subprocess.Popen(
                [binary, "run", "--config", str(config_file)],
                stdin=subprocess.DEVNULL,
                stdout=errors,
                stderr=errors,
                env=env,
                cwd=str(self.work_dir),
            )
        deadline = time.monotonic() + START_SECONDS
        while proc.poll() is None and time.monotonic() < deadline:
            if _accepts(self.port):
                self._proc = proc
                return
            time.sleep(0.1)
        if proc.poll() is None:
            _end(proc)
            raise CaddyError(f"Caddy did not listen on port {self.port}: {self._why()}")
        raise CaddyError(f"Caddy stopped at once (exit {proc.returncode}): {self._why()}")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            _end(proc)
        self.access_log.unlink(missing_ok=True)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


def _end(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(STOP_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
