"""Fake runner so tests never call real programs."""

from __future__ import annotations

from collections.abc import Sequence

from vpstream.firewall import FirewallError
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


class FakeFirewall:
    """Records open and close calls; `fail` makes every call raise FirewallError."""

    def __init__(self, fail: str = "") -> None:
        self.calls: list[str] = []
        self.fail = fail

    def _call(self, action: str) -> None:
        self.calls.append(action)
        if self.fail:
            raise FirewallError(self.fail)

    def open(self) -> None:
        self._call("open")

    def close(self) -> None:
        self._call("close")
