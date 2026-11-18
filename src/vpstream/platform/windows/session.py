"""The daemon, inside the window: what `vpstream daemon` runs on Linux, run in-process.

The same Control starts, stops and changes the stream, and the same gate (`public`) answers
every request Caddy hands it. Windows' Python has no unix sockets (bpo-33408), so the gate
listens on 127.0.0.1, on a port Windows picks and no other program may share
(SO_EXCLUSIVEADDRUSE), and Caddy names a secret only this run knows with every request:
another program on the PC that finds the port is refused all the same.

What else the window adds to a daemon: a free port that is easy to type (1337, 1234,
2345, ...), the address the person chose, both kept in the configuration; a stream taken up
after a crash, but not after three crashes in a row; a log file the person can send.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hmac
import re
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vpstream import config, public, state
from vpstream.caddy import SERVER, CaddyServer
from vpstream.config import Config, Paths
from vpstream.control import Control
from vpstream.daemon import TICK_SECONDS, make_control
from vpstream.platform.common import FirewallError
from vpstream.platform.windows import WINDOWS_TEXT, network, processes
from vpstream.platform.windows.firewall import NetshFirewall
from vpstream.tools import Runner, SystemRunner

GATE_HEADER = "X-Vpstream-Gate"
GATE_ENVIRON = "HTTP_X_VPSTREAM_GATE"
# The stream port: the configured one, else the first of these that is free.
PORTS = (1337, 1234, 2345, 3456, 4567, 5678, 6789, 7890, 9876, 8765, 7654, 6543, 5432, 4321)
CRASHES_FILE = "crashes"
MOST_CRASHES = 3  # in a row: the stream is not taken up again
CALM_SECONDS = 600.0  # a stream taken up again that runs this long counts as fine
LOG_BYTES = 2_000_000  # the log is started again above this; the last one is kept


# --- the log


class Log:
    """vpstream.log, next to the state folder: what the daemon would print, with the time.
    `stopped` is why the last stream stopped, for the window to say."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.lines: list[str] = []
        self.stopped = ""
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_BYTES:
                path.replace(path.with_suffix(".old.log"))

    def __call__(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
        found = re.match(r"the stream is stopped \((.*)\); ", message)
        with self.lock:
            if found:
                self.stopped = found[1]
            self.lines = [*self.lines[-199:], line]
            with contextlib.suppress(OSError), self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


# --- the configuration file


def set_setting(text: str, section: str, key: str, value: str) -> str:
    """`text` with `key = value` in `[section]` (value as TOML, quoted where it must be):
    the line replaced, or a commented one taken up, or added. The rest is kept as it is."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"[{section}]"), None)
    if start is None:
        lines += ["", f"[{section}]", f"{key} = {value}"]
        return "\n".join(lines).lstrip("\n") + "\n"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    found = [i for i in range(start + 1, end) if pattern.match(lines[i])]
    set_line = next((i for i in found if not lines[i].lstrip().startswith("#")), None)
    where = set_line if set_line is not None else (found[0] if found else None)
    if where is None:
        lines.insert(start + 1, f"{key} = {value}")
    else:
        lines[where] = f"{key} = {value}"
    return "\n".join(lines) + "\n"


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_setting(path: Path, section: str, key: str, value: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = WINDOWS_TEXT
    new = set_setting(text, section, key, value)
    config.parse(new)  # never write what would not be read back
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")


def load_config(path: Path) -> Config:
    """The configuration, written first with Windows' defaults when there is none."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(WINDOWS_TEXT, encoding="utf-8")
    return config.load(path)


# --- the port


def port_free(host: str, port: int) -> bool:
    """Whether a program could listen on `host`:`port` now. Nothing listens after."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        if sys.platform == "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined,unused-ignore]
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(
    host: str, configured: int, free: Callable[[str, int], bool] = port_free
) -> int | None:
    """The configured port when it is free, else the first free one of PORTS."""
    for port in dict.fromkeys((configured, *PORTS)):
        if free(host, port):
            return port
    return None


# --- the gate, on a loopback port


def guarded(app: Any, secret: str) -> Any:
    """`app`, answering only requests that carry `secret` (as Caddy sends them)."""

    def check(environ: dict[str, Any], start_response: Callable[..., Any]) -> Any:
        if not hmac.compare_digest(environ.get(GATE_ENVIRON, ""), secret):
            start_response("403 Forbidden", [("Content-Type", "text/plain")])
            return [b"forbidden\n"]
        return app(environ, start_response)

    return check


def gate_server(app: Any) -> Any:
    """A threaded server for the gate on 127.0.0.1, on a port Windows picks, that no other
    program can bind as well."""
    from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler

    class Quiet(WSGIRequestHandler):
        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            pass  # one for every piece of a video; the access log has them

    class Exclusive(ThreadedWSGIServer):
        def server_bind(self) -> None:
            if sys.platform == "win32":
                opt = socket.SO_EXCLUSIVEADDRUSE  # type: ignore[attr-defined,unused-ignore]
                self.socket.setsockopt(socket.SOL_SOCKET, opt, 1)
            super().server_bind()

    Exclusive.allow_reuse_address = False
    return Exclusive("127.0.0.1", 0, app, handler=Quiet)


# --- Caddy


def to_gate(cfg: dict[str, Any], port: int, secret: str, windows: bool = True) -> dict[str, Any]:
    """The stream's Caddy configuration, asking the gate on 127.0.0.1:`port` with `secret`
    instead of on a unix socket. On Windows its access log is not rolled: Windows does not
    rename a file another program reads, and the stream's log is removed when it stops."""
    server = cfg["apps"]["http"]["servers"][SERVER]
    for route in server["routes"]:
        for handler in route.get("handle", []):
            if handler.get("handler") == "reverse_proxy":
                handler["upstreams"] = [{"dial": f"127.0.0.1:{port}"}]
                handler["headers"]["request"]["set"][GATE_HEADER] = [secret]
    if windows:
        writer = cfg["logging"]["logs"]["access"]["writer"]
        writer.pop("roll_size_mb", None)
        writer.pop("roll_keep", None)
        writer["roll"] = False
    return cfg


class WindowsCaddy(CaddyServer):
    """The stream's Caddy on Windows: it asks the gate over TCP, the firewall rule is added
    before it listens (else Windows would ask the person), it runs at normal priority
    though the window does not, and a pid file stops one a crashed window left."""

    def __init__(
        self, runner: Runner, port: int, paths: Paths, gate_port: int, secret: str
    ) -> None:
        super().__init__(runner, port, paths)
        self.gate_port, self.secret = gate_port, secret
        self.firewall: NetshFirewall | None = None

    def config(self) -> dict[str, Any]:
        return to_gate(super().config(), self.gate_port, self.secret)

    @property
    def pid_file(self) -> Path:
        return self.work_dir / "caddy.pid.json"

    def stop_left_over(self) -> None:
        processes.stop_noted(self.pid_file)

    def start(self) -> None:
        if self.running():
            return
        if self.firewall is not None:
            self.firewall.program = self.runner.which("caddy") or self.firewall.program
            self.firewall.port = self.port
            self.firewall.open()
        try:
            super().start()
        except BaseException:
            if self.firewall is not None:
                with contextlib.suppress(FirewallError):
                    self.firewall.close()
            raise
        if self._proc is not None:
            processes.normal_priority(self._proc.pid)
            with contextlib.suppress(OSError):
                processes.write_pid(self.pid_file, self._proc.pid)

    def stop(self) -> None:
        super().stop()
        self.pid_file.unlink(missing_ok=True)


# --- the session


@dataclasses.dataclass
class Session:
    """Everything a running window holds. `open()` before use, `close()` at the end."""

    paths: Paths
    runner: Runner = dataclasses.field(default_factory=SystemRunner)
    log: Callable[[str], None] = print
    free: Callable[[str, int], bool] = port_free
    boot: str = dataclasses.field(default_factory=processes.boot_id)
    firewall: NetshFirewall | None = None
    server: Any = None
    clock: Callable[[], float] = time.monotonic
    has_address: Callable[[str], bool] = network.in_use
    config: Config = dataclasses.field(init=False)
    control: Control = dataclasses.field(init=False)
    notice: str = ""  # what the window says when it opens (three crashes)
    _gate: Any = None
    _stop: threading.Event = dataclasses.field(default_factory=threading.Event)
    _threads: list[threading.Thread] = dataclasses.field(default_factory=list)
    _calm_at: float | None = None  # when a resumed stream counts as running fine

    def __post_init__(self) -> None:
        self.config = load_config(self.paths.config_file)
        secret = secrets.token_urlsafe(24)
        self._gate = None
        if self.server is None:  # the real one; tests bring their own
            self.firewall = self.firewall or NetshFirewall(
                self.runner, self.config.stream_port, self.runner.which("caddy") or "caddy.exe"
            )
            self.server = WindowsCaddy(self.runner, self.config.stream_port, self.paths, 0, secret)
            self.server.firewall = self.firewall
        self.control = make_control(
            self.config,
            self.paths,
            self.runner,
            self.firewall,
            self.server,
            self.log,
            boot=self.boot,
            has_address=self.has_address,
        )

    # --- open and close

    @property
    def crashes_file(self) -> Path:
        return self.paths.state_dir / CRASHES_FILE

    def _crashes(self) -> int:
        try:
            return int(self.crashes_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return 0

    def _set_crashes(self, count: int) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        if count:
            self.crashes_file.write_text(f"{count}\n", encoding="ascii")
        else:
            self.crashes_file.unlink(missing_ok=True)

    def open(self) -> None:
        """Serve the gate, take up a stream the last window left (unless it kept crashing),
        and look at the stream every second."""
        if isinstance(self.server, WindowsCaddy):
            self._gate = gate_server(guarded(public.create_app(self.control), self.server.secret))
            self.server.gate_port = self._gate.server_port
            self._threads.append(threading.Thread(target=self._gate.serve_forever, name="gate"))
        recorded, _ = state.load(self.paths.state_dir / state.FILE_NAME)
        if recorded.stream is not None:
            crashes = self._crashes() + 1
            self._set_crashes(crashes)
            if crashes >= MOST_CRASHES:
                state.save(self.paths.state_dir / state.FILE_NAME, state.State(stream=None))
                self.notice = (
                    f"vpstream closed unexpectedly {crashes} times in a row while it streamed, "
                    "so the stream was not started again. The log says what happened."
                )
                self.log(f"WARNING: {crashes} crashes in a row; the stream is not taken up")
            else:
                self._calm_at = self.clock() + CALM_SECONDS
        self.control.reconcile()
        if self.control.stream is None:
            self._calm_at = None
        self._threads.append(threading.Thread(target=self._tick, name="tick"))
        for thread in self._threads:
            thread.daemon = True
            thread.start()

    def _tick(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.control.tick()
            except Exception as exc:  # one bad look must not end the looking
                self.log(f"WARNING: checking the stream failed: {exc!r}")
            if self._calm_at is not None and self.clock() >= self._calm_at:
                self._calm_at = None
                self._set_crashes(0)

    def close(self) -> None:
        """The window closes: the stream stops, the rule goes, the gate ends."""
        self._stop.set()
        try:
            self.control.shutdown()
        finally:
            if self._gate is not None:
                self._gate.shutdown()
            for thread in self._threads:
                thread.join(timeout=5)
            self._set_crashes(0)

    # --- what the window asks for

    def use_port(self, port: int) -> None:
        self.config = dataclasses.replace(self.config, stream_port=port)
        self.control.config = self.config
        self.server.port = port
        if self.firewall is not None:
            self.firewall.port = port

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        """Start a stream on a free port (kept for next time when it changed)."""
        host = self.control.host()
        if host and self.control.stream is None:
            port = pick_port(host, self.config.stream_port, self.free)
            if port is None:
                raise StreamPortError(
                    f"no port to stream on is free on {host}: {', '.join(map(str, PORTS))}"
                )
            if port != self.config.stream_port:
                self.log(f"port {self.config.stream_port} is in use; streaming on {port}")
                self.use_port(port)
                save_setting(self.paths.config_file, "stream", "port", str(port))
        found = self.control.start(options)
        self._calm_at = None
        self._set_crashes(0)
        return found

    def stop_reason(self) -> str:
        """Why the last stream stopped, as the log said."""
        return str(getattr(self.log, "stopped", ""))

    def stop(self) -> dict[str, Any]:
        found = self.control.stop()
        self._calm_at = None
        self._set_crashes(0)
        return found

    def choose_address(self, address: str) -> None:
        """ "" for the one Windows reaches the network from. Kept in the configuration."""
        save_setting(self.paths.config_file, "network", "address", toml_string(address))
        self.config = dataclasses.replace(self.config, local_address=address)
        self.control.config = self.config


class StreamPortError(RuntimeError):
    pass
