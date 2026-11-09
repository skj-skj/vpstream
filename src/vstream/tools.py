"""Small interface around external programs, so tests can swap in fakes."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


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
        proc = subprocess.run(
            list(args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)


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


class ToolMissing(RuntimeError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name} was not found on PATH. Install it first, for example: "
            f"sudo apt install {'ffmpeg' if name.startswith('ff') else name}"
        )
        self.name = name


def require(runner: Runner, name: str) -> str:
    path = runner.which(name)
    if not path:
        raise ToolMissing(name)
    return path
