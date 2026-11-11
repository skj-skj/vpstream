"""Small interface around external programs, so tests can swap in fakes."""

from __future__ import annotations

import collections
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from vpstream.ffargs import parse_ffmpeg_list


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def which(self, name: str) -> str | None: ...

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result: ...


class SystemRunner:
    """Runs real programs found on PATH."""

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        """Run a program. A timeout is returned as a failed Result, never raised."""
        try:
            proc = subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return Result(TIMEOUT_RETURNCODE, _text(exc.stdout), timed_out(timeout))
        return Result(proc.returncode, proc.stdout, proc.stderr)


class _NoPrograms:
    """A runner on a host with no programs at all."""

    def which(self, name: str) -> str | None:
        return None

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        return Result(127, "", f"{args[0]}: not available")


NO_PROGRAMS: Runner = _NoPrograms()

TIMEOUT_RETURNCODE = 124  # the same code as coreutils `timeout`


def timed_out(seconds: float | None) -> str:
    return f"stopped: no result after {seconds:g} seconds"


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def tail(res: Result, chars: int = 300, default: str = "failed", *, stdout: bool = False) -> str:
    """The end of a failed program's error output, for short messages.

    `stdout` also reads standard output, for programs that print errors there (mkvtoolnix).
    """
    text = f"{res.stdout}\n{res.stderr}" if stdout else res.stderr
    return text.strip()[-chars:] or default


def low_priority_prefix(runner: Runner) -> list[str]:
    """Return a command prefix that runs a program at the lowest CPU and I/O priority.

    Each tool is used only when it exists on this host.
    """
    prefix: list[str] = []
    if runner.which("nice"):
        prefix += ["nice", "-n", "19"]
    if runner.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    return prefix


# The Ubuntu package that provides each program, where it differs from the program's name.
PACKAGES = {
    "ffprobe": "ffmpeg",
    "mkvextract": "mkvtoolnix",
    "mkvmerge": "mkvtoolnix",
    "setfacl": "acl",
}


class ToolMissing(RuntimeError):
    def __init__(self, name: str, needed_for: str = "") -> None:
        why = f" It is needed for {needed_for}." if needed_for else ""
        how = (
            "vpstream install ffmpeg"
            if name in ("ffmpeg", "ffprobe")
            else f"sudo apt install {PACKAGES.get(name, name)}"
        )
        super().__init__(f"{name} was not found on PATH.{why} Install it first, for example: {how}")
        self.name = name


def require(runner: Runner, name: str, needed_for: str = "") -> str:
    path = runner.which(name)
    if not path:
        raise ToolMissing(name, needed_for)
    return path


class Cancelled(Exception):
    """A long job was told to stop before it was done."""


POLL_SECONDS = 0.5
OUTPUT_LINES = 40  # lines of a program's output kept for an error message


def run_cancellable(
    args: Sequence[str],
    cancel: threading.Event,
    on_line: Callable[[str], None] | None = None,
    poll: float = POLL_SECONDS,
) -> tuple[int, str]:
    """Run a long program until it ends or `cancel` is set.

    Its output (standard output and errors together) is read line by line as it comes and
    each line goes to `on_line`. Returns the exit code and the last lines of the output.
    Raises Cancelled, after the program has ended, when `cancel` was set first.
    """
    proc = subprocess.Popen(
        list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    last: collections.deque[str] = collections.deque(maxlen=OUTPUT_LINES)

    def read() -> None:
        assert proc.stdout is not None
        failed = False
        # Read to the end whatever happens: a pipe that is not drained stops the program.
        for line in proc.stdout:
            last.append(line.rstrip("\n"))
            if on_line is not None and not failed:
                try:
                    on_line(last[-1])
                except Exception:  # a broken report must not stop the job it reports on
                    failed = True

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        while proc.poll() is None:
            if cancel.wait(poll):
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise Cancelled
    finally:
        reader.join(5)
        if proc.stdout is not None:
            proc.stdout.close()
    return proc.returncode, "\n".join(last)


def ffmpeg_names(runner: Runner, what: str) -> set[str]:
    """Names this ffmpeg lists for `what`: "encoders" or "filters"."""
    res = runner.run(["ffmpeg", "-hide_banner", f"-{what}"], timeout=30)
    return parse_ffmpeg_list(res.stdout)
