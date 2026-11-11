"""Fake runner so tests never call real programs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from vpstream.caddy import CaddyError
from vpstream.firewall import FirewallError
from vpstream.tools import Result

NAME = "host.example.ts.net"  # the fake host's name on its tailnet


def node_status(https: bool = True, state: str = "Running") -> dict[str, Any]:
    """What `tailscale status --json` prints, as far as vpstream reads it."""
    return {
        "BackendState": state,
        "Self": {"DNSName": NAME + "."},
        "CertDomains": [NAME] if https else [],
    }


def serve_status(ports: Mapping[int, str]) -> dict[str, Any]:
    """What `tailscale serve status --json` prints for HTTPS ports proxied to these targets."""
    if not ports:
        return {}
    web = {f"{NAME}:{p}": {"Handlers": {"/": {"Proxy": t}}} for p, t in ports.items()}
    return {"TCP": {str(p): {"HTTPS": True} for p in ports}, "Web": web}


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


class FakeServer:
    """Stands in for Caddy: records start and stop; `fail` makes every start raise."""

    def __init__(self, fail: str = "") -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.up = False

    def start(self) -> None:
        self.calls.append("start")
        if self.fail:
            raise CaddyError(self.fail)
        self.up = True

    def stop(self) -> None:
        self.calls.append("stop")
        self.up = False

    def running(self) -> bool:
        return self.up
