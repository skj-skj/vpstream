"""The stream port in ufw: a small root helper, and the daemon's way of calling it.

The daemon runs without privileges. Opening and closing the port is done by `vpstream-fw`,
which a sudoers rule lets the service user run as root with exactly `open` or `close`. The
helper takes no port: it reads the stream port from the root-owned configuration file, so the
caller cannot make it touch any other port. It never enables, disables, resets or reloads ufw.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from vpstream import config
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


def helper(
    argv: Sequence[str],
    runner: Runner,
    *,
    config_file: Path,
    euid: int,
    ufw: str | None,
    owner_uid: int = 0,
) -> tuple[int, str]:
    """The root helper without its surroundings: returns an exit code and a message."""
    if len(argv) != 1 or argv[0] not in ACTIONS:
        return 2, f"usage: {HELPER_NAME} open|close"
    if euid != 0:
        return 1, f"{HELPER_NAME} must run as root (the daemon calls it through sudo)"
    if problem := config_file_problem(config_file, owner_uid):
        return 1, f"refusing to read the port: {problem}"
    try:
        port = config.load(config_file).stream_port
    except config.ConfigError as exc:
        return 1, str(exc)
    if ufw is None:
        return 1, "ufw was not found"
    res = runner.run([ufw, *ufw_args(argv[0], port)], timeout=TIMEOUT)
    if res.returncode != 0:
        return 1, f"ufw failed: {tail(res, stdout=True)}"
    said = (res.stdout.strip().splitlines() or ["done"])[0]
    return 0, f"stream port {port}/tcp, {argv[0]}: {said}"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of `vpstream-fw`. Always uses the real paths, whatever the environment."""
    code, message = helper(
        sys.argv[1:] if argv is None else argv,
        SystemRunner(),
        config_file=config.Paths().config_file,
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
        res: Result = self.runner.run(["sudo", "-n", str(self.helper), action], timeout=TIMEOUT)
        if res.returncode != 0:
            raise FirewallError(f"could not {action} the stream port: {tail(res, stdout=True)}")

    def open(self) -> None:
        self._call("open")

    def close(self) -> None:
        self._call("close")
