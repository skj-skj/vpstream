"""The always-on daemon: serves the API on a unix socket and on the dashboard port.

The dashboard port is bound to the loopback address only, never to every interface. It is
`tailscale serve`, set up by `vpstream init`, that publishes it on the tailnet over HTTPS.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import __version__, api, state, tailscale
from vpstream.config import Config, Paths
from vpstream.firewall import Firewall, FirewallError
from vpstream.tools import Runner

SOCKET_MODE = 0o660  # the service user and its group; the folder keeps everyone else out
RETRY_SECONDS = 10.0
LOOPBACK = "127.0.0.1"
URL_SECONDS = 30.0  # how long the tailnet address is remembered between status requests
# A status request asks Tailscale two things. Both waits together stay well under the time
# the command line waits for the daemon (client.TIMEOUT), so a slow Tailscale is reported as
# "not published" instead of the daemon looking dead.
TAILSCALE_SECONDS = 4.0
PROBE_SECONDS = 2.0


class DaemonError(RuntimeError):
    pass


def socket_in_use(path: Path) -> bool:
    """Whether something answers on the unix socket at `path`. A file left by a daemon that
    is gone refuses the connection."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(PROBE_SECONDS)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    finally:
        probe.close()
    return True


def bind_unix(path: Path, mode: int = SOCKET_MODE) -> socket.socket:
    """Listen on a unix socket at `path`, replacing a socket file left by an earlier run.

    A socket that another daemon still listens on is never taken over.
    """
    try:
        if path.is_socket():
            if socket_in_use(path):
                raise DaemonError(
                    f"a vpstream daemon is already listening on {path}; "
                    "stop it first (sudo systemctl stop vpstream)"
                )
            path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # The mask applies from the moment the file exists, so it is never open to others.
        old = os.umask(0o777 & ~mode)
        try:
            sock.bind(str(path))
        finally:
            os.umask(old)
        sock.listen(16)
    except OSError as exc:
        raise DaemonError(f"cannot listen on the socket {path}: {exc}") from exc
    return sock


def bind_tcp(host: str, port: int) -> socket.socket:
    """Listen on one address. Raises OSError when the address or port cannot be used."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(16)
    except OSError:
        sock.close()
        raise
    return sock


@dataclass
class Daemon:
    config: Config
    paths: Paths
    runner: Runner
    firewall: Firewall | None = None
    log: Callable[[str], None] = print
    retry_seconds: float = RETRY_SECONDS
    started: float = field(default_factory=time.time)
    dashboard_address: str | None = None
    dashboard_problem: str = "starting"
    ready: threading.Event = field(default_factory=threading.Event)
    stream: dict[str, Any] | None = None
    stream_port_state: str = "unknown"  # "closed", "open" or "unknown"
    firewall_problem: str | None = None
    _published: tuple[float, str | None, str | None] | None = None

    def published(self) -> tuple[str | None, str | None]:
        """The dashboard's tailnet URL, or why it has none. Asked from Tailscale, then
        remembered for a short while."""
        now = time.monotonic()
        if self._published and now - self._published[0] < URL_SECONDS:
            return self._published[1], self._published[2]
        port = self.config.dashboard_port
        url: str | None = None
        problem: str | None = None
        try:
            proxy = tailscale.serve_target(self.runner, port, TAILSCALE_SECONDS)
            name = tailscale.node(self.runner, TAILSCALE_SECONDS).name
            if proxy != tailscale.local_target(port) or not name:
                problem = "not published on the tailnet; publish it with: sudo vpstream init"
            else:
                url = f"https://{name}:{port}/"
        except tailscale.TailscaleDown as exc:
            problem = f"not published on the tailnet: {exc}"
        self._published = (now, url, problem)
        return url, problem

    def reconcile(self) -> None:
        """Bring the firewall in line with the recorded state when the daemon starts.

        An open rule survives a crash or a reboot, so the stream port is closed here unless
        a running stream is recorded.
        """
        recorded, problem = state.load(self.paths.state_dir / state.FILE_NAME)
        if problem:
            self.log(f"{problem}; treating it as no stream running")
        self.stream = recorded.stream
        port = self.config.stream_port
        if self.stream is not None:
            self.log(f"a running stream is recorded; leaving stream port {port} as it is")
            return
        if self.firewall is None:
            self.log(f"no firewall helper; stream port {port} was not checked")
            return
        try:
            self.firewall.close()
        except FirewallError as exc:
            self.firewall_problem = str(exc)
            self.log(f"WARNING: {exc}")
            return
        self.stream_port_state = "closed"
        self.log(f"stream port {port} is closed (no stream is running)")

    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.started, 1),
            "stream": self.stream,
            "stream_port": self.config.stream_port,
            "stream_port_state": self.stream_port_state,
            "firewall_problem": self.firewall_problem,
            "dashboard": self._dashboard_status(),
        }

    def _dashboard_status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "port": self.config.dashboard_port,
            "address": self.dashboard_address,
            "url": None,
            "problem": None,
        }
        if not self.dashboard_address:
            info["problem"] = self.dashboard_problem
        else:
            info["url"], info["problem"] = self.published()
        return info

    def serve(self, stop: threading.Event) -> None:
        """Serve until `stop` is set. The socket file is removed on the way out."""
        from werkzeug.serving import make_server

        self.reconcile()
        app = api.create_app(self.status)
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        listener = bind_unix(self.paths.socket)
        server = make_server(
            f"unix://{self.paths.socket}", 0, app, threaded=True, fd=listener.fileno()
        )
        threads = [
            threading.Thread(target=server.serve_forever, name="socket", daemon=True),
            threading.Thread(target=self._dashboard, args=(app, stop), name="dash", daemon=True),
        ]
        for thread in threads:
            thread.start()
        self.log(f"vpstream {__version__} daemon listening on {self.paths.socket}")
        self.ready.set()
        try:
            stop.wait()
        finally:
            server.shutdown()
            for thread in threads:
                thread.join(timeout=5)
            server.server_close()
            listener.close()
            self.paths.socket.unlink(missing_ok=True)
            self.log("vpstream daemon stopped")

    def _dashboard(self, app: Any, stop: threading.Event) -> None:
        """Keep the dashboard up on the loopback address; wait and retry while the port is
        taken."""
        from werkzeug.serving import make_server

        port = self.config.dashboard_port
        while not stop.is_set():
            host = LOOPBACK
            try:
                listener = bind_tcp(host, port)
            except OSError as exc:
                problem = f"the dashboard is not available: {exc}"
                if problem != self.dashboard_problem:
                    self.log(f"{problem}; trying again every {self.retry_seconds:g} seconds")
                self.dashboard_problem = problem
                stop.wait(self.retry_seconds)
                continue
            bound_port = listener.getsockname()[1]
            server = make_server(host, bound_port, app, threaded=True, fd=listener.fileno())
            self.dashboard_address = f"{host}:{bound_port}"
            self.log(f"dashboard on http://{self.dashboard_address}/ (this host only)")
            closer = threading.Thread(
                target=lambda s=server: (stop.wait(), s.shutdown()), daemon=True
            )
            closer.start()
            try:
                server.serve_forever()
            finally:
                self.dashboard_address = None
                self.dashboard_problem = "stopped"
                server.server_close()
                listener.close()


def run(config: Config, paths: Paths, runner: Runner, firewall: Firewall | None = None) -> int:
    """Run the daemon in the foreground until SIGTERM or Ctrl+C."""
    import signal

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    Daemon(config, paths, runner, firewall, log=lambda m: print(m, flush=True)).serve(stop)
    return 0
