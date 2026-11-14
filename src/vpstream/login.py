"""The password of the public dashboard: its verifier, the login, the sessions, the limits.

The password never crosses the wire, and neither does anything a listener could send again:

1. the browser asks for a challenge and gets a one-time random nonce, with the salt and the
   iteration count of the verifier;
2. it derives the key from the password with PBKDF2-HMAC-SHA256 (`derive_key`);
3. it answers with HMAC-SHA256(key, LABEL + nonce) (`proof`);
4. the server compares that with its own, in constant time, and burns the nonce whatever the
   answer, so a reply seen once can never be used again.

The verifier is the derived key itself. It does not give the password away, but it is
enough to log in, so it is kept in a file only the service can read (`Paths.dashboard_key`).

This holds against someone who can only listen. Someone who can change what the browser
gets, before the browser trusts the dashboard's certificate authority, can serve a login
page of their own and read the password typed into it; and a nonce with its answer, once
read, can be tried against guessed passwords offline (600,000 rounds each). So the
authority is to be installed on the phone before the first login (see `vpstream init`).

Failed logins are counted per address (per /64 for IPv6, which a viewer gets by the
million) and over all addresses, and are written down so a restart does not reset them. A
browser that logged in before carries a signed device cookie and is counted on its own, so
guessing from elsewhere never locks it out; `vpstream dash-unblock` lets a new one in.
Only the record is I/O here; Flask is in `api`.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vpstream.checks import is_finite, is_token
from vpstream.files import write_atomic
from vpstream.net import normal_ip

SCHEME = "pbkdf2-sha256"
ITERATIONS = 600_000  # OWASP's figure for PBKDF2-HMAC-SHA256
MIN_ITERATIONS = 1_000  # what a verifier file may hold; tests use few, init uses ITERATIONS
MAX_ITERATIONS = 10_000_000  # a browser must still finish the derivation
SALT_BYTES = 16
KEY_BYTES = 32
LABEL = "vpstream-login:"  # the proof is for this login, never for anything else signed

MIN_PASSWORD = 10
MAX_PASSWORD = 1024

COOKIE = "__Host-vpstream-dash"  # __Host-: only set over HTTPS, for this host and path /
DEVICE_COOKIE = "__Host-vpstream-device"  # a browser that logged in here before
DEVICE_SECONDS = 365 * 86400
DEVICE_LABEL = "vpstream-device:"
SECRET_BYTES = 32
CSRF_HEADER = "X-Vpstream-Csrf"
SESSION_SECONDS = 12 * 3600
MAX_SESSIONS = 16
NONCE_SECONDS = 120.0  # the slow pure-JavaScript derivation must fit
MAX_NONCES = 1024  # held by browsers not known yet; a known one has room of its own
MAX_NONCES_PER_ADDRESS = 4

FAIL_SECONDS = 15 * 60
FAILS_PER_ADDRESS = 5  # then that address waits until its oldest failure is FAIL_SECONDS old
ALL_FAIL_SECONDS = 3600
FAILS_ALL = 50  # from every address together: guessing from many addresses is slow too
MAX_ADDRESSES = 5000

_HEX64 = re.compile(r"[0-9a-f]{64}")
_DEVICE = re.compile(r"[A-Za-z0-9_-]{22}\.[0-9a-f]{64}")  # token_urlsafe(16), its signature


class PasswordError(ValueError):
    """A password that cannot be used, or a verifier file that cannot be read."""


class LoginRefused(Exception):
    """A login step that failed. `status` is the HTTP status it answers with."""

    def __init__(self, status: int, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


# --- the verifier


def normal_password(password: str) -> bytes:
    """The bytes the key is derived from. NFC, so "é" typed as one character or as e and an
    accent is the same password; the page does the same."""
    return unicodedata.normalize("NFC", password).encode("utf-8")


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"the password must have at least {MIN_PASSWORD} characters"
    if len(password) > MAX_PASSWORD:
        return f"the password must have at most {MAX_PASSWORD} characters"
    if any(unicodedata.category(c) == "Cc" for c in password):
        return "the password must not contain control characters"
    return None


def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", normal_password(password), salt, iterations, KEY_BYTES)


def proof(key: bytes, nonce: str) -> str:
    """What the browser answers a challenge with, as hex."""
    return hmac.new(key, (LABEL + nonce).encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Verifier:
    salt: bytes
    iterations: int
    key: bytes

    @classmethod
    def make(cls, password: str, iterations: int = ITERATIONS) -> Verifier:
        if problem := password_problem(password):
            raise PasswordError(problem)
        salt = secrets.token_bytes(SALT_BYTES)
        return cls(salt, iterations, derive_key(password, salt, iterations))

    def to_text(self) -> str:
        return f"{SCHEME}${self.iterations}${self.salt.hex()}${self.key.hex()}\n"

    @classmethod
    def parse(cls, text: str) -> Verifier:
        parts = text.strip().split("$")
        if len(parts) != 4 or parts[0] != SCHEME:
            raise PasswordError(f"not a {SCHEME} verifier")
        _, count, salt, key = parts
        if not (count.isascii() and count.isdecimal()):
            raise PasswordError("the iteration count is not a number")
        iterations = int(count)
        if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            raise PasswordError(f"the iteration count {iterations} is out of range")
        if not re.fullmatch(r"[0-9a-f]{16,128}", salt) or len(salt) % 2:
            raise PasswordError("the salt is not hex")
        if not _HEX64.fullmatch(key):
            raise PasswordError("the key is not 32 bytes of hex")
        return cls(bytes.fromhex(salt), iterations, bytes.fromhex(key))

    def matches(self, nonce: str, answer: Any) -> bool:
        """Whether `answer` is the proof for `nonce`. Anything but 64 hex digits is not, and
        is never compared: compare_digest takes ASCII text only."""
        if not isinstance(answer, str) or not _HEX64.fullmatch(answer):
            return False
        return hmac.compare_digest(proof(self.key, nonce), answer)


def load_verifier(path: Path) -> Verifier | None:
    """The verifier in `path`, or None when there is no file. Raises PasswordError when the
    file is there but cannot be used."""
    try:
        text = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PasswordError(f"cannot read {path}: {exc}") from exc
    try:
        return Verifier.parse(text)
    except PasswordError as exc:
        raise PasswordError(f"{path}: {exc}") from exc


# --- who a login is: a browser that logged in before, or an address


def address_key(ip: str | None) -> str:
    """What failures of a browser not known yet are counted under: its address, or the /64
    of an IPv6 address."""
    normal = normal_ip(ip)
    if normal is None:
        return "unknown"
    address = ipaddress.ip_address(normal)
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.IPv6Network(f"{address}/64", strict=False))
    return normal


@dataclass(frozen=True)
class Who:
    """Whom a login step is counted for. A known browser (`known`) carries a device cookie
    from an earlier login: it is counted on its own, so failures from other addresses,
    however many, never lock it out. Everyone else is counted by address and all together."""

    key: str
    known: bool = False


def _wait(times: list[float], limit: int, window: float, now: float) -> float:
    """How long until fewer than `limit` of these failures are within `window`."""
    if len(times) < limit:
        return 0.0
    return max(0.0, sorted(times)[-limit] + window - now)


@dataclass
class Limiter:
    """Failed logins, written to `path` after each change (None keeps them in memory).

    Times are wall-clock seconds, because they outlive the process. A clock set back makes
    a recorded time lie in the future; it is taken as now, so a failure never counts for
    longer than its window after the change.
    """

    path: Path | None = None
    clock: Callable[[], float] = time.time
    log: Callable[[str], None] = print
    _by_key: dict[str, list[float]] = field(default_factory=dict)
    _all: list[float] = field(default_factory=list)  # failures of browsers not known yet

    def __post_init__(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.log(f"WARNING: {self.path} cannot be read ({exc}); failed logins start at 0")
            return
        if not isinstance(data, dict):
            return
        keys = data.get("addresses")
        for key, times in keys.items() if isinstance(keys, dict) else ():
            if isinstance(key, str) and isinstance(times, list):
                self._by_key[key] = [t for t in times if is_finite(t)]
        everyone = data.get("all")
        self._all = [t for t in everyone if is_finite(t)] if isinstance(everyone, list) else []
        self._prune(self.clock())

    def _prune(self, now: float) -> None:
        def keep(times: list[float], window: float) -> list[float]:
            return [min(t, now) for t in times if now - t < window]

        self._all = keep(self._all, ALL_FAIL_SECONDS)[-FAILS_ALL:]
        for key in list(self._by_key):
            times = keep(self._by_key[key], FAIL_SECONDS)[-FAILS_PER_ADDRESS:]
            if times:
                self._by_key[key] = times
            else:
                del self._by_key[key]
        while len(self._by_key) > MAX_ADDRESSES:  # the longest quiet goes first
            del self._by_key[min(self._by_key, key=lambda k: self._by_key[k][-1])]

    def _save(self) -> None:
        if self.path is None:
            return
        data = {"addresses": self._by_key, "all": self._all}
        try:
            write_atomic(self.path, json.dumps(data))
        except OSError as exc:
            self.log(f"WARNING: failed logins could not be written to {self.path}: {exc}")

    def wait(self, who: Who) -> float:
        """Seconds until `who` may try again; 0 when it may now."""
        now = self.clock()
        self._prune(now)
        mine = _wait(self._by_key.get(who.key, []), FAILS_PER_ADDRESS, FAIL_SECONDS, now)
        if who.known:
            return mine
        return max(mine, _wait(self._all, FAILS_ALL, ALL_FAIL_SECONDS, now))

    def failed(self, who: Who) -> None:
        now = self.clock()
        self._by_key.setdefault(who.key, []).append(now)
        if not who.known:
            self._all.append(now)
        self._prune(now)
        self._save()

    def succeeded(self, who: Who) -> None:
        """A right password clears the failures of `who`, not everyone's."""
        if self._by_key.pop(who.key, None) is not None:
            self._save()

    def clear(self) -> None:
        """`vpstream dash-unblock`, and a new password: every count starts again."""
        self._by_key.clear()
        self._all.clear()
        self._save()


# --- the login itself


@dataclass
class Session:
    expires: float  # on the monotonic clock
    csrf: str


@dataclass(frozen=True)
class Granted:
    """A login that succeeded: the session's cookie and the session, and the device cookie
    that makes this browser known from now on."""

    token: str
    session: Session
    device: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def load_secret(path: Path) -> bytes:
    """The key device cookies are signed with, made the first time. Raises OSError."""
    try:
        secret = path.read_bytes()
        if len(secret) == SECRET_BYTES:
            return secret
    except FileNotFoundError:
        pass
    secret = secrets.token_bytes(SECRET_BYTES)
    _write_private(path, secret)
    return secret


@dataclass
class Login:
    """Challenges and sessions live in memory: a restart logs everyone out. `verifier` None
    means no password is set: nobody gets in. `device_secret` signs the device cookies."""

    verifier: Verifier | None
    limiter: Limiter = field(default_factory=Limiter)
    clock: Callable[[], float] = time.monotonic
    device_secret: bytes = field(default_factory=lambda: secrets.token_bytes(SECRET_BYTES))
    _nonces: dict[str, tuple[float, Who]] = field(default_factory=dict)  # nonce -> (by, who)
    _sessions: dict[str, Session] = field(default_factory=dict)  # hash of the cookie -> it
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- devices

    def _sign(self, device_id: str) -> str:
        message = (DEVICE_LABEL + device_id).encode("ascii")
        return hmac.new(self.device_secret, message, hashlib.sha256).hexdigest()

    def _device(self, cookie: str | None) -> str | None:
        """The id of the device this cookie was given to, when it is one this daemon signed."""
        if not isinstance(cookie, str) or not _DEVICE.fullmatch(cookie):
            return None
        device_id, signature = cookie.split(".")
        return device_id if hmac.compare_digest(self._sign(device_id), signature) else None

    def who(self, ip: str | None, device: str | None) -> Who:
        found = self._device(device)
        return Who(f"device:{found}", True) if found else Who(address_key(ip))

    # --- the steps

    def _check(self, who: Who) -> Verifier:
        if self.verifier is None:
            raise LoginRefused(503, "no password is set: sudo vpstream dash-pass")
        wait = self.limiter.wait(who)
        if wait > 0:
            minutes = max(1, round(wait / 60))
            raise LoginRefused(
                429,
                f"too many wrong passwords; try again in about {minutes} min",
                retry_after=wait,
            )
        return self.verifier

    def challenge(self, ip: str | None, device: str | None = None) -> dict[str, Any]:
        """A new nonce, with what the key is derived with.

        A nonce given out is never taken back for someone else: when browsers not known yet
        hold MAX_NONCES, the next one is refused until some expire. A known browser does not
        count towards that, so a flood of challenges cannot keep it from logging in.
        """
        with self._lock:
            who = self.who(ip, device)
            verifier = self._check(who)
            now = self.clock()
            self._nonces = {n: v for n, v in self._nonces.items() if v[0] > now}
            mine = [n for n, v in self._nonces.items() if v[1] == who]
            for old in mine[: max(0, len(mine) - MAX_NONCES_PER_ADDRESS + 1)]:
                del self._nonces[old]
            strangers = sum(1 for v in self._nonces.values() if not v[1].known)
            if not who.known and strangers >= MAX_NONCES:
                raise LoginRefused(503, "too many logins at once; try again in a minute", 60.0)
            nonce = secrets.token_hex(32)
            self._nonces[nonce] = (now + NONCE_SECONDS, who)
            return {
                "nonce": nonce,
                "salt": verifier.salt.hex(),
                "iterations": verifier.iterations,
                "label": LABEL,
            }

    def login(self, ip: str | None, nonce: Any, answer: Any, device: str | None = None) -> Granted:
        """Check an answer to a challenge.

        The nonce is burned before the answer is looked at, so each one is tried at most
        once, right or wrong. Only an answer to a live nonce counts as a failure: without
        one, nothing about the password was tried.
        """
        with self._lock:
            who = self.who(ip, device)
            verifier = self._check(who)
            issued = self._nonces.pop(nonce, None) if isinstance(nonce, str) else None
            if issued is None or issued[0] <= self.clock():
                raise LoginRefused(400, "the login page was open too long; try again")
            if not verifier.matches(nonce, answer):
                self.limiter.failed(who)
                raise LoginRefused(401, "wrong password")
            self.limiter.succeeded(who)
            device_id = who.key.removeprefix("device:") if who.known else secrets.token_urlsafe(16)
            token, session = self._new_session()
            return Granted(token, session, f"{device_id}.{self._sign(device_id)}")

    def _new_session(self) -> tuple[str, Session]:
        now = self.clock()
        self._sessions = {h: s for h, s in self._sessions.items() if s.expires > now}
        while len(self._sessions) >= MAX_SESSIONS:  # the oldest goes
            del self._sessions[min(self._sessions, key=lambda h: self._sessions[h].expires)]
        token = secrets.token_urlsafe(32)
        session = Session(now + SESSION_SECONDS, secrets.token_urlsafe(32))
        self._sessions[_hash(token)] = session
        return token, session

    def session(self, cookie: str | None) -> Session | None:
        """The live session this cookie belongs to, or None."""
        if not is_token(cookie):
            return None
        with self._lock:
            found = self._sessions.get(_hash(cookie))
            if found is None:
                return None
            if found.expires <= self.clock():
                del self._sessions[_hash(cookie)]
                return None
            return found

    def logout(self, cookie: str | None) -> None:
        if is_token(cookie):
            with self._lock:
                self._sessions.pop(_hash(cookie), None)

    def seconds_left(self, session: Session) -> float:
        return max(0.0, session.expires - self.clock())

    def unblock(self) -> None:
        """Forget every failed login and every challenge given out."""
        with self._lock:
            self.limiter.clear()
            self._nonces.clear()

    def reset(self, verifier: Verifier | None, device_secret: bytes) -> None:
        """A new password: every session ends, every device must log in again, and the
        old password's failures are forgotten."""
        with self._lock:
            self.verifier = verifier
            self.device_secret = device_secret
            self._sessions.clear()
            self._nonces.clear()
            self.limiter.clear()


def csrf_ok(session: Session, sent: str | None) -> bool:
    """Whether a change carries the session's token. Compared as bytes, in constant time."""
    if not isinstance(sent, str):
        return False
    return hmac.compare_digest(session.csrf.encode(), sent.encode("utf-8", "surrogateescape"))


def _write_private(
    path: Path, data: bytes, before: Callable[[Path], None] = lambda tmp: None
) -> None:
    """Write a file whole, never readable by others even for a moment. `before` gets the
    finished temporary file, to give it to its owner, before it takes the file's place: a
    write cut off at any point leaves the old file or the new one, each with its owner."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    try:
        before(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_verifier(
    path: Path, verifier: Verifier, before: Callable[[Path], None] = lambda tmp: None
) -> None:
    """Write the verifier (see `_write_private`; init gives it to the service's group)."""
    _write_private(path, verifier.to_text().encode("ascii"), before)
