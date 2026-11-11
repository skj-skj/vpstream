"""Small interface around external programs, so tests can swap in fakes."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
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


def ffmpeg_names(runner: Runner, what: str) -> set[str]:
    """Names this ffmpeg lists for `what`: "encoders" or "filters"."""
    res = runner.run(["ffmpeg", "-hide_banner", f"-{what}"], timeout=30)
    return parse_ffmpeg_list(res.stdout)
