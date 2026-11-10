"""The stream port in ufw: a small root helper, and the daemon's way of calling it.

The daemon runs without privileges. Opening and closing the port is done by `vpstream-fw`,
which a sudoers rule lets the service user run as root with exactly `open` or `close`. The
helper takes no port: it reads the stream port from the root-owned configuration file, so the
caller cannot make it touch any other port. It writes down what it opened, next to that file,
and closes that too. It never enables, disables, resets or reloads ufw.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from vpstream import config
from vpstream.files import write_atomic
from vpstream.tools import Result, Runner, SystemRunner, tail

HELPER_NAME = "vpstream-fw"
ACTIONS = ("open", "close")
UFW_DIRS = ("/usr/sbin", "/usr/bin", "/sbin", "/bin")  # never taken from the caller's PATH
TIMEOUT = 60.0


class FirewallError(RuntimeError):
    pass


def ufw_args(action: str, port: int) -> list[str]:
    """The ufw arguments that open or close one TCP port. Both are safe to repeat."""
    if action == "open":
        return ["allow", f"{port}/tcp"]
    if action == "close":
        return ["delete", "allow", f"{port}/tcp"]
    raise ValueError(f"not an action: {action!r}")


def config_file_problem(path: Path, owner_uid: int = 0) -> str | None:
    """Why the helper must not trust this configuration file, or None.

    A missing file is fine: the defaults apply.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot check {path}: {exc.strerror}"
    if st.st_uid != owner_uid:
        return f"{path} is not owned by root"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path} is writable by users other than root"
    return None


def find_ufw(dirs: Sequence[str] = UFW_DIRS) -> str | None:
    for folder in dirs:
        candidate = Path(folder) / "ufw"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def recorded_ports(path: Path, owner_uid: int = 0) -> list[int]:
    """The ports the helper opened and has not closed yet, oldest first.

    A record that is missing, unreadable or not safely root's counts as empty.
    """
    if config_file_problem(path, owner_uid):
        return []
    try:
        words = path.read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError):
        return []
    ports: list[int] = []
    for word in words:
        if re.fullmatch(r"[0-9]{1,5}", word) and config.MIN_PORT <= int(word) <= 65535:
            ports.append(int(word))
    return list(dict.fromkeys(ports))


def _write_record(path: Path, ports: Sequence[int]) -> None:
    if not ports:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    write_atomic(path, "".join(f"{port}\n" for port in ports))
    path.chmod(0o644)


def helper(
    argv: Sequence[str],
    runner: Runner,
    *,
    config_file: Path,
    record_file: Path,
    euid: int,
    ufw: str | None,
    owner_uid: int = 0,
) -> tuple[int, str]:
    """The root helper without its surroundings: returns an exit code and a message.

    `open` writes the port down before it opens it. `close` closes every port written down
    as well as the configured one, so a port stays closable after the configuration was
    changed or broken while it was open.
    """
    if len(argv) != 1 or argv[0] not in ACTIONS:
        return 2, f"usage: {HELPER_NAME} open|close"
    action = argv[0]
    if euid != 0:
        return 1, f"{HELPER_NAME} must run as root (the daemon calls it through sudo)"
    port: int | None = None
    config_problem = ""
    if problem := config_file_problem(config_file, owner_uid):
        config_problem = f"refusing to read the port: {problem}"
    else:
        try:
            port = config.load(config_file).stream_port
        except config.ConfigError as exc:
            config_problem = str(exc)
    recorded = recorded_ports(record_file, owner_uid)
    if port is None and (action == "open" or not recorded):
        return 1, config_problem
    if ufw is None:
        return 1, "ufw was not found"

    if action == "open":
        assert port is not None
        try:
            _write_record(record_file, list(dict.fromkeys([*recorded, port])))
        except OSError as exc:
            return 1, f"cannot write {record_file}, so the port stays closed: {exc.strerror}"
        ports = [port]
    else:
        ports = list(dict.fromkeys([*recorded, *([] if port is None else [port])]))
    said = "done"
    for one in ports:
        res = runner.run([ufw, *ufw_args(action, one)], timeout=TIMEOUT)
        if res.returncode != 0:
            return 1, f"ufw failed: {tail(res, stdout=True)}"
        if one == port or port is None:
            said = (res.stdout.strip().splitlines() or ["done"])[0]
    if action == "close":
        try:
            _write_record(record_file, [])
        except OSError as exc:
            return 1, f"the port is closed, but {record_file} could not be removed: {exc.strerror}"
        listed = ", ".join(f"{p}/tcp" for p in ports if p != port)
        if port is None:
            return (
                1,
                f"closed {listed}, opened earlier; the port is not known now: {config_problem}",
            )
        if listed:
            return 0, f"stream port {port}/tcp, close: {said}; also closed {listed}, opened earlier"
    return 0, f"stream port {port}/tcp, {action}: {said}"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of `vpstream-fw`. Always uses the real paths, whatever the environment."""
    code, message = helper(
        sys.argv[1:] if argv is None else argv,
        SystemRunner(),
        config_file=config.Paths().config_file,
        record_file=config.Paths().open_ports_file,
        euid=os.geteuid(),
        ufw=find_ufw(),
    )
    print(message, file=sys.stderr if code else sys.stdout)
    return code


def helper_path() -> Path:
    """The helper that belongs to this install: next to the Python that runs vpstream."""
    return Path(sys.executable).parent / HELPER_NAME


class Firewall(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...


class SudoFirewall:
    """Opens and closes the stream port by running the helper through `sudo -n`."""

    def __init__(self, runner: Runner, helper: Path) -> None:
        self.runner = runner
        self.helper = helper

    def _call(self, action: str) -> None:
        try:
            res: Result = self.runner.run(["sudo", "-n", str(self.helper), action], timeout=TIMEOUT)
        except OSError as exc:  # sudo is missing or cannot be run
            raise FirewallError(f"could not {action} the stream port: {exc}") from exc
        if res.returncode != 0:
            raise FirewallError(f"could not {action} the stream port: {tail(res, stdout=True)}")

    def open(self) -> None:
        self._call("open")

    def close(self) -> None:
        self._call("close")
