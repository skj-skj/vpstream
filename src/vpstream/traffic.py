"""How many bytes each viewer got, for the device table.

Caddy's access log is not enough on its own: Caddy 2.6.2, the version in Ubuntu 24.04, logs
about 512 bytes for a transfer the player hangs up on, and a player hangs up on every seek
and every close. The kernel knows what each connection really delivered, so it is asked
once a second with `ss` (no root needed). A connection that opened and closed between two
looks is never seen there; for those the access log's count is taken, matched by the
viewer's port. Where `ss` cannot be used (Termux), the access log is all there is, and Caddy
versions that count a cut-off transfer correctly make that enough.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from itertools import pairwise
from pathlib import Path

from vpstream.caddy import LogTail
from vpstream.net import normal_ip
from vpstream.tools import Runner

TOOL = "ss"
SEEN_SECONDS = 120.0  # a closed connection's log lines can come after it is gone
_ACKED = re.compile(r"\bbytes_acked:(\d+)")

Connection = tuple[str, str]  # the viewer's address and port


def ss_args(port: int) -> list[str]:
    return [TOOL, "-t", "-i", "-n", "-H", "state", "established", f"( sport = :{port} )"]


def parse(text: str) -> dict[Connection, int]:
    """Connection -> bytes the viewer has acknowledged, from what `ss -tinH` prints: a line
    with the queues and both ends, then an indented line of details."""
    found: dict[Connection, int] = {}
    lines = text.splitlines()
    for head, info in pairwise(lines):
        parts = head.split()
        if head[:1].isspace() or len(parts) < 4 or not info[:1].isspace():
            continue
        host, _, port = parts[3].rpartition(":")
        ip = normal_ip(host.strip("[]"))
        if ip is None or not port.isdigit():
            continue
        acked = _ACKED.search(info)
        found[(ip, port)] = int(acked[1]) if acked else 0
    return found


class Traffic:
    """Bytes sent to each viewer since the last look, as (address, bytes) pairs."""

    def __init__(
        self,
        runner: Runner,
        port: int,
        access_log: Path,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runner = runner
        self.port = port
        self.log = LogTail(access_log)
        self.clock = clock
        self._acked: dict[Connection, int] = {}  # open connections, as at the last look
        self._seen: dict[Connection, float] = {}  # connections the kernel counts
        self._logged: dict[Connection, int] = {}  # counted from the log before they were seen
        self.kernel = bool(runner.which(TOOL))

    def _sample(self) -> dict[Connection, int] | None:
        if not self.kernel:
            return None
        res = self.runner.run(ss_args(self.port), timeout=5)
        if res.returncode != 0:
            self.kernel = False  # not here (no netlink in Termux): the log it is, from now on
            return None
        return parse(res.stdout)

    def read(self) -> Iterator[tuple[str, int]]:
        now = self.clock()
        current = self._sample()
        if current is not None:
            for conn, acked in current.items():
                if conn in self._acked and acked >= self._acked[conn]:
                    sent = acked - self._acked[conn]
                else:  # new, or a new connection on a port used before: all of it is new
                    sent = acked - self._logged.pop(conn, 0)
                if sent > 0:
                    yield conn[0], sent
                self._seen[conn] = now
            self._acked = current
            for conn, when in list(self._seen.items()):
                if now - when > SEEN_SECONDS:
                    del self._seen[conn]
        for ip, port, size in self.log.read():
            conn = (ip, port)
            if conn in self._seen:
                continue  # the kernel has counted it, and correctly
            self._logged[conn] = self._logged.get(conn, 0) + size
            yield ip, size
