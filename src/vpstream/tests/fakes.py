"""Fake runner so tests never call real programs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from vpstream.caddy import CaddyError
from vpstream.firewall import FirewallError
from vpstream.tools import Result, SystemRunner

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


FAKE_MKVMERGE = """\
#!{python}
import json, shutil, sys, time
from pathlib import Path
from vpstream.tests import mkvdata
mode = Path(sys.argv[0] + ".mode").read_text().strip()
Path(sys.argv[0] + ".calls").open("a").write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1] == "-J":
    kinds = ("video", "audio", "subtitles")
    tracks = [{{"id": n, "type": kind}} for n, kind in enumerate(kinds)]
    print(json.dumps({{"tracks": tracks}}))
    sys.exit(0)
out, source = Path(sys.argv[sys.argv.index("-o") + 1]), sys.argv[-1]
if mode == "fail":
    print("Error: the file could not be read")
    sys.exit(2)
if mode == "slow":
    out.write_bytes(b"half")
    time.sleep(30)
if mode == "same":
    shutil.copy(source, out)
else:
    mkvdata.write(out, ("video", "audio", "subtitles"), indexed=[1, 2])
"""


class FakeMkvmerge(SystemRunner):
    """A host whose mkvmerge is a stand-in: `mode` is "ok", "slow", "fail" or "same" (writes
    the file back unchanged). It has no nice or ionice, and runs nothing else."""

    def __init__(self, folder: Path, mode: str = "ok") -> None:
        import sys

        self.program = folder / "mkvmerge"
        self.program.write_text(FAKE_MKVMERGE.format(python=sys.executable))
        self.program.chmod(0o755)
        self.mode = mode

    @property
    def mode(self) -> str:
        return Path(f"{self.program}.mode").read_text()

    @mode.setter
    def mode(self, value: str) -> None:
        Path(f"{self.program}.mode").write_text(value)

    def calls(self) -> list[list[str]]:
        import json

        record = Path(f"{self.program}.calls")
        return (
            [json.loads(line) for line in record.read_text().splitlines()]
            if record.exists()
            else []
        )

    def which(self, name: str) -> str | None:
        return str(self.program) if name == "mkvmerge" else None

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        if not args or args[0] != "mkvmerge":
            return Result(127, "", f"{args[0] if args else ''}: not here")
        return super().run([str(self.program), *args[1:]], timeout=timeout)
