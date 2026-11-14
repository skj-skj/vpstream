"""Caddy, the public server: its configuration, running it, and reading its access log.

Two Caddy processes can run, each a child of the daemon with a fixed configuration and no
admin endpoint. The stream's (`CaddyServer`) listens on the stream port only while a stream
runs. The public dashboard's (`DashboardCaddy`), only in public mode, serves the dashboard
over HTTPS with a certificate from its own local authority (`tls internal`).

The stream's Caddy hands every request to the daemon's gate over a unix socket. The gate
answers either with the page itself (an index, a playlist, a refusal) or with the number of
a video, which Caddy then sends, with range requests, from a folder that holds one link per
video. A number needs no encoding, which
Caddy versions do differently for a path. Nothing about a stream is in Caddy's
configuration, and a change of alias, policy or device applies on the next request without
a reload.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

from vpstream.checks import is_whole
from vpstream.config import Paths
from vpstream.files import write_atomic
from vpstream.net import normal_ip, url_host
from vpstream.tools import Runner, require

# Set by Caddy on the way in, whatever the viewer sent: the address the request came from.
HEADER_IP = "X-Vpstream-Ip"
# Set by the gate on the way back: send the video with this number, with this type.
HEADER_FILE = "X-Vpstream-File"
HEADER_TYPE = "X-Vpstream-Type"

SERVER = "vpstream"
DASHBOARD_SERVER = "dashboard"
START_SECONDS = 10.0
STOP_SECONDS = 5.0
_SECOND = 1_000_000_000  # Caddy's JSON takes durations in nanoseconds


class CaddyError(RuntimeError):
    pass


def link_videos(folder: Path, videos: Sequence[str | None]) -> None:
    """Fill `folder` with one link per video, named 1, 2, 3 and so on; None leaves a number
    without one for now. What was there goes."""
    unlink_videos(folder)
    folder.mkdir(mode=0o750, parents=True)
    for number, video in enumerate(videos, 1):
        if video is not None:
            (folder / str(number)).symlink_to(video)


def link_video(folder: Path, number: int, video: str) -> None:
    """Point one number at `video`, in one step: a request sees the old link or the new."""
    part = folder / f".{number}.new"
    part.unlink(missing_ok=True)
    part.symlink_to(video)
    os.replace(part, folder / str(number))


def unlink_videos(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)


_NO_ADMIN = {"disabled": True, "config": {"persist": False}}


def _proxy_to(socket_path: Path) -> dict[str, Any]:
    """Hand the request to the daemon on a unix socket, with the viewer's address in a
    header Caddy sets itself, so whatever a viewer sends under that name is replaced."""
    return {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": f"unix/{socket_path}"}],
        "headers": {
            "request": {"set": {HEADER_IP: ["{http.request.remote.host}"]}},
            "response": {"delete": ["Server"]},  # what the daemon is built with is its own
        },
    }


def config(port: int, gate_socket: Path, access_log: Path, videos_dir: Path) -> dict[str, Any]:
    """The stream's Caddy: its whole configuration, as its native JSON."""
    send_file = [
        {
            "handler": "headers",
            "response": {"set": {"Content-Type": [f"{{http.reverse_proxy.header.{HEADER_TYPE}}}"]}},
        },
        {"handler": "rewrite", "uri": f"/{{http.reverse_proxy.header.{HEADER_FILE}}}"},
        {"handler": "file_server", "root": str(videos_dir)},
    ]
    ask_gate = {
        **_proxy_to(gate_socket),
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
        "admin": _NO_ADMIN,
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


def dashboard_config(port: int, hosts: list[str], socket_path: Path) -> dict[str, Any]:
    """The public dashboard's Caddy: HTTPS with certificates from Caddy's own local
    authority, for every address in `hosts` (IPv4 and IPv6; a name works by its SNI).

    A browser sends no server name for an address, so the certificate cannot be picked by
    it. Each address gets a listener of its own, whose `default_sni` is that address. The
    authority is never put in the system's trust store: the service user cannot, and must
    not try. Plain HTTP is not redirected, so nothing listens on port 80.
    """
    addresses = [h for h in hosts if normal_ip(h)] or [""]  # only a name: one listener for all
    servers = {}
    for n, address in enumerate(addresses):
        listen = f"{url_host(address)}:{port}" if address else f":{port}"
        servers[DASHBOARD_SERVER if n == 0 else f"{DASHBOARD_SERVER}-{n + 1}"] = {
            "listen": [listen],
            "tls_connection_policies": [{"default_sni": address or hosts[0]}],
            "automatic_https": {"disable_redirects": True},
            "read_header_timeout": 10 * _SECOND,
            "idle_timeout": 120 * _SECOND,
            "routes": [{"handle": [_proxy_to(socket_path)]}],
        }
    return {
        "admin": _NO_ADMIN,
        "apps": {
            "pki": {"certificate_authorities": {"local": {"install_trust": False}}},
            "tls": {
                "certificates": {"automate": hosts},
                "automation": {
                    "policies": [{"subjects": hosts, "issuers": [{"module": "internal"}]}]
                },
            },
            "http": {"servers": servers},
        },
    }


_PEM = re.compile(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)


def certificate_fingerprint(pem: str) -> str:
    """The SHA-256 fingerprint of the first certificate in `pem`, as AB:CD:..., the way
    browsers and `openssl x509 -fingerprint -sha256` show it. Raises ValueError when there
    is no certificate in it."""
    found = _PEM.search(pem)
    if not found:
        raise ValueError("no certificate in the file")
    try:
        der = base64.b64decode("".join(found.group(1).split()), validate=True)
    except binascii.Error as exc:
        raise ValueError(f"the certificate is not valid base64: {exc}") from exc
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def read_certificate(path: Path) -> dict[str, str] | None:
    """A certificate file and its SHA-256 fingerprint, or None while there is none (Caddy
    makes its authority the first time it starts)."""
    try:
        pem = path.read_text(encoding="ascii")
        return {"fingerprint": certificate_fingerprint(pem), "pem": pem}
    except (OSError, UnicodeDecodeError, ValueError):
        return None


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
    if ip is None or not is_whole(size):
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


def _accepts(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


class CaddyProcess:
    """Runs `caddy run` as a child of the daemon, so it lives and dies with the service.
    A subclass says what it serves (`config`)."""

    needed_for = "serving streams"

    def __init__(self, runner: Runner, port: int, work_dir: Path) -> None:
        self.runner = runner
        self.port = port
        self.work_dir = work_dir  # Caddy's own files and its error output
        self._proc: subprocess.Popen[bytes] | None = None

    def config(self) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def probe_host(self) -> str:
        """Where to see that Caddy listens."""
        return "127.0.0.1"

    @property
    def data_dir(self) -> Path:
        """Caddy's storage: certificates, and the local authority of `tls internal`."""
        return self.work_dir / "data"

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
        binary = require(self.runner, "caddy", self.needed_for)
        if _accepts(self.port, self.probe_host):
            raise CaddyError(f"port {self.port} is already used by another program")
        self.work_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        config_file = self.work_dir / "caddy.json"
        write_atomic(config_file, json.dumps(self.config(), indent=1))
        # The service user has no home folder; Caddy keeps its files where it is told to.
        env = {
            **os.environ,
            "XDG_DATA_HOME": str(self.data_dir),
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
            if _accepts(self.port, self.probe_host):
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

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def why_stopped(self) -> str:
        """What Caddy last wrote, for a report after it stopped by itself."""
        return self._why()


class CaddyServer(CaddyProcess):
    """The stream's Caddy: the stream port, asking the gate about every request."""

    def __init__(self, runner: Runner, port: int, paths: Paths) -> None:
        super().__init__(runner, port, paths.caddy_dir)
        self.gate_socket = paths.gate_socket
        self.access_log = paths.access_log
        self.videos_dir = paths.videos_dir

    def config(self) -> dict[str, Any]:
        return config(self.port, self.gate_socket, self.access_log, self.videos_dir)

    def start(self) -> None:
        if not self.running():
            self.access_log.unlink(missing_ok=True)
        super().start()

    def stop(self) -> None:
        super().stop()
        self.access_log.unlink(missing_ok=True)


def dashboard_root_cert(paths: Paths) -> Path:
    """The public dashboard's local authority certificate, made by Caddy the first time it
    starts. Only this is stable: what Caddy serves lasts 12 hours, its intermediate 7 days.
    Caddy keeps it readable by the service user only."""
    return paths.dashboard_caddy_dir / "data/caddy/pki/authorities/local/root.crt"


class DashboardCaddy(CaddyProcess):
    """The public dashboard's Caddy: HTTPS on the dashboard port, for the addresses `hosts`."""

    needed_for = "the public dashboard"

    def __init__(self, runner: Runner, port: int, hosts: list[str], paths: Paths) -> None:
        super().__init__(runner, port, paths.dashboard_caddy_dir)
        self.hosts = hosts  # the daemon sets them before each start
        self.socket_path = paths.dashboard_socket
        self.root_cert = dashboard_root_cert(paths)

    def config(self) -> dict[str, Any]:
        return dashboard_config(self.port, self.hosts, self.socket_path)

    @property
    def probe_host(self) -> str:
        first = next((h for h in self.hosts if normal_ip(h)), "")
        return first or "127.0.0.1"


def _end(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(STOP_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
