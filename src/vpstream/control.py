"""Starting, stopping and changing the stream: the one place that drives Caddy, the firewall
and the state file. The command line and the dashboard both end up here."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream import net, state
from vpstream.caddy import CaddyError, LogTail, Server
from vpstream.config import Config, Paths
from vpstream.firewall import Firewall, FirewallError
from vpstream.stream import MISSING, Answer, Stream, StreamError, check_alias
from vpstream.tools import ToolMissing

BOOT_ID = "/proc/sys/kernel/random/boot_id"


class ControlError(RuntimeError):
    """The host did not do what the stream needs (Caddy, the firewall)."""


def boot_id() -> str:
    """A name for this boot of the machine: a stream is never carried over a reboot."""
    try:
        with open(BOOT_ID, encoding="ascii") as fh:
            return fh.read().strip()
    except OSError:
        return ""


@dataclass
class Control:
    config: Config
    paths: Paths
    firewall: Firewall | None = None
    server: Server | None = None
    log: Callable[[str], None] = print
    clock: Callable[[], float] = time.time
    boot: str = field(default_factory=boot_id)
    stream: Stream | None = None
    port_state: str = "unknown"  # "closed", "open" or "unknown"
    firewall_problem: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _tail: LogTail | None = None
    _host: str | None = None

    # --- the pieces

    @property
    def _state_file(self) -> Path:
        return self.paths.state_dir / state.FILE_NAME

    def _save(self) -> None:
        recorded = None if self.stream is None else {**self.stream.to_dict(), "boot": self.boot}
        state.save(self._state_file, state.State(stream=recorded))

    def _port(self, action: str) -> None:
        """Open or close the stream port. Without a helper (a trial run) nothing is done."""
        if self.firewall is None:
            return
        try:
            self.firewall.open() if action == "open" else self.firewall.close()
        except FirewallError as exc:
            self.port_state = "unknown"
            self.firewall_problem = str(exc)
            raise
        self.port_state = "open" if action == "open" else "closed"
        self.firewall_problem = None

    def base_url(self) -> str:
        if self._host is None:
            self._host = self.config.stream_host or net.outgoing_address()
        return f"http://{net.url_host(self._host)}:{self.config.stream_port}"

    def _running(self) -> Stream:
        if self.stream is None:
            raise StreamError("no stream is running")
        return self.stream

    def _serve(self, stream: Stream) -> None:
        """Start Caddy, then open the port: the port is never open with nothing behind it
        that asks the gate."""
        if self.server is None:
            raise ControlError("this daemon has no public server to start")
        try:
            self.server.start()
        except (CaddyError, ToolMissing, OSError) as exc:
            raise ControlError(f"Caddy could not be started: {exc}") from exc
        try:
            self._port("open")
        except FirewallError as exc:
            self.server.stop()
            raise ControlError(str(exc)) from exc
        self.stream = stream
        self._tail = LogTail(self.paths.access_log)
        self._save()

    def _end(self, why: str) -> None:
        """Close the port, then stop Caddy, then forget the stream and its approvals."""
        port = self.config.stream_port
        try:
            self._port("close")
        except FirewallError as exc:
            self.log(f"WARNING: {exc}")
        if self.server is not None:
            self.server.stop()
        self.stream = self._tail = None
        self._save()
        if self.firewall_problem:
            port_now = "may still be open"
        else:
            port_now = "is closed" if self.firewall else "was not opened (no firewall helper)"
        self.log(f"the stream is stopped ({why}); stream port {port} {port_now}")

    # --- when the daemon starts and stops

    def reconcile(self) -> None:
        """Bring the host in line with the recorded state when the daemon starts.

        A stream that was running when the daemon crashed is taken up again, if its time is
        not over and the machine has not rebooted. Otherwise the stream port is closed: an
        open rule survives a crash or a reboot.
        """
        with self._lock:
            recorded, problem = state.load(self._state_file)
            if problem:
                self.log(f"{problem}; treating it as no stream running")
            if recorded.stream is not None and self._resume(recorded.stream):
                return
            port = self.config.stream_port
            if recorded.stream is not None:
                self._save()
            if self.firewall is None:
                self.log(f"no firewall helper; stream port {port} was not checked")
                return
            try:
                self._port("close")
            except FirewallError as exc:
                self.log(f"WARNING: {exc}")
                return
            self.log(f"stream port {port} is closed (no stream is running)")

    def _resume(self, recorded: dict[str, Any]) -> bool:
        try:
            stream = Stream.from_dict(recorded)
        except StreamError as exc:
            self.log(f"{exc}; treating it as no stream running")
            return False
        if recorded.get("boot") != self.boot:
            self.log("the recorded stream is from before a reboot; it is not started again")
            return False
        if stream.expires is not None and stream.expires <= self.clock():
            self.log("the recorded stream's time limit is over; it is not started again")
            return False
        try:
            self._serve(stream)
        except ControlError as exc:
            self.log(f"WARNING: the recorded stream could not be started again: {exc}")
            return False
        self.log(f"the stream /{stream.alias} is running again after a restart of the daemon")
        return True

    def shutdown(self) -> None:
        """The daemon is told to stop: the stream stops with it."""
        with self._lock:
            if self.stream is not None:
                self._end("the daemon is stopping")

    def tick(self) -> None:
        """Called every second or so: count the bytes sent, and stop the stream when its
        time is over or Caddy is gone."""
        with self._lock:
            if self.stream is None:
                return
            if self._tail is not None:
                for ip, size in self._tail.read():
                    self.stream.devices.add_bytes(ip, size)
            if self.stream.expires is not None and self.stream.expires <= self.clock():
                self._end("its time limit is over")
            elif self.server is not None and not self.server.running():
                self._end("Caddy stopped unexpectedly")

    # --- what the command line and the dashboard ask for

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self.stream is not None:
                raise StreamError(
                    f"a stream is already running (/{self.stream.alias}); stop it first"
                )
            stream = Stream.create(options, self.clock())
            self._serve(stream)
            self.log(f"stream /{stream.alias} started: {stream.source} ({stream.policy})")
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._running()
            self._end("stopped by request")
            return self.status()

    def _change(self, change: Callable[[Stream], Any]) -> dict[str, Any]:
        with self._lock:
            change(self._running())
            self._save()
            return self.status()

    def set_alias(self, alias: Any) -> dict[str, Any]:
        new = check_alias(alias)
        return self._change(lambda s: setattr(s, "alias", new))

    def set_policy(self, policy: Any) -> dict[str, Any]:
        return self._change(lambda s: s.set_policy(policy))

    def unlock(self) -> dict[str, Any]:
        return self._change(lambda s: s.unlock())

    def set_device(self, ip: Any, allowed: Any) -> dict[str, Any]:
        if not isinstance(ip, str) or not isinstance(allowed, bool):
            raise StreamError("a device change needs an ip and allowed (true or false)")
        return self._change(lambda s: s.devices.decide(ip, allowed, self.clock()))

    # --- what viewers ask for

    def visit(self, path: str, ip: str, user_agent: str, wants_html: bool) -> Answer:
        with self._lock:
            if self.stream is None:
                return MISSING
            return self.stream.visit(path, ip, user_agent, wants_html, self.clock())

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            stream = self.stream
            return {
                "now": now,
                "stream": None if stream is None else stream.describe(self.base_url(), now),
                "stream_port": self.config.stream_port,
                "stream_port_state": self.port_state,
                "firewall_problem": self.firewall_problem,
            }
