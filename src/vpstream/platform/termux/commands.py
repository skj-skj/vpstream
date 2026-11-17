"""`init`, `uninstall`, `dash-pass` and `install` on Termux, where nothing needs root.

There is no service, no user and no firewall to set up: `init` writes the configuration and
says what is still missing; `uninstall` removes vpstream's own folders.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vpstream import client, config, login
from vpstream.files import remove_path
from vpstream.platform import termux
from vpstream.tools import Runner

NEEDED = ("ffmpeg", "ffprobe", "caddy", "mkvmerge", "mkvextract")
FOLDER_MODE = 0o700  # the person's own; other apps have no access to Termux's home anyway


class CommandError(RuntimeError):
    """The command cannot be done; the message says why and what to do."""


def missing(runner: Runner) -> list[str]:
    """The programs vpstream needs that are not installed."""
    return [name for name in NEEDED if not runner.which(name)]


def storage_problem(storage: Path = termux.STORAGE) -> str | None:
    """Why the phone's shared storage cannot be read, or None."""
    try:
        next(iter(storage.iterdir()), None)
    except OSError as exc:
        return f"Termux cannot read {storage} ({exc.strerror})"
    return None


def init(
    runner: Runner,
    paths: config.Paths,
    say: Callable[[str], None] = print,
    storage: Path = termux.STORAGE,
) -> None:
    """Write the configuration unless there is one, make the folders, and say what is left
    to do. Safe to run again."""
    path = paths.config_file
    path.parent.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
    if path.exists():
        say(f"{path}: kept")
    else:
        path.write_text(config.TERMUX_TEXT, encoding="utf-8")
        say(f"{path}: written")
    try:
        config.load(path)
    except config.ConfigError as exc:
        raise CommandError(f"fix the configuration: {exc}") from exc
    for folder in (paths.state_dir, paths.cache_dir):
        folder.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
    todo = []
    if lacking := missing(runner):
        todo.append(f"install what is missing ({', '.join(lacking)}): {termux.PACKAGES}")
    if problem := storage_problem(storage):
        todo.append(f"let Termux read your videos ({problem}): termux-setup-storage")
    if password_problem(paths):
        todo.append(f"set the dashboard password: {termux.HINTS.dash_pass}")
    todo.append(f"start the daemon: {termux.HINTS.start}")
    say("vpstream is set up for the local network. Left to do:")
    for n, step in enumerate(todo, 1):
        say(f"  {n}. {step}")


def password_problem(paths: config.Paths) -> str | None:
    """Why the dashboard has no password it can use, or None."""
    try:
        if login.load_verifier(paths.dashboard_key) is None:
            return "no password is set"
    except login.PasswordError as exc:
        return f"the password file cannot be used ({exc})"
    return None


def set_password(paths: config.Paths, password: str, say: Callable[[str], None] = print) -> None:
    """A new dashboard password, readable by this user only. The running daemon takes it
    at once and logs every browser out; a running stream goes on."""
    try:
        verifier = login.Verifier.make(password)
    except login.PasswordError as exc:
        raise CommandError(str(exc)) from exc
    paths.dashboard_key.parent.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
    login.write_verifier(paths.dashboard_key, verifier)
    for old in (paths.login_record, paths.device_key):
        old.unlink(missing_ok=True)
    say(f"{paths.dashboard_key}: dashboard password set (only a verifier of it is kept)")
    try:
        client.call(paths.socket, "POST", "dashboard/reload", {})
    except (client.DaemonUnavailable, client.DaemonRefused) as exc:
        say(f"the daemon did not take it now ({exc}); it does when it starts")
    else:
        say("the daemon took it: every browser must log in again; a stream goes on")


def uninstall(paths: config.Paths, purge: bool, say: Callable[[str], None] = print) -> None:
    """Remove the configuration, the state and the run folder; the prepared videos too
    with `purge`. The daemon must be stopped first."""
    if paths.socket.exists():
        try:
            client.call(paths.socket, "GET", "status")
        except client.DaemonUnavailable:
            pass
        else:
            raise CommandError(f"the daemon is running; stop it first: {termux.HINTS.stop}")
    for folder in (paths.config_file.parent, paths.state_dir, paths.run_dir):
        if folder.exists() or folder.is_symlink():
            remove_path(folder)
            say(f"{folder}: removed")
    cache = paths.cache_dir
    if purge or (cache.is_dir() and not any(cache.iterdir())):
        if cache.exists():
            remove_path(cache)
            say(f"{cache}: removed")
    elif cache.exists():
        say(f"{cache}: kept (prepared videos). Remove it with: vpstream uninstall --purge")
    say("vpstream's files are removed. The packages stay; remove them with pkg if you like.")


def install_refusal(program: str) -> str:
    """`vpstream install` is for Linux; Termux has its own packages."""
    return (
        f"vpstream does not install {program} on Termux; Termux's own package is the one to "
        f"use: {termux.HINTS.install(program)} (all of what vpstream needs: {termux.PACKAGES})"
    )


LOG_NAME = "daemon.log"
LOG_KEEP_BYTES = 5 * 1024 * 1024  # a longer log is moved aside to daemon.log.1 at `up`
UP_SECONDS = 15.0
DOWN_SECONDS = 20.0  # a stream and a prepare are stopped first


def _answers(paths: config.Paths) -> dict[str, Any] | None:
    try:
        info: dict[str, Any] = client.call(paths.socket, "GET", "status", timeout=3)
    except (client.DaemonUnavailable, client.DaemonRefused):
        return None
    return info


def _wait(until: Callable[[], bool], seconds: float, sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if until():
            return True
        sleep(0.2)
    return until()


def up(
    runner: Runner,
    paths: config.Paths,
    say: Callable[[str], None] = print,
    spawn: Callable[[list[str], Path], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Start the daemon in the background, with the phone kept awake (termux-wake-lock),
    and wait until it answers. Returns its status. A stream that ran when Android stopped
    Termux is taken up again by the daemon itself (see `control.reconcile`)."""
    if info := _answers(paths):
        say(f"the daemon is already running (pid {info.get('pid')})")
        return info
    paths.state_dir.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
    log = paths.state_dir / LOG_NAME
    with contextlib.suppress(OSError):
        if log.stat().st_size > LOG_KEEP_BYTES:
            os.replace(log, log.with_name(LOG_NAME + ".1"))
    # -P: the folder this is run from is never put on the module path, where a folder or
    # file of the same name (vpstream's own platform/, a json.py) would shadow a module.
    proc = (spawn or _spawn)([sys.executable, "-P", "-m", "vpstream", "daemon"], log)
    if runner.which("termux-wake-lock"):
        runner.run(["termux-wake-lock"], timeout=10)
    else:
        say("note: termux-wake-lock is missing; Android may pause vpstream with the screen off")
    if not _wait(lambda: _answers(paths) is not None or proc.poll() is not None, UP_SECONDS, sleep):
        raise CommandError(f"the daemon did not answer within {UP_SECONDS:g} seconds; see {log}")
    info = _answers(paths)
    if info is None:
        raise CommandError(f"the daemon stopped at once:\n{_tail(log)}")
    say(f"the daemon is running (pid {info.get('pid')}); its log is {log}")
    return info


def _spawn(argv: list[str], log: Path) -> subprocess.Popen[bytes]:
    with log.open("ab") as out:
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # it outlives this command and the terminal
            close_fds=True,
            cwd=log.parent,  # not the folder this was run from, which may go
        )


def _tail(log: Path, lines: int = 15) -> str:
    try:
        return "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError as exc:
        return f"(no log: {exc})"


def down(
    runner: Runner,
    paths: config.Paths,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Stop the daemon (it stops a running stream first) and let the phone sleep again."""
    if _answers(paths) is None:
        say("the daemon is not running")
    else:
        try:
            client.call(paths.socket, "POST", "daemon/stop", {})
        except (client.DaemonUnavailable, client.DaemonRefused) as exc:
            raise CommandError(f"the daemon did not stop: {exc}") from exc
        if not _wait(lambda: not paths.socket.exists(), DOWN_SECONDS, sleep):
            raise CommandError(f"the daemon is still stopping after {DOWN_SECONDS:g} seconds")
        say("the daemon is stopped")
    if runner.which("termux-wake-unlock"):
        runner.run(["termux-wake-unlock"], timeout=10)
