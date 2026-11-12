"""Who may fetch a stream: the three policies and the device table. No I/O happens here.

A device is a player known by its IP address, or a browser known by its cookie. Players such
as VLC keep no cookie, so the address is all there is to tell them apart. A browser in browser
mode gets a random cookie on its first page and is known by it from then on: a changed address
does not send it back to the waiting list, and a stranger behind the same address (a carrier's
shared IP) is another device, which waits. The user agent is kept to show which player it was.

Only a hash of each cookie is kept, so the state file holds nothing a viewer could send.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

from vpstream.checks import is_finite
from vpstream.clients import Client, name_value
from vpstream.net import normal_ip

APPROVAL = "approval"  # only approved devices; the others wait on the list, marked WL
OPEN = "open"  # everyone, except the devices that were disabled
LOCKED = "locked"  # only enabled devices
POLICIES = (APPROVAL, OPEN, LOCKED)
PLAYER, BROWSER = "player", "browser"  # known by IP address, known by cookie

MAX_DEVICES = 500  # a port scan must not grow the table without end
MAX_UNCONFIRMED = 3  # browsers per address whose cookie has not come back yet
MAX_UNDECIDED = 8  # browsers per address nobody decided about: one address cannot fill the table
MAX_USER_AGENT = 200
MAX_MISSES = 20  # wrong addresses one viewer may try before it is shut out as a guesser
MAX_GUESSERS = 5000
COOKIE = "vpstream"
_COOKIE_VALUE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32)
_BROWSER_ID = re.compile(r"b-[0-9a-f]{6}")
_HASH = re.compile(r"[0-9a-f]{64}")


class GateError(ValueError):
    pass


def allows(policy: str, allowed: bool | None) -> bool:
    """Whether a device gets in. `allowed` is True (enabled or approved), False (disabled or
    denied) or None (nobody decided yet)."""
    if policy == OPEN:
        return allowed is not False
    return allowed is True


def _hash(cookie: str) -> str:
    return hashlib.sha256(cookie.encode("ascii")).hexdigest()


@dataclass
class Device:
    ip: str  # for a browser, the address it came from last
    allowed: bool | None = None
    wl: bool = False  # it hit the waiting list at least once
    user_agent: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    requests: int = 0
    blocked: int = 0
    bytes: int = 0
    code: str = ""  # shown on the waiting page, to tell this device apart on the list
    id: str = ""  # the address for a player; b-XXXXXX for a browser
    kind: str = PLAYER
    token: str = ""  # a browser's cookie, hashed
    confirmed: bool = False  # the browser sent its cookie back at least once
    name: str = ""  # what the owner calls it
    client: Client | None = None  # what its player reports and is asked (browser mode)

    def __post_init__(self) -> None:
        self.id = self.id or self.ip

    def to_dict(self, private: bool = False, now: float = 0.0) -> dict[str, Any]:
        """As plain data. Only the state file gets the cookie's hash (`private`); the
        dashboard gets whether its player plays at `now`."""
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "ip": self.ip,
            "name": self.name,
            "allowed": self.allowed,
            "wl": self.wl,
            "user_agent": self.user_agent,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "requests": self.requests,
            "blocked": self.blocked,
            "bytes": self.bytes,
            "code": self.code,
            "confirmed": self.confirmed,
        }
        if private:
            data["token"] = self.token
            data["client"] = self.client.to_dict() if self.client else None
        else:
            data["client"] = self.client.describe(now) if self.client else None
        return data


@dataclass
class Admission:
    """What the gate made of one request."""

    ok: bool
    device: Device | None = None
    cookie: str = ""  # a new cookie to send the browser; empty for none


def new_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


@dataclass
class Devices:
    """The device table of one stream."""

    _rows: dict[str, Device] = field(default_factory=dict)  # id -> device
    _tokens: dict[str, str] = field(default_factory=dict)  # cookie hash -> id
    _misses: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, key: str) -> Device | None:
        """A device by its id (for a player, its address)."""
        return self._rows.get(key)

    def browser(self, cookie: str | None) -> Device | None:
        """The browser that holds this cookie, or None."""
        if not cookie or not _COOKIE_VALUE.fullmatch(cookie):
            return None
        found = self._tokens.get(_hash(cookie))
        return self._rows.get(found) if found else None

    def browsers(self) -> list[Device]:
        return [row for row in self._rows.values() if row.kind == BROWSER]

    def _add(self, row: Device) -> None:
        self._rows[row.id] = row
        if row.token:
            self._tokens[row.token] = row.id

    def _drop(self, row: Device) -> None:
        del self._rows[row.id]
        self._tokens.pop(row.token, None)

    def _make_room(self) -> bool:
        """Drop an undecided device: a browser whose cookie never came back first, then
        the one seen longest ago. False when every device has a decision, which are never
        dropped."""
        undecided = [d for d in self._rows.values() if d.allowed is None]
        if not undecided:
            return False
        self._drop(min(undecided, key=lambda d: (d.kind != BROWSER or d.confirmed, d.last_seen)))
        return True

    def _has_room(self) -> bool:
        return len(self._rows) < MAX_DEVICES or self._make_room()

    def _player(self, ip: str, now: float) -> Device | None:
        row = self._rows.get(ip)
        if row is None and self._has_room():
            row = Device(ip, first_seen=now)
            self._add(row)
        return row

    def _at(self, ip: str) -> list[Device]:
        """The browsers that came from `ip` last."""
        return [d for d in self.browsers() if d.ip == ip]

    def _new_browser(self, ip: str, now: float) -> tuple[Device | None, str]:
        """A browser seen for the first time, with the cookie to send it.

        One that keeps no cookie would be new on every page, and one that sends each cookie
        back once can make as many as it likes; so few undecided browsers are kept per
        address, and the one that did least goes first. An address cannot push the viewers
        of other addresses out of the table that way."""
        undecided = [d for d in self._at(ip) if d.allowed is None]
        fresh = [d for d in undecided if not d.confirmed]
        if len(fresh) >= MAX_UNCONFIRMED:
            self._drop(min(fresh, key=lambda d: d.last_seen))
        elif len(undecided) >= MAX_UNDECIDED:
            self._drop(min(undecided, key=lambda d: (d.requests, d.last_seen)))
        if not self._has_room():
            return None, ""
        cookie = secrets.token_urlsafe(32)
        while (key := f"b-{secrets.token_hex(3)}") in self._rows:
            pass
        row = Device(ip, first_seen=now, id=key, kind=BROWSER, token=_hash(cookie))
        # The owner shut this address, or a browser that came from it, out: a new browser
        # there is not let in either, so a disabled one cannot come back by dropping its
        # cookie. An address let in is not enough for a browser: it may be a stranger.
        player = self._rows.get(ip)
        if any(d.allowed is False for d in [*([player] if player else []), *self._at(ip)]):
            row.allowed = False
        self._add(row)
        return row, cookie

    def admit(
        self,
        policy: str,
        ip: str,
        user_agent: str,
        now: float,
        cookie: str | None = None,
        browser: bool = False,
    ) -> Admission:
        """Count one request and say whether the policy lets it in. A request with a known
        cookie is that browser's, from wherever it comes. A `browser` (a page asked for in
        browser mode) without one is a new browser and gets a cookie; anything else is the
        player at its address."""
        issued = ""
        row = self.browser(cookie)
        if row is not None:
            row.ip, row.confirmed = ip, True
        elif browser:
            row, issued = self._new_browser(ip, now)
        else:
            row = self._player(ip, now)
        if row is None:
            # The table is full of decided devices and this one is not among them. It could
            # not be listed or disabled, so it stays out, whatever the policy.
            return Admission(False)
        ok = allows(policy, row.allowed)
        row.last_seen = now
        row.requests += 1
        row.user_agent = user_agent[:MAX_USER_AGENT]
        if not ok:
            row.blocked += 1
            if policy == APPROVAL and row.allowed is None:
                row.wl = True
                row.code = row.code or new_code()
        return Admission(ok, row, issued)

    def find(self, key: str, now: float) -> Device:
        """A device by its id or its address. An address that was never seen is listed now,
        so it can be decided ahead of its first visit."""
        row = self._rows.get(key)
        if row is not None:
            return row
        normal = normal_ip(key)
        if normal is None:
            raise GateError(f"not a device or an IP address: {key!r}")
        row = self._player(normal, now)
        if row is None:
            raise GateError(f"the device table is full ({MAX_DEVICES} decided devices)")
        return row

    def decide(self, key: str, allowed: bool, now: float) -> Device:
        """Enable (or approve) or disable (or deny) one device, by its id or address.

        By address it is the player there, and the browsers that came from there last as
        well: disabled all of them, or enabled the ones nobody decided about yet (those that
        wait). A browser that comes there later still waits."""
        row = self.find(key, now)
        row.allowed = allowed
        if row.kind == PLAYER:
            for browser in self._at(row.ip):
                if not allowed or browser.allowed is None:
                    browser.allowed = allowed
        if allowed:
            self._misses.pop(row.ip, None)
        return row

    # --- viewers that try addresses until one works

    def passes(self, policy: str, ip: str, cookie: str | None = None) -> bool:
        """Whether a device that is already listed gets in. Nothing is counted."""
        row = self.browser(cookie) or self._rows.get(ip)
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
        players that are connected and shuts out only what comes after.

        A browser whose cookie never came back (a scanner, a link preview) is not kept.
        """
        for row in self._rows.values():
            if row.kind == BROWSER and not row.confirmed:
                continue
            if row.allowed is None and allows(policy, None):
                row.allowed = True

    def add_bytes(self, ip: str, count: int) -> None:
        """Count bytes sent to an address: to its player, else to the browser that came
        from it last. The access log knows no cookies."""
        normal = normal_ip(ip) or ""
        row = self._rows.get(normal)
        if row is None:
            from_there = [d for d in self.browsers() if d.ip == normal]
            row = max(from_there, key=lambda d: d.last_seen, default=None)
        if row is not None:
            row.bytes += count

    def rows(self, private: bool = False, now: float = 0.0) -> list[dict[str, Any]]:
        """Every device as plain data, the one seen last first."""
        ordered = sorted(self._rows.values(), key=lambda d: d.last_seen, reverse=True)
        return [row.to_dict(private, now) for row in ordered]

    @classmethod
    def from_rows(cls, rows: Any, videos: int = 0) -> Devices:
        """Read back what `rows(private=True)` gave, for a stream of `videos` videos. Rows
        that do not fit are skipped."""
        table = cls()
        for data in rows if isinstance(rows, list) else []:
            row = _row(data, videos) if isinstance(data, dict) else None
            if row is not None and row.id not in table._rows:
                table._add(row)
        return table


def _row(data: dict[str, Any], videos: int) -> Device | None:
    ip = normal_ip(data.get("ip"))
    if ip is None:
        return None
    if data.get("kind") == BROWSER:
        key, token = data.get("id"), data.get("token")
        if not (isinstance(key, str) and _BROWSER_ID.fullmatch(key)):
            return None
        if not (isinstance(token, str) and _HASH.fullmatch(token)):
            return None
        row = Device(ip, id=key, kind=BROWSER, token=token, confirmed=data.get("confirmed") is True)
        row.client = Client.from_dict(data.get("client"), videos)
    elif data.get("id", ip) == ip:  # a row recorded before 0.11 has no id
        row = Device(ip)
    else:
        return None
    allowed = data.get("allowed")
    row.allowed = allowed if isinstance(allowed, bool) else None
    row.wl = data.get("wl") is True
    row.user_agent = str(data.get("user_agent") or "")[:MAX_USER_AGENT]
    code = data.get("code")
    row.code = code if isinstance(code, str) and re.fullmatch(r"\d{4}", code) else ""
    name = data.get("name")
    row.name = name_value(name) if isinstance(name, str) else ""
    for attr in ("first_seen", "last_seen"):
        value = data.get(attr)
        setattr(row, attr, float(value) if is_finite(value) else 0.0)
    for attr in ("requests", "blocked", "bytes"):
        value = data.get(attr)
        setattr(row, attr, int(value) if is_finite(value) else 0)
    return row
