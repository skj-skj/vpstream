"""The stream's HTTPS certificate: a Let's Encrypt certificate for the stream's address.

An address can only have a short-lived certificate (the `shortlived` profile, 160 hours),
and Let's Encrypt limits how many it issues for one address in a week. So a certificate is
kept and used again for as long as it lasts past the end of the stream that starts; only
when it does not is a new one requested, before the stream's Caddy starts. Nothing changes
while a stream runs, which is why an HTTPS stream's time limit fits in a new certificate's
life (LONGEST_STREAM_SECONDS).

A request opens port 80 (where Let's Encrypt checks) through the firewall helper only while
it lasts, and runs a Caddy of its own for it (`caddy.CertificateCaddy`). Caddy keeps the
certificate in its storage under `Paths.certificate_dir`; the stream's Caddy loads the files
from there and never asks for one itself. A request that fails leaves the old certificate as
it was.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import ipaddress
import json
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vpstream.config import LETS_ENCRYPT, LETS_ENCRYPT_STAGING, Config, Paths
from vpstream.firewall import Firewall, FirewallError
from vpstream.net import is_ipv4

PROFILE = "shortlived"  # the only Let's Encrypt profile that allows an address
# A certificate must last this much past the end of a stream, so a clock that is a little
# off, or a stream that ends a little late, does not show a browser an expired one.
MARGIN_SECONDS = 10 * 60
# A new certificate is good for 160 hours from an hour before it is issued (Let's Encrypt
# dates it back). The longest HTTPS stream leaves room for that, the margin, and a slow clock.
LONGEST_STREAM_SECONDS = 156 * 3600


class CertificateError(RuntimeError):
    """No certificate that lasts the stream could be had."""


_PEM = re.compile(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)


def first_der(pem: str) -> bytes:
    """The first certificate in `pem`, as DER. Raises ValueError when there is none."""
    found = _PEM.search(pem)
    if not found:
        raise ValueError("no certificate in the file")
    try:
        return base64.b64decode("".join(found.group(1).split()), validate=True)
    except binascii.Error as exc:
        raise ValueError(f"the certificate is not valid base64: {exc}") from exc


def _element(der: bytes, at: int) -> tuple[int, int, int]:
    """The DER element at `at`: its tag, where its content starts and where it ends."""
    tag, size = der[at], der[at + 1]
    start = at + 2
    if size & 0x80:
        count = size & 0x7F
        if not 1 <= count <= 4:
            raise ValueError("a DER length this long is not used in a certificate")
        size = int.from_bytes(der[start : start + count], "big")
        start += count
    end = start + size
    if end > len(der):
        raise ValueError("the certificate is cut short")
    return tag, start, end


_TIMES = {0x17: ("%y%m%d%H%M%SZ", 13), 0x18: ("%Y%m%d%H%M%SZ", 15)}  # UTCTime, GeneralizedTime


def _time(der: bytes, at: int) -> tuple[float, int]:
    tag, start, end = _element(der, at)
    if tag not in _TIMES:
        raise ValueError("the certificate's validity is not a time")
    form, size = _TIMES[tag]
    text = der[start:end].decode("ascii")
    if len(text) != size:
        raise ValueError(f"not a certificate time: {text!r}")
    return float(calendar.timegm(time.strptime(text, form))), end


def validity(pem: str) -> tuple[float, float]:
    """When the first certificate in `pem` becomes valid and when it expires, in seconds
    since the epoch. Raises ValueError when it cannot be read."""
    der = first_der(pem)
    try:
        _, start, _ = _element(der, 0)  # Certificate
        _, at, _ = _element(der, start)  # TBSCertificate
        tag, _, end = _element(der, at)
        if tag == 0xA0:  # [0] version, present in every v3 certificate
            at = end
        for _ in range(3):  # serial number, signature algorithm, issuer
            at = _element(der, at)[2]
        tag, at, _ = _element(der, at)
        if tag != 0x30:
            raise ValueError("no validity in the certificate")
        not_before, at = _time(der, at)
        not_after, _ = _time(der, at)
    except (IndexError, UnicodeDecodeError) as exc:
        raise ValueError(f"the certificate cannot be read: {exc}") from exc
    return not_before, not_after


@dataclass(frozen=True)
class Certificate:
    host: str
    cert_file: Path
    key_file: Path
    not_before: float
    not_after: float

    def lasts(self, until: float) -> bool:
        """Whether it is still good, with a margin, at `until`."""
        return self.not_after - MARGIN_SECONDS >= until

    def describe(self) -> dict[str, Any]:
        return {"host": self.host, "not_before": self.not_before, "expires": self.not_after}


def stored(store: Path, ca: str, host: str) -> Certificate | None:
    """The certificate for `host` from the authority `ca` that Caddy keeps in `store` (its
    storage folder), or None when there is none that can be used. Caddy keeps one folder
    per authority, and its own record of each certificate names the authority and the
    addresses."""
    found = []
    for meta_file in store.glob(f"certificates/*/{host}/{host}.json"):
        folder = meta_file.parent
        cert_file, key_file = folder / f"{host}.crt", folder / f"{host}.key"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if meta["issuer_data"]["ca"] != ca or meta["sans"] != [host]:
                continue
            if not key_file.is_file():
                continue
            not_before, not_after = validity(cert_file.read_text(encoding="ascii"))
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
            continue  # Caddy is writing it, or it is not one of ours
        found.append(Certificate(host, cert_file, key_file, not_before, not_after))
    return max(found, key=lambda c: c.not_after, default=None)


class Issuer(Protocol):
    """What requests a certificate (a Caddy of its own). Tests swap in a fake."""

    def problem(self) -> str | None:
        """Why it cannot request one at all (a Caddy without ACME profiles), or None."""
        ...

    def obtain(self, host: str, done: Callable[[], bool]) -> None:
        """Request a certificate for `host` and return once `done()` says it is stored.
        Raises CertificateError with the authority's reason when it fails."""
        ...


# Let's Encrypt issues for public addresses only; another authority (Pebble) may not care.
_PUBLIC_ONLY = (LETS_ENCRYPT, LETS_ENCRYPT_STAGING)


def address_problem(host: str, ca: str) -> str | None:
    """Why `ca` cannot give `host` a certificate, or None: Let's Encrypt refuses an address
    that is not a public IPv4 one (a private, CGNAT or loopback address)."""
    if not is_ipv4(host):
        return f"{host or 'the address'} is not an IPv4 address"
    if ca in _PUBLIC_ONLY and not ipaddress.ip_address(host).is_global:
        return f"{host} is not a public address, and Let's Encrypt certifies public ones only"
    return None


@dataclass
class Certificates:
    """The stream's certificate: kept, used again, and requested when it does not last."""

    config: Config
    paths: Paths
    issuer: Issuer
    firewall: Firewall | None = None  # port 80; None in a trial run, where nothing is opened
    log: Callable[[str], None] = print
    clock: Callable[[], float] = time.time

    @property
    def store(self) -> Path:
        """Caddy's storage: XDG_DATA_HOME is its `data` folder, and Caddy adds `caddy`."""
        return self.paths.certificate_dir / "data" / "caddy"

    @property
    def _previous(self) -> Path:
        """Where a kept certificate waits while a new one is requested, at the same place
        under it as in the store."""
        return self.paths.certificate_dir / "previous"

    def current(self, host: str) -> Certificate | None:
        return stored(self.store, self.config.acme_ca, host)

    def status(self, host: str) -> dict[str, Any] | None:
        cert = self.current(host)
        return None if cert is None else cert.describe()

    def for_stream(self, host: str, until: float) -> Certificate:
        """A certificate for `host` that lasts until `until`: the kept one when it does,
        otherwise a new one. Raises CertificateError when none can be had."""
        self._recover()
        cert = self.current(host)
        if cert is not None and cert.lasts(until):
            return cert
        # Nothing is asked when it could only fail: no request, no failed check is spent.
        if until > self.clock() + LONGEST_STREAM_SECONDS:
            raise CertificateError(
                f"an HTTPS stream can run for at most {LONGEST_STREAM_SECONDS // 3600} hours, "
                "the life of a certificate for an address"
            )
        if problem := address_problem(host, self.config.acme_ca) or self.issuer.problem():
            raise CertificateError(problem)
        why = "there is none yet" if cert is None else "the one kept does not last the stream"
        self.log(f"requesting a certificate for {host}: {why}")
        new = self._request(host, cert)
        if not new.lasts(until):
            raise CertificateError(
                f"the new certificate for {host} expires before the stream ends; "
                "give the stream a shorter time limit (--ttl)"
            )
        self.log(f"certificate for {host} until {when(new.not_after)}")
        return new

    def _recover(self) -> None:
        """Put back a kept certificate that a request moved aside and could not put back
        itself (the daemon was killed meanwhile), unless a new one took its place."""
        if not self._previous.is_dir():
            return
        for folder in self._previous.glob("certificates/*/*"):
            home = self.store / folder.relative_to(self._previous)
            if not home.exists():
                home.parent.mkdir(parents=True, exist_ok=True)
                folder.rename(home)
                self.log(f"{home}: the kept certificate is back after an interrupted request")
        shutil.rmtree(self._previous, ignore_errors=True)

    def _request(self, host: str, old: Certificate | None) -> Certificate:
        """Ask for a new certificate. The old one is moved out of Caddy's way first, so
        Caddy asks whatever it thinks of the old one, and put back when the request fails
        (or by `_recover`, when the daemon dies meanwhile)."""
        aside = None
        if old is not None:
            aside = self._previous / old.cert_file.parent.relative_to(self.store)
            aside.parent.mkdir(parents=True, exist_ok=True)
            old.cert_file.parent.rename(aside)
        try:
            self._obtain(host)
            new = self.current(host)
            if new is None:
                raise CertificateError("Caddy said the certificate was stored, but it is not")
        except BaseException:
            if aside is not None and old is not None:
                shutil.rmtree(old.cert_file.parent, ignore_errors=True)
                aside.rename(old.cert_file.parent)
            raise
        finally:
            shutil.rmtree(self._previous, ignore_errors=True)
        return new

    def _obtain(self, host: str) -> None:
        """Run the request with port 80 open, and closed again whatever happens: a helper
        that failed or was cut off may have opened it all the same."""
        if self.firewall is not None:
            try:
                self.firewall.open()
            except FirewallError as exc:
                self._close_port()
                # Nothing is asked: a check that cannot reach the port counts as a failure.
                raise CertificateError(str(exc)) from exc
        try:
            self.issuer.obtain(host, lambda: self.current(host) is not None)
        finally:
            self._close_port()

    def _close_port(self) -> None:
        if self.firewall is None:
            return
        try:
            self.firewall.close()
        except FirewallError as exc:  # ExecStopPost closes it at the latest
            self.log(f"WARNING: {exc}")


def when(seconds: float) -> str:
    """A certificate's time, as `status`, `doctor` and the log show it."""
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(seconds))
