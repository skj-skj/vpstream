"""The stream port in ufw: a small root helper, and the daemon's way of calling it.

The daemon runs without privileges. Opening and closing the port is done by `vpstream-fw`,
which a sudoers rule lets the service user run as root with exactly one of ACTIONS. The
helper takes no port: it reads the stream port from the root-owned configuration file, so the
caller cannot make it touch any other port. It writes down what it opened, next to that file,
and closes that too. It never enables, disables, resets or reloads ufw.

`dash-open` and `dash-close` do the same for the dashboard port, which is open only in public
mode: `dash-open` refuses unless the configuration says `[dashboard] public = true`, and
`dash-close` closes only what was written down, never a rule it did not add.
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
STREAM_ACTIONS = ("open", "close")
DASHBOARD_ACTIONS = ("dash-open", "dash-close")
ACTIONS = (*STREAM_ACTIONS, *DASHBOARD_ACTIONS)
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


def _trusted_config(config_file: Path, owner_uid: int) -> tuple[config.Config | None, str]:
    """The configuration, or None and why it is not trusted or not readable."""
    if problem := config_file_problem(config_file, owner_uid):
        return None, f"refusing to read the port: {problem}"
    try:
        return config.load(config_file), ""
    except config.ConfigError as exc:
        return None, str(exc)


def _first_line(res: Result) -> str:
    return (res.stdout.strip().splitlines() or ["done"])[0]


def helper(
    argv: Sequence[str],
    runner: Runner,
    *,
    config_file: Path,
    record_file: Path,
    dashboard_record_file: Path,
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
        return 2, f"usage: {HELPER_NAME} {'|'.join(ACTIONS)}"
    action = argv[0]
    if euid != 0:
        return 1, f"{HELPER_NAME} must run as root (the daemon calls it through sudo)"
    cfg, config_problem = _trusted_config(config_file, owner_uid)
    if action in DASHBOARD_ACTIONS:
        return _dashboard(
            action, runner, cfg, config_problem, dashboard_record_file, ufw, owner_uid
        )
    port = None if cfg is None else cfg.stream_port
    recorded = recorded_ports(record_file, owner_uid)
    if port is None and (action == "open" or not recorded):
        return 1, config_problem
    if action == "open":
        assert port is not None
        ports = [port]
    else:
        ports = list(dict.fromkeys([*recorded, *([] if port is None else [port])]))
    problem, said = _apply(runner, ufw, action, ports, recorded, record_file)
    if problem:
        return 1, problem
    first = said[port] if port is not None else "done"
    if action == "close":
        listed = ", ".join(f"{p}/tcp" for p in ports if p != port)
        if port is None:
            return (
                1,
                f"closed {listed}, opened earlier; the port is not known now: {config_problem}",
            )
        if listed:
            return (
                0,
                f"stream port {port}/tcp, close: {first}; also closed {listed}, opened earlier",
            )
    return 0, f"stream port {port}/tcp, {action}: {first}"


def _apply(
    runner: Runner,
    ufw: str | None,
    action: str,
    ports: Sequence[int],
    recorded: Sequence[int],
    record_file: Path,
) -> tuple[str, dict[int, str]]:
    """Open or close `ports` in ufw, keeping `record_file` true: a port is written down
    before it is opened, and the record is emptied once every port in it is closed.
    Returns what went wrong ("" when nothing did) and ufw's first line for each port."""
    if ufw is None:
        return "ufw was not found", {}
    if action == "open":
        try:
            _write_record(record_file, list(dict.fromkeys([*recorded, *ports])))
        except OSError as exc:
            return f"cannot write {record_file}, so the port stays closed: {exc.strerror}", {}
    said: dict[int, str] = {}
    for one in ports:
        res = runner.run([ufw, *ufw_args(action, one)], timeout=TIMEOUT)
        if res.returncode != 0:
            return f"ufw failed: {tail(res, stdout=True)}", said
        said[one] = _first_line(res)
    if action == "close":
        try:
            _write_record(record_file, [])
        except OSError as exc:
            return (
                f"the port is closed, but {record_file} could not be removed: {exc.strerror}",
                said,
            )
    return "", said


def _dashboard(
    action: str,
    runner: Runner,
    cfg: config.Config | None,
    config_problem: str,
    record_file: Path,
    ufw: str | None,
    owner_uid: int,
) -> tuple[int, str]:
    """`dash-open` and `dash-close`: the dashboard port, in public mode only. Only what
    `dash-open` wrote down is ever closed."""
    recorded = recorded_ports(record_file, owner_uid)
    if action == "dash-open":
        if cfg is None:
            return 1, config_problem
        if not cfg.dashboard_public:
            return 1, "the dashboard is not public ([dashboard] public = false); not opening"
        ports = [cfg.dashboard_port]
    else:
        ports = recorded
        if not ports:
            return 0, "dashboard port: nothing to close"
    ufw_action = action.removeprefix("dash-")
    problem, said = _apply(runner, ufw, ufw_action, ports, recorded, record_file)
    if problem:
        return 1, problem
    listed = "; ".join(f"{p}/tcp: {line}" for p, line in said.items())
    return 0, f"dashboard port, {ufw_action}: {listed}"


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of `vpstream-fw`. Always uses the real paths, whatever the environment."""
    paths = config.Paths()
    code, message = helper(
        sys.argv[1:] if argv is None else argv,
        SystemRunner(),
        config_file=paths.config_file,
        record_file=paths.open_ports_file,
        dashboard_record_file=paths.open_dashboard_ports_file,
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


STREAM, DASHBOARD = "stream", "dashboard"


class SudoFirewall:
    """Opens and closes one port, the stream's or the dashboard's, by running the helper
    through `sudo -n`."""

    def __init__(self, runner: Runner, helper: Path, port: str = STREAM) -> None:
        self.runner = runner
        self.helper = helper
        self.port = port
        self._actions = dict(
            zip(
                STREAM_ACTIONS, STREAM_ACTIONS if port == STREAM else DASHBOARD_ACTIONS, strict=True
            )
        )

    def _call(self, action: str) -> None:
        argv = ["sudo", "-n", str(self.helper), self._actions[action]]
        what = f"could not {action} the {self.port} port"
        try:
            res: Result = self.runner.run(argv, timeout=TIMEOUT)
        except OSError as exc:  # sudo is missing or cannot be run
            raise FirewallError(f"{what}: {exc}") from exc
        if res.returncode != 0:
            raise FirewallError(f"{what}: {tail(res, stdout=True)}")

    def open(self) -> None:
        self._call("open")

    def close(self) -> None:
        self._call("close")
