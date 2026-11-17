"""The always-on daemon: serves the API on a unix socket and on the dashboard port.

By default the dashboard port is bound to the loopback address only, never to every
interface. It is `tailscale serve`, set up by `vpstream init`, that publishes it on the
tailnet over HTTPS.

In public mode (`[dashboard] public = true`) the daemon serves the dashboard, behind its
password, on a unix socket of its own, and runs a Caddy of its own in front of it on the
dashboard port, over HTTPS. The port is opened in the firewall only while that Caddy runs.
"""

from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vpstream import __version__, api, local, net, platform, prepare, public
from vpstream import login as auth
from vpstream.caddy import (
    CaddyError,
    CaddyServer,
    CertificateCaddy,
    DashboardCaddy,
    Server,
    StreamServer,
    read_certificate,
)
from vpstream.certificate import Certificates
from vpstream.config import Config, Paths
from vpstream.control import Control
from vpstream.platform.common import Firewall, FirewallError, switch
from vpstream.platform.linux import tailscale
from vpstream.tools import Runner

SOCKET_MODE = 0o660  # the service user and its group; the folder keeps everyone else out
RETRY_SECONDS = 10.0
MAX_RETRY_SECONDS = 300.0  # a public dashboard that keeps failing is tried less and less often
TICK_SECONDS = 1.0  # how often the stream's time limit, Caddy and the access log are looked at
LOOPBACK = "127.0.0.1"
URL_SECONDS = 30.0  # how long the tailnet address is remembered between status requests
# A status request asks Tailscale two things. Both waits together stay well under the time
# the command line waits for the daemon (client.TIMEOUT), so a slow Tailscale is reported as
# "not published" instead of the daemon looking dead.
TAILSCALE_SECONDS = 4.0
PROBE_SECONDS = 2.0


class DaemonError(RuntimeError):
    pass


class DashboardServer(Server, Protocol):
    """The public dashboard's Caddy as the daemon uses it. Tests swap in a fake."""

    root_cert: Path
    hosts: list[str]  # the addresses its certificates are for; set before each start

    def why_stopped(self) -> str: ...


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
                    f"stop it first ({platform.hints().stop})"
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


def _make_server(host: str, port: int, app: Any, listener: socket.socket) -> Any:
    """A threaded server on a socket that is already listening. Requests that only read (a
    viewer's, a status poll) are not logged one by one: there is one for every piece of a
    video. Changes are."""
    from werkzeug.serving import WSGIRequestHandler, make_server

    class Handler(WSGIRequestHandler):
        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            if self.command not in ("GET", "HEAD"):
                super().log_request(code, size)

    return make_server(
        host, port, app, threaded=True, request_handler=Handler, fd=listener.fileno()
    )


@dataclass
class Daemon:
    config: Config
    paths: Paths
    runner: Runner
    firewall: Firewall | None = None
    server: StreamServer | None = None  # the public server; None: streams cannot be started
    certificates: Certificates | None = None  # with `[stream] https`: the certificate
    # Public mode only: the dashboard's Caddy, and the firewall for the dashboard port.
    dashboard_server: DashboardServer | None = None
    dashboard_firewall: Firewall | None = None
    log: Callable[[str], None] = print
    retry_seconds: float = RETRY_SECONDS
    started: float = field(default_factory=time.time)
    dashboard_address: str | None = None
    dashboard_problem: str = "starting"
    ready: threading.Event = field(default_factory=threading.Event)
    dashboard_port_state: str = "closed"  # public mode: "open", "closed" or "unknown"
    # Local mode: the dashboard on the local network's address, behind the password.
    lan_address: str | None = None
    lan_problem: str = "starting"
    control: Control = field(init=False)
    login: auth.Login | None = field(init=False, default=None)
    _published: tuple[float, str | None, str | None] | None = None

    def __post_init__(self) -> None:
        if self.config.dashboard_public or self.config.local:
            self.login = auth.Login(
                self._verifier(),
                auth.Limiter(self.paths.login_record, log=self.log),
                device_secret=self._device_secret(),
            )
        speeds = prepare.Speeds(self.config.copy_speed, self.config.encode_speed)
        preparer = prepare.Preparer(self.paths.cache_dir, self.runner, self.log, speeds=speeds)
        self.control = Control(
            self.config,
            self.paths,
            self.firewall,
            self.server,
            self.log,
            runner=self.runner,
            preparer=preparer,
            certificates=self.certificates,
        )

    def _verifier(self) -> auth.Verifier | None:
        try:
            verifier = auth.load_verifier(self.paths.dashboard_key)
        except auth.PasswordError as exc:
            self.log(f"WARNING: nobody can log in to the dashboard: {exc}")
            return None
        if verifier is None:
            self.log(f"WARNING: no dashboard password is set: {platform.hints().dash_pass}")
        return verifier

    def _device_secret(self, new: bool = False) -> bytes:
        """The key of the device cookies, kept across restarts; `new` makes another."""
        path = self.paths.device_key
        try:
            if new:
                path.unlink(missing_ok=True)
            return auth.load_secret(path)
        except OSError as exc:
            self.log(f"WARNING: {path}: {exc}; browsers are known only until a restart")
            return secrets.token_bytes(auth.SECRET_BYTES)

    def reload_password(self) -> dict[str, Any]:
        """`vpstream dash-pass` changed the password: take it, log everyone out and forget
        every device and every failed login. A running stream goes on."""
        assert self.login is not None  # the route is there in public mode only
        self.login.reset(self._verifier(), self._device_secret(new=True))
        self.log("the dashboard password was changed: every browser must log in again")
        return {"password": self.login.verifier is not None}

    def unblock(self) -> dict[str, Any]:
        """`vpstream dash-unblock`: forget every failed login, for the owner to log in."""
        assert self.login is not None  # the route is there in public mode only
        self.login.unblock()
        self.log("failed logins to the dashboard were forgotten (dash-unblock)")
        return {}

    def public_hosts(self) -> list[str]:
        """The addresses the public dashboard's certificates are for, the one its URL names
        first. Looked up on each try: at boot the network may not be up yet."""
        return net.public_hosts(self.config.stream_host)

    def certificate(self) -> dict[str, str] | None:
        if self.dashboard_server is None:
            return None
        return read_certificate(self.dashboard_server.root_cert)

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

    def status(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.started, 1),
            **self.control.status(),
            "https": {
                "enabled": self.config.stream_https,
                "certificate": self.control.certificate_status(),
            },
            "dashboard": self._dashboard_status(),
        }

    def _dashboard_status(self) -> dict[str, Any]:
        if self.config.local:
            return self._local_status()
        public = self.config.dashboard_public
        info: dict[str, Any] = {
            "mode": "local" if self.config.local else "public" if public else "tailnet",
            "port": self.config.dashboard_port,
            "address": self.dashboard_address,
            "url": None,
            "problem": None,
        }
        if public:
            cert = self.certificate()
            info["port_state"] = self.dashboard_port_state
            info["certificate"] = cert["fingerprint"] if cert else None
            info["password"] = bool(self.login and self.login.verifier)
        if not self.dashboard_address:
            info["problem"] = self.dashboard_problem
        elif public and self.dashboard_server is not None:
            hosts = self.dashboard_server.hosts
            info["url"] = net.url("https", hosts[0], self.config.dashboard_port) + "/"
        elif self.config.local:
            info["url"] = f"http://{self.dashboard_address}/"
        else:
            info["url"], info["problem"] = self.published()
        return info

    def _local_status(self) -> dict[str, Any]:
        here = self.dashboard_address
        return {
            "mode": "local",
            "port": self.config.dashboard_port,
            "address": self.lan_address,
            "url": f"http://{self.lan_address}/" if self.lan_address else None,
            "problem": None if self.lan_address else self.lan_problem,
            "this_device": f"http://{here}/" if here else None,
            "this_device_problem": None if here else self.dashboard_problem,
            "this_device_password": platform.is_termux(),
            "password": bool(self.login and self.login.verifier),
        }

    def _unix_server(self, path: Path, app: Any) -> tuple[socket.socket, Any]:
        listener = bind_unix(path)
        return listener, _make_server(f"unix://{path}", 0, app, listener)

    def _tick(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_SECONDS):
            try:
                self.control.tick()
            except Exception as exc:  # one bad look must not end the looking
                self.log(f"WARNING: checking the stream failed: {exc!r}")

    def serve(self, stop: threading.Event) -> None:
        """Serve until `stop` is set. On the way out a running stream is stopped and the
        socket files are removed."""
        apps = {
            self.paths.socket: api.create_app(
                self.status, self.control, None, self.certificate, self._admin(stop)
            ),
            self.paths.gate_socket: public.create_app(self.control),
        }
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        # Both sockets first: a second daemon is refused here, before it touches the firewall
        # or the state, and the gate is there before Caddy can be started.
        servers: list[tuple[Path, socket.socket, Any]] = []
        try:
            for path, app in apps.items():
                servers.append((path, *self._unix_server(path, app)))
        except DaemonError:
            _close(servers)
            raise
        try:
            self.control.reconcile()
        except BaseException:
            _close(servers)
            raise
        threads = [
            threading.Thread(target=server.serve_forever, name=path.name, daemon=True)
            for path, _, server in servers
        ]
        if self.config.local:
            threads += self._local_dashboards(stop)
        else:
            keep_up: Callable[[Any, threading.Event], None]
            if self.login is None:
                dash, keep_up = apps[self.paths.socket], self._dashboard
            else:
                dash = api.create_app(self.status, self.control, self.login, self.certificate)
                keep_up = self._public_dashboard
            threads.append(threading.Thread(target=keep_up, args=(dash, stop), name="dash"))
        threads.append(threading.Thread(target=self._tick, args=(stop,), name="tick"))
        for thread in threads:
            thread.daemon = True
            thread.start()
        self.log(f"vpstream {__version__} daemon listening on {self.paths.socket}")
        self.ready.set()
        try:
            stop.wait()
        finally:
            stop.set()
            try:
                self.control.shutdown()
            except Exception as exc:  # the sockets are still closed and removed
                self.log(f"WARNING: stopping the stream failed: {exc!r}")
            for _, _, server in servers:
                server.shutdown()
            for thread in threads:
                thread.join(timeout=5)
            _close(servers)
            self.log("vpstream daemon stopped")

    def _local_dashboards(self, stop: threading.Event) -> list[threading.Thread]:
        """Local mode: the dashboard on the local network's address, behind the password,
        and on the loopback address. On Termux that one has the password too, since every
        app on the phone can reach it; on Linux it is the host's own, as on the tailnet."""
        # Nothing is in front of these: the address a request comes from is the viewer's,
        # and a header that says otherwise is the viewer's own.
        lan = api.create_app(
            self.status, self.control, self.login, self.certificate, secure=False, ip_header=False
        )
        home = lan if platform.is_termux() else api.create_app(self.status, self.control)
        return [
            threading.Thread(target=self._dashboard, args=(home, stop), name="dash"),
            threading.Thread(target=self._dashboard, args=(lan, stop, True), name="dash-lan"),
        ]

    def _lan_host(self) -> str:
        """The local network's address for the dashboard, once it has a password."""
        if self.login is None or self.login.verifier is None:
            raise DaemonError(f"no password is set: {platform.hints().dash_pass}")
        try:
            return local.find(self.config.local_address)
        except local.LocalError as exc:
            raise DaemonError(str(exc)) from exc

    def _lan_lost(self, host: str) -> bool:
        return not local.is_mine(host) or self.login is None or self.login.verifier is None

    def _dashboard(self, app: Any, stop: threading.Event, lan: bool = False) -> None:
        """Keep the dashboard up on the loopback address, or with `lan` on the local
        network's (local mode); wait and retry while the port is taken. The local network's
        is taken down when its address goes, and comes back with it."""
        port = self.config.dashboard_port
        while not stop.is_set():
            try:
                host = self._lan_host() if lan else LOOPBACK
                listener = bind_tcp(host, port)
            except (OSError, DaemonError) as exc:
                where = " on the local network" if lan else ""
                self._dashboard_failed(f"the dashboard is not available{where}: {exc}", lan=lan)
                stop.wait(self.retry_seconds)
                continue
            bound_port = listener.getsockname()[1]
            server = _make_server(host, bound_port, app, listener)
            address = f"{host}:{bound_port}"
            if lan:
                self.lan_address = address
                self.log(f"dashboard on http://{address}/ (local network, behind the password)")
            else:
                self.dashboard_address = address
                self.log(f"dashboard on http://{address}/ (this host only)")

            def close(s: Any = server, watched: str = host if lan else "") -> None:
                while not stop.wait(TICK_SECONDS if watched else None):
                    if self._lan_lost(watched):
                        break
                s.shutdown()

            threading.Thread(target=close, daemon=True).start()
            try:
                server.serve_forever()
            finally:
                if lan:
                    self.lan_address, self.lan_problem = None, "stopped"
                else:
                    self.dashboard_address, self.dashboard_problem = None, "stopped"
                server.server_close()
                listener.close()
            if lan and not stop.is_set():
                self.log(f"the dashboard on {address} is closed: the address is gone")

    def _admin(self, stop: threading.Event) -> dict[str, Callable[[], Any]] | None:
        """What only the command line's socket may ask for: the dashboard's password, and on
        Termux, where no service manager stops the daemon, `vpstream down`."""
        routes: dict[str, Callable[[], Any]] = {}
        if self.login is not None:
            routes |= {"dashboard/reload": self.reload_password, "dashboard/unblock": self.unblock}
        if platform.is_termux():

            def stop_daemon() -> dict[str, Any]:
                self.log("asked to stop (vpstream down)")
                stop.set()
                return {"stopping": True}

            routes["daemon/stop"] = stop_daemon
        return routes or None

    def _dashboard_port(self, action: str) -> None:
        """Open or close the dashboard port. Without a helper (a trial run) nothing is done."""
        if self.dashboard_firewall is None:
            self.dashboard_port_state = "not opened (no firewall helper)"
            return
        try:
            self.dashboard_port_state = switch(self.dashboard_firewall, action)
        except FirewallError:
            self.dashboard_port_state = "unknown"
            raise

    def _dashboard_failed(self, problem: str, wait: float | None = None, lan: bool = False) -> None:
        if problem != (self.lan_problem if lan else self.dashboard_problem):
            self.log(f"{problem}; trying again in {wait or self.retry_seconds:g} seconds")
        if lan:
            self.lan_problem = problem
        else:
            self.dashboard_problem = problem

    def _public_dashboard(self, app: Any, stop: threading.Event) -> None:
        """Keep the public dashboard up: the app on its unix socket, Caddy in front of it,
        then the port open. When any part fails or Caddy stops, all of it is taken down,
        the port first, and tried again."""
        caddy = self.dashboard_server
        if caddy is None:
            self.dashboard_problem = "the public dashboard is not available: no Caddy"
            return
        path = self.paths.dashboard_socket
        port = self.config.dashboard_port
        failures = 0
        while not stop.is_set():
            served: tuple[socket.socket, Any] | None = None
            # Each failure in a row doubles the wait, so a dashboard that cannot come up
            # (sudo refused, the port taken) does not call sudo every few seconds.
            wait = min(self.retry_seconds * 2**failures, MAX_RETRY_SECONDS)
            try:
                caddy.hosts = self.public_hosts()
                if not caddy.hosts:
                    raise DaemonError("this host's public address is not known; set [stream] host")
                served = self._unix_server(path, app)
                threading.Thread(
                    target=served[1].serve_forever, name=path.name, daemon=True
                ).start()
                caddy.start()
                self._dashboard_port("open")
            except (DaemonError, CaddyError, FirewallError, OSError) as exc:
                failures += 1
                self._dashboard_failed(f"the public dashboard is not available: {exc}", wait)
            else:
                failures = 0
                self.dashboard_address = f"0.0.0.0:{port}"
                self.log(f"public dashboard on {net.url('https', caddy.hosts[0], port)}/")
                while not stop.wait(TICK_SECONDS) and caddy.running():
                    pass
                if not stop.is_set():
                    self._dashboard_failed(
                        f"Caddy for the dashboard stopped: {caddy.why_stopped()}"
                    )
            finally:
                self.dashboard_address = None
                self._public_down(caddy, served, path)
            stop.wait(wait)
        self.dashboard_problem = "stopped"

    def _public_down(
        self, caddy: DashboardServer, served: tuple[socket.socket, Any] | None, path: Path
    ) -> None:
        # Only a port that was opened is closed here; one whose opening failed is left to
        # the next open or to ExecStopPost (dash-close closes whatever the helper recorded).
        if self.dashboard_port_state == "open":
            try:
                self._dashboard_port("close")
            except FirewallError as exc:
                self.log(f"WARNING: {exc}")
        caddy.stop()
        if served is not None:
            _close([(path, *served)], shutdown=True)


def _close(servers: list[tuple[Path, socket.socket, Any]], shutdown: bool = False) -> None:
    for path, listener, server in servers:
        if shutdown:  # it is serving in a thread of its own
            server.shutdown()
        server.server_close()
        listener.close()
        path.unlink(missing_ok=True)


def stream_certificates(
    config: Config,
    paths: Paths,
    runner: Runner,
    firewall: Firewall | None,
    log: Callable[[str], None],
) -> Certificates | None:
    """With `[stream] https`: the stream's certificate, requested by a Caddy of its own
    with port 80 opened through `firewall` meanwhile."""
    if not config.stream_https:
        return None
    return Certificates(config, paths, CertificateCaddy(runner, config, paths), firewall, log=log)


def run(
    config: Config,
    paths: Paths,
    runner: Runner,
    firewall: Firewall | None = None,
    dashboard_firewall: Firewall | None = None,
    acme_firewall: Firewall | None = None,
) -> int:
    """Run the daemon in the foreground until SIGTERM or Ctrl+C."""
    import signal

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    server = CaddyServer(runner, config.stream_port, paths)
    log = lambda m: print(m, flush=True)  # noqa: E731
    certificates = stream_certificates(config, paths, runner, acme_firewall, log)
    daemon = Daemon(config, paths, runner, firewall, server, certificates, log=log)
    if config.dashboard_public:
        daemon.dashboard_server = DashboardCaddy(runner, config.dashboard_port, [], paths)
        daemon.dashboard_firewall = dashboard_firewall
    daemon.serve(stop)
    return 0
