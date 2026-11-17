"""Caddy, the public server: its configuration, running it, and reading its access log.

Three Caddy processes can run, each a child of the daemon with a fixed configuration and no
admin endpoint. The stream's (`CaddyServer`) listens on the stream port only while a stream
runs, over plain HTTP or, with `[stream] https`, over HTTPS with a certificate it loads from
files. The public dashboard's (`DashboardCaddy`), only in public mode, serves the dashboard
over HTTPS with a certificate from its own local authority (`tls internal`). The third
(`CertificateCaddy`) runs only while the stream's certificate is requested (see
`certificate`).

The stream's Caddy hands every request to the daemon's gate over a unix socket. The gate
answers either with the page itself (an index, a playlist, a refusal) or with the number of
a video, which Caddy then sends, with range requests, from a folder that holds one link per
video. A number needs no encoding, which Caddy versions do differently for a path.
Nothing about a stream is in Caddy's configuration, and a change of alias, policy or device
applies on the next request without a reload.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Protocol

from vpstream import dashboard_ca
from vpstream.certificate import PROFILE, Certificate, CertificateError, first_der
from vpstream.checks import is_whole
from vpstream.config import Config, Paths
from vpstream.files import write_atomic
from vpstream.net import normal_ip, url_host
from vpstream.tools import Runner, require

# Set by Caddy on the way in, whatever the viewer sent: the address the request came from,
# and whether it came over HTTPS.
HEADER_IP = "X-Vpstream-Ip"
HEADER_SCHEME = "X-Vpstream-Scheme"
# Set by the gate on the way back: send the video with this number, with this type.
HEADER_FILE = "X-Vpstream-File"
HEADER_TYPE = "X-Vpstream-Type"

SERVER = "vpstream"
DASHBOARD_SERVER = "dashboard"
START_SECONDS = 10.0
OBTAIN_SECONDS = 120.0  # a request that has not ended by then is given up
# The first Caddy with ACME profiles; the only kind of certificate for an address needs one.
PROFILES_VERSION = (2, 10)
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
            "request": {
                "set": {
                    HEADER_IP: ["{http.request.remote.host}"],
                    HEADER_SCHEME: ["{http.request.scheme}"],
                }
            },
            "response": {"delete": ["Server"]},  # what the daemon is built with is its own
        },
    }


def config(
    port: int,
    gate_socket: Path,
    access_log: Path,
    videos_dir: Path,
    certificate: Certificate | None = None,
    host: str = "",
) -> dict[str, Any]:
    """The stream's Caddy: its whole configuration, as its native JSON. With a
    `certificate` it serves HTTPS with it, loaded from its files: this Caddy never asks for
    one (see `certificate`). With a `host` it listens on that address only (local mode),
    else on every address."""
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
    server: dict[str, Any] = {
        "listen": [f"{url_host(host) if host else ''}:{port}"],
        "automatic_https": {"disable": True},
        "read_header_timeout": 10 * _SECOND,
        "idle_timeout": 120 * _SECOND,
        "logs": {"default_logger_name": SERVER},
        "routes": [refuse_method, {"handle": [ask_gate]}],
    }
    apps: dict[str, Any] = {"http": {"servers": {SERVER: server}}}
    if certificate is not None:
        files = {"certificate": str(certificate.cert_file), "key": str(certificate.key_file)}
        apps["tls"] = {"certificates": {"load_files": [files]}}
        # A viewer sends no server name for an address. HTTP/3 would listen on UDP, which
        # the firewall does not open.
        server["tls_connection_policies"] = [{"default_sni": certificate.host}]
        server["protocols"] = ["h1", "h2"]
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
        "apps": apps,
    }


DASHBOARD_CA = "vpstream"  # Caddy's name for it, apart from its own "local"


def dashboard_config(
    port: int, hosts: list[str], socket_path: Path, ca_cert: Path, ca_key: Path
) -> dict[str, Any]:
    """The public dashboard's Caddy: HTTPS for every address in `hosts` (IPv4 and IPv6; a
    name works by its SNI), with certificates signed under the authority `init` made for
    this host (`dashboard_ca`), never Caddy's own, which would be valid for any site.

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
            "pki": {
                "certificate_authorities": {
                    DASHBOARD_CA: {
                        "name": "vpstream dashboard",
                        "install_trust": False,
                        "root": {
                            "certificate": str(ca_cert),
                            "private_key": str(ca_key),
                            "format": "pem_file",
                        },
                    }
                }
            },
            "tls": {
                "certificates": {"automate": hosts},
                "automation": {
                    "policies": [
                        {
                            "subjects": hosts,
                            "issuers": [{"module": "internal", "ca": DASHBOARD_CA}],
                        }
                    ]
                },
            },
            "http": {"servers": servers},
        },
    }


def certificate_config(host: str, config: Config) -> dict[str, Any]:
    """The Caddy that requests the stream's certificate: nothing but the request. Let's
    Encrypt checks over plain HTTP on `config.acme_port`, where Caddy answers only for the
    time of the check; TLS-ALPN is off, its port 443 is not opened."""
    issuer: dict[str, Any] = {
        "module": "acme",
        "ca": config.acme_ca,
        "profile": PROFILE,
        "challenges": {
            "http": {"alternate_port": config.acme_port},
            "tls-alpn": {"disabled": True},
        },
    }
    if config.acme_ca_root:
        issuer["trusted_roots_pem_files"] = [config.acme_ca_root]
    return {
        "admin": _NO_ADMIN,
        "apps": {
            "pki": {"certificate_authorities": {"local": {"install_trust": False}}},
            "tls": {
                "certificates": {"automate": [host]},
                "automation": {"policies": [{"subjects": [host], "issuers": [issuer]}]},
            },
        },
    }


def caddy_version(runner: Runner) -> tuple[int, ...] | None:
    """Caddy's version, from `caddy version`: `v2.11.9 h1:...`, or `2.6.2` from Ubuntu's
    package. None when it does not say (a build without its version, such as Termux's)."""
    path = runner.which("caddy")
    if path is None:
        return None
    found = re.match(
        r"v?(\d+)\.(\d+)(?:\.(\d+))?\b", runner.run([path, "version"], timeout=30).stdout
    )
    return None if found is None else tuple(int(n or 0) for n in found.groups())


def profiles_problem(runner: Runner) -> str | None:
    """Why this host's Caddy cannot request the stream's certificate, or None."""
    version = caddy_version(runner)
    if version is None or version[:2] >= PROFILES_VERSION:
        return None
    need = ".".join(map(str, PROFILES_VERSION))
    return (
        f"Caddy {'.'.join(map(str, version))} cannot request a certificate for an address "
        f"(that needs Caddy {need} or newer, for ACME profiles). Install the current Caddy "
        "from Caddy's own package repository: vpstream install caddy "
        "(see https://caddyserver.com/docs/install)"
    )


def certificate_fingerprint(pem: str) -> str:
    """The SHA-256 fingerprint of the first certificate in `pem`, as AB:CD:..., the way
    browsers and `openssl x509 -fingerprint -sha256` show it. Raises ValueError when there
    is no certificate in it."""
    return ":".join(f"{b:02X}" for b in hashlib.sha256(first_der(pem)).digest())


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
    listens = True  # started once it accepts connections on `port`; False: once it runs

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

    def _log_text(self) -> str:
        try:
            return self.error_log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _why(self) -> str:
        return self._log_text().strip()[-600:]

    def start(self) -> None:
        if self.running():
            return
        binary = require(self.runner, "caddy", self.needed_for)
        if problem := self.port_problem():
            raise CaddyError(problem)
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
            if not self.listens or _accepts(self.port, self.probe_host):
                self._proc = proc
                return
            time.sleep(0.1)
        if proc.poll() is None:
            _end(proc)
            raise CaddyError(f"Caddy did not listen on port {self.port}: {self._why()}")
        raise CaddyError(f"Caddy stopped at once (exit {proc.returncode}): {self._why()}")

    def port_problem(self) -> str | None:
        """Why Caddy cannot listen on its port, or None."""
        if _accepts(self.port, self.probe_host):
            return f"port {self.port} is already used by another program"
        return None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            _end(proc)

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def why_stopped(self) -> str:
        """What Caddy last wrote, for a report after it stopped by itself."""
        return self._why()


class StreamServer(Server, Protocol):
    """The stream's Caddy as the daemon uses it. Tests swap in a fake."""

    certificate: Certificate | None  # HTTPS with it, or plain HTTP; set before each start
    host: str  # the one address to listen on (local mode), or "" for every address


class CaddyServer(CaddyProcess):
    """The stream's Caddy: the stream port, asking the gate about every request."""

    def __init__(self, runner: Runner, port: int, paths: Paths) -> None:
        super().__init__(runner, port, paths.caddy_dir)
        self.gate_socket = paths.gate_socket
        self.access_log = paths.access_log
        self.videos_dir = paths.videos_dir
        self.certificate: Certificate | None = None
        self.host = ""  # local mode: the one address to listen on; set before each start

    def config(self) -> dict[str, Any]:
        return config(
            self.port,
            self.gate_socket,
            self.access_log,
            self.videos_dir,
            self.certificate,
            self.host,
        )

    @property
    def probe_host(self) -> str:
        return self.host or "127.0.0.1"

    def start(self) -> None:
        if not self.running():
            self.access_log.unlink(missing_ok=True)
        super().start()

    def stop(self) -> None:
        super().stop()
        self.access_log.unlink(missing_ok=True)


class DashboardCaddy(CaddyProcess):
    """The public dashboard's Caddy: HTTPS on the dashboard port, for the addresses `hosts`."""

    needed_for = "the public dashboard"

    def __init__(self, runner: Runner, port: int, hosts: list[str], paths: Paths) -> None:
        super().__init__(runner, port, paths.dashboard_caddy_dir)
        self.hosts = hosts  # the daemon sets them before each start
        self.socket_path = paths.dashboard_socket
        self.paths = paths
        # The authority `init` made (see `dashboard_ca`). Only this is stable: what Caddy
        # serves lasts 12 hours, its intermediate 7 days.
        self.root_cert = paths.dashboard_ca_cert

    def config(self) -> dict[str, Any]:
        return dashboard_config(
            self.port,
            self.hosts,
            self.socket_path,
            self.paths.dashboard_ca_cert,
            self.paths.dashboard_ca_key,
        )

    def start(self) -> None:
        """Serve only the addresses the authority permits: a phone refuses the others. Not
        at all without the authority, rather than with Caddy's own."""
        if not self.running():
            permitted = dashboard_ca.covered(self.paths)
            if permitted is None:
                raise CaddyError(
                    "the dashboard has no certificate authority of its own yet; run: "
                    "sudo vpstream init, then install the authority it shows on your phone"
                )
            if not any(h in permitted for h in self.hosts):
                raise CaddyError(
                    f"this host's addresses ({', '.join(self.hosts)}) are not the ones its "
                    f"authority is for ({', '.join(permitted)}); run: sudo vpstream init, "
                    "then install the new authority on your phone"
                )
            self.hosts = [h for h in self.hosts if h in permitted]
        super().start()

    @property
    def probe_host(self) -> str:
        first = next((h for h in self.hosts if normal_ip(h)), "")
        return first or "127.0.0.1"


class CertificateCaddy(CaddyProcess):
    """Requests the stream's certificate, then stops: started for one request at a time,
    with port 80 open only meanwhile (see `certificate.Certificates`). Caddy keeps what it
    gets in its storage."""

    needed_for = "HTTPS for streams"
    listens = False  # it listens only while Let's Encrypt checks

    def __init__(self, runner: Runner, config: Config, paths: Paths) -> None:
        super().__init__(runner, config.acme_port, paths.certificate_dir)
        self.settings = config
        self.host = ""
        self.timeout = OBTAIN_SECONDS

    def config(self) -> dict[str, Any]:
        return certificate_config(self.host, self.settings)

    def problem(self) -> str | None:
        """Why a request cannot work now: checked before port 80 is opened for it."""
        return profiles_problem(self.runner) or self.port_problem()

    def port_problem(self) -> str | None:
        """Bind the port as Caddy will, on every address: a program listening on any one of
        them would take Let's Encrypt's check, and Caddy goes on regardless."""
        for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            try:
                probe = socket.socket(family, socket.SOCK_STREAM)
            except OSError:  # no IPv6 on this host
                continue
            with probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    probe.bind((address, self.port))
                except PermissionError:
                    return (
                        f"this service may not listen on port {self.port}, where Let's Encrypt "
                        "checks; `sudo vpstream init --https` lets it"
                    )
                except OSError as exc:
                    if exc.errno == errno.EADDRNOTAVAIL and family == socket.AF_INET6:
                        continue
                    return (
                        f"port {self.port}, where Let's Encrypt checks, is already used by "
                        f"another program ({exc.strerror}); stop it, or start with --http"
                    )
        return None

    def obtain(self, host: str, done: Callable[[], bool]) -> None:
        """Request a certificate for `host`, until `done()` says it is stored. Caddy is
        stopped at the first failure: left alone it tries again every minute or so, and
        every failed check counts against a limit (5 an hour for an address)."""
        self.host = host
        try:
            self.start()
        except (CaddyError, OSError) as exc:
            raise CertificateError(f"Caddy could not request the certificate: {exc}") from exc
        try:
            deadline = time.monotonic() + self.timeout
            while not done():
                if failure := obtain_failure(self._log_text()):
                    raise CertificateError(f"the certificate was refused: {failure}")
                if not self.running():
                    raise CertificateError(
                        f"Caddy stopped before it had the certificate: {self._why()}"
                    )
                if time.monotonic() > deadline:
                    raise CertificateError(
                        f"no certificate after {self.timeout:g} seconds: {self._why()}"
                    )
                time.sleep(0.2)
        finally:
            self.stop()


def obtain_failure(log: str) -> str | None:
    """Why the request failed, from Caddy's log, or None while it has not. The check's own
    answer says most (the port could not be reached, the rate limit), so it is preferred."""
    problem = None
    for line in log.splitlines():
        if "address already in use" in line:
            # Caddy says it goes on as if the other program answered the check: it would
            # spend a failed check on it.
            return line.strip()
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("level") != "error":
            continue
        logger = str(entry.get("logger", ""))
        if entry.get("msg") == "challenge failed" and isinstance(entry.get("problem"), dict):
            problem = problem or str(entry["problem"].get("detail") or "")
        elif logger == "tls.obtain" and entry.get("msg") in (
            "will retry",
            "could not get certificate from issuer",
        ):
            return problem or str(entry.get("error") or entry.get("msg"))
    return None


def _end(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(STOP_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
