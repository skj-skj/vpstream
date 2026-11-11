"""Who may fetch a stream: the three policies and the device table. No I/O happens here.

A device is an IP address. Players such as VLC keep no cookie, so the address is all there is
to tell them apart; the user agent is kept to show which player it was.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass
from typing import Any

from vpstream.net import normal_ip

APPROVAL = "approval"  # only approved devices; the others wait on the list, marked WL
OPEN = "open"  # everyone, except the devices that were disabled
LOCKED = "locked"  # only enabled devices
POLICIES = (APPROVAL, OPEN, LOCKED)

MAX_DEVICES = 500  # a port scan must not grow the table without end
MAX_USER_AGENT = 200
MAX_MISSES = 20  # wrong addresses one viewer may try before it is shut out as a guesser
MAX_GUESSERS = 5000


class GateError(ValueError):
    pass


def allows(policy: str, allowed: bool | None) -> bool:
    """Whether a device gets in. `allowed` is True (enabled or approved), False (disabled or
    denied) or None (nobody decided yet)."""
    if policy == OPEN:
        return allowed is not False
    return allowed is True


@dataclass
class Device:
    ip: str
    allowed: bool | None = None
    wl: bool = False  # it hit the waiting list at least once
    user_agent: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    requests: int = 0
    blocked: int = 0
    bytes: int = 0
    code: str = ""  # shown on the waiting page, to tell this device apart on the list


def new_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


class Devices:
    """The device table of one stream."""

    def __init__(self, rows: list[Device] | None = None) -> None:
        self._rows = {row.ip: row for row in rows or []}
        self._misses: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, ip: str) -> Device | None:
        return self._rows.get(ip)

    def _make_room(self) -> bool:
        """Drop the undecided device that was seen longest ago. False when every device has
        a decision, which are never dropped."""
        undecided = [d for d in self._rows.values() if d.allowed is None]
        if not undecided:
            return False
        del self._rows[min(undecided, key=lambda d: d.last_seen).ip]
        return True

    def _row(self, ip: str, now: float) -> Device | None:
        row = self._rows.get(ip)
        if row is None and (len(self._rows) < MAX_DEVICES or self._make_room()):
            row = self._rows[ip] = Device(ip, first_seen=now)
        return row

    def visit(self, policy: str, ip: str, user_agent: str, now: float) -> bool:
        """Count one request from `ip` and say whether the policy lets it in."""
        row = self._row(ip, now)
        if row is None:
            # The table is full of decided devices and this one is not among them. It could
            # not be listed or disabled, so it stays out, whatever the policy.
            return False
        ok = allows(policy, row.allowed)
        row.last_seen = now
        row.requests += 1
        row.user_agent = user_agent[:MAX_USER_AGENT]
        if not ok:
            row.blocked += 1
            if policy == APPROVAL and row.allowed is None:
                row.wl = True
                row.code = row.code or new_code()
        return ok

    def decide(self, ip: str, allowed: bool, now: float) -> Device:
        """Enable (or approve) or disable (or deny) one address. An address that was never
        seen can be decided ahead of its first visit."""
        normal = normal_ip(ip)
        if normal is None:
            raise GateError(f"not an IP address: {ip!r}")
        row = self._row(normal, now)
        if row is None:
            raise GateError(f"the device table is full ({MAX_DEVICES} decided devices)")
        row.allowed = allowed
        if allowed:
            self._misses.pop(normal, None)
        return row

    # --- viewers that try addresses until one works

    def passes(self, policy: str, ip: str) -> bool:
        """Whether a device that is already listed gets in. Nothing is counted."""
        row = self._rows.get(ip)
        return row is not None and allows(policy, row.allowed)

    def miss(self, ip: str) -> None:
        """Count a request for an address that does not exist."""
        if ip in self._misses or len(self._misses) < MAX_GUESSERS:
            self._misses[ip] = self._misses.get(ip, 0) + 1

    def guesser(self, ip: str) -> bool:
        return self._misses.get(ip, 0) >= MAX_MISSES

    def guessers(self) -> list[str]:
        return sorted(ip for ip in self._misses if self.guesser(ip))

    def keep_current(self, policy: str) -> None:
        """Before a lock: every device that gets in now is enabled, so the lock keeps the
        players that are connected and shuts out only what comes after."""
        for row in self._rows.values():
            if row.allowed is None and allows(policy, None):
                row.allowed = True

    def add_bytes(self, ip: str, count: int) -> None:
        row = self._rows.get(normal_ip(ip) or "")
        if row is not None:
            row.bytes += count

    def rows(self) -> list[dict[str, Any]]:
        """Every device as plain data, the one seen last first."""
        ordered = sorted(self._rows.values(), key=lambda d: d.last_seen, reverse=True)
        return [asdict(row) for row in ordered]

    @classmethod
    def from_rows(cls, rows: Any) -> Devices:
        """Read back what `rows` gave. Rows that do not fit are skipped."""
        table = cls()
        for data in rows if isinstance(rows, list) else []:
            ip = normal_ip(data.get("ip")) if isinstance(data, dict) else None
            if ip is None:
                continue
            allowed = data.get("allowed")
            row = Device(ip, allowed if isinstance(allowed, bool) else None, data.get("wl") is True)
            row.user_agent = str(data.get("user_agent") or "")[:MAX_USER_AGENT]
            code = data.get("code")
            row.code = code if isinstance(code, str) and re.fullmatch(r"\d{4}", code) else ""
            for name in ("first_seen", "last_seen"):
                value = data.get(name)
                setattr(row, name, float(value) if _number(value) else 0.0)
            for name in ("requests", "blocked", "bytes"):
                value = data.get(name)
                setattr(row, name, int(value) if _number(value) else 0)
            table._rows[ip] = row
        return table


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
