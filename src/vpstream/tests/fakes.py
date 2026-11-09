"""Fake runner so tests never call real programs."""

from __future__ import annotations

from collections.abc import Sequence

from vpstream.tools import Result


class FakeRunner:
    def __init__(self, available: Sequence[str] = ("ffmpeg", "ffprobe")) -> None:
        self.available = set(available)
        self.calls: list[list[str]] = []
        self._responses: list[tuple[tuple[str, ...], Result]] = []

    def on(self, *prefix: str, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """Answer any command that starts with `prefix` (matched after nice/ionice)."""
        self._responses.append((prefix, Result(returncode, stdout, stderr)))

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.available else None

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        cmd = list(args)
        self.calls.append(cmd)
        core = cmd
        while core and core[0] in ("nice", "ionice"):
            core = core[3:]
        for prefix, result in reversed(self._responses):
            if tuple(core[: len(prefix)]) == prefix:
                return result
        return Result(0, "", "")
