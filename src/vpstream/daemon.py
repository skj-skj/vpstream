"""The always-on daemon: serves the API on a unix socket and on the dashboard port.

The dashboard port is bound to the Tailscale address only, never to every interface. While
Tailscale has no address the daemon keeps running, so the CLI works, and tries again.
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

from vpstream import __version__, api, tailscale
from vpstream.config import Config, Paths
from vpstream.tools import Runner

SOCKET_MODE = 0o660  # the service user and its group; the folder keeps everyone else out
RETRY_SECONDS = 10.0


class DaemonError(RuntimeError):
    pass


def bind_unix(path: Path, mode: int = SOCKET_MODE) -> socket.socket:
    """Listen on a unix socket at `path`, replacing a socket file left by an earlier run."""
    try:
        if path.is_socket():
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
    log: Callable[[str], None] = print
    retry_seconds: float = RETRY_SECONDS
    started: float = field(default_factory=time.time)
    dashboard_address: str | None = None
    dashboard_problem: str = "starting"
    ready: threading.Event = field(default_factory=threading.Event)

    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.started, 1),
            "stream": None,
            "stream_port": self.config.stream_port,
            "dashboard": {
                "port": self.config.dashboard_port,
                "address": self.dashboard_address,
                "problem": None if self.dashboard_address else self.dashboard_problem,
            },
        }

    def serve(self, stop: threading.Event) -> None:
        """Serve until `stop` is set. The socket file is removed on the way out."""
        from werkzeug.serving import make_server

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
        """Keep the dashboard up on the Tailscale address; wait and retry while it has none."""
        from werkzeug.serving import make_server

        port = self.config.dashboard_port
        while not stop.is_set():
            try:
                host = tailscale.ipv4(self.runner)
                listener = bind_tcp(host, port)
            except (tailscale.TailscaleDown, OSError) as exc:
                problem = f"the dashboard is not available: {exc}"
                if problem != self.dashboard_problem:
                    self.log(f"{problem}; trying again every {self.retry_seconds:g} seconds")
                self.dashboard_problem = problem
                stop.wait(self.retry_seconds)
                continue
            bound_port = listener.getsockname()[1]
            server = make_server(host, bound_port, app, threaded=True, fd=listener.fileno())
            self.dashboard_address = f"{host}:{bound_port}"
            self.log(f"dashboard on http://{self.dashboard_address}/ (Tailscale only)")
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


def run(config: Config, paths: Paths, runner: Runner) -> int:
    """Run the daemon in the foreground until SIGTERM or Ctrl+C."""
    import signal

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    Daemon(config, paths, runner, log=lambda m: print(m, flush=True)).serve(stop)
    return 0
