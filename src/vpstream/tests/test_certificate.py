"""The stream's certificate: kept and used again, asked for only when it does not last."""

from __future__ import annotations

import calendar
import time
from pathlib import Path

import pytest

from vpstream import certificate, config
from vpstream.certificate import CertificateError, Certificates
from vpstream.firewall import FirewallError
from vpstream.tests.fakes import (
    FakeFirewall,
    FakeIssuer,
    certificate_pem,
    store_certificate,
)

DATA = Path(__file__).parent / "data"
HOST = "203.0.113.5"
NOW = 1_800_000_000.0
HOUR = 3600.0
CFG = config.Config(stream_https=True)


def _at(text: str) -> float:
    return float(calendar.timegm(time.strptime(text, "%b %d %H:%M:%S %Y")))


# --- reading a certificate


def test_the_dates_of_a_real_certificate_are_what_openssl_says() -> None:
    # A certificate Pebble issued for 127.0.0.1 with the shortlived profile; openssl x509
    # -dates: notBefore=Nov 15 09:01:24 2026 GMT, notAfter=Nov 22 01:01:23 2026 GMT.
    pem = (DATA / "ip-certificate.pem").read_text()
    assert certificate.validity(pem) == (_at("Nov 15 09:01:24 2026"), _at("Nov 22 01:01:23 2026"))


@pytest.mark.parametrize("year", [2026, 2049, 2050, 2101])
def test_both_kinds_of_time_are_read(year: int) -> None:
    start = float(calendar.timegm((year, 3, 4, 5, 6, 7, 0, 0, 0)))
    assert certificate.validity(certificate_pem(start, start + 160 * HOUR)) == (
        start,
        start + 160 * HOUR,
    )


@pytest.mark.parametrize(
    "pem",
    [
        "",
        "-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n",
        "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\n",  # an empty SEQUENCE
        "-----BEGIN CERTIFICATE-----\nMIIBAA==\n-----END CERTIFICATE-----\n",  # cut short
    ],
)
def test_what_is_not_a_certificate_is_refused(pem: str) -> None:
    with pytest.raises(ValueError):
        certificate.validity(pem)


def test_a_cut_certificate_is_refused_at_every_length() -> None:
    import base64

    pem = certificate_pem(NOW, NOW + HOUR)
    der = certificate.first_der(pem)
    for size in range(len(der)):
        cut = base64.b64encode(der[:size]).decode()
        with pytest.raises(ValueError):
            certificate.validity(f"-----BEGIN CERTIFICATE-----\n{cut}\n-----END CERTIFICATE-----")


# --- the kept certificate


def test_only_a_certificate_from_this_authority_for_this_address_is_used(tmp_path: Path) -> None:
    ca = CFG.acme_ca
    store_certificate(tmp_path, config.LETS_ENCRYPT_STAGING, HOST, NOW, NOW + 160 * HOUR)
    store_certificate(tmp_path, ca, "198.51.100.7", NOW, NOW + 160 * HOUR)
    store_certificate(tmp_path, ca, HOST, NOW, NOW + 160 * HOUR, sans=[HOST, "198.51.100.7"])
    assert certificate.stored(tmp_path, ca, HOST) is None
    store_certificate(tmp_path / "b", ca, HOST, NOW, NOW + 160 * HOUR)
    found = certificate.stored(tmp_path / "b", ca, HOST)
    assert found is not None and (found.not_before, found.not_after) == (NOW, NOW + 160 * HOUR)


def test_a_certificate_without_its_key_or_half_written_is_not_used(tmp_path: Path) -> None:
    folder = store_certificate(tmp_path, CFG.acme_ca, HOST, NOW, NOW + HOUR)
    (folder / f"{HOST}.key").unlink()
    assert certificate.stored(tmp_path, CFG.acme_ca, HOST) is None
    folder = store_certificate(tmp_path, CFG.acme_ca, HOST, NOW, NOW + HOUR)
    (folder / f"{HOST}.crt").write_text("-----BEGIN CERTIFICATE-----\nMII")
    assert certificate.stored(tmp_path, CFG.acme_ca, HOST) is None
    (folder / f"{HOST}.crt").write_text(certificate_pem(NOW, NOW + HOUR))
    (folder / f"{HOST}.json").write_text("{")
    assert certificate.stored(tmp_path, CFG.acme_ca, HOST) is None


# --- reuse, and asking again


class Setup:
    def __init__(self, tmp_path: Path, fail: str = "", fw_fail: str = "") -> None:
        self.now = NOW
        self.paths = config.Paths.under(tmp_path)
        self.firewall = FakeFirewall(fw_fail)
        self.log: list[str] = []
        self.certs = Certificates(
            CFG,
            self.paths,
            issuer=None,  # type: ignore[arg-type]  # set below: it needs the store
            firewall=self.firewall,
            log=self.log.append,
            clock=lambda: self.now,
        )
        self.issuer = FakeIssuer(
            self.certs.store, CFG.acme_ca, lambda: self.now, fail=fail, firewall=self.firewall
        )
        self.certs.issuer = self.issuer

    def keep(self, not_after: float) -> Path:
        return store_certificate(self.certs.store, CFG.acme_ca, HOST, NOW - HOUR, not_after)


def test_a_kept_certificate_that_lasts_the_stream_is_used_and_nothing_is_asked(
    tmp_path: Path,
) -> None:
    s = Setup(tmp_path)
    s.keep(NOW + 50 * HOUR)
    cert = s.certs.for_stream(HOST, NOW + 3 * HOUR)
    assert cert.not_after == NOW + 50 * HOUR
    assert s.issuer.asked == [] and s.firewall.calls == []
    # Up to its end, less the margin.
    s.certs.for_stream(HOST, NOW + 50 * HOUR - certificate.MARGIN_SECONDS)
    assert s.issuer.asked == []
    # A stream that ends within the margin of its end gets a new one.
    s.certs.for_stream(HOST, NOW + 50 * HOUR - certificate.MARGIN_SECONDS + 1)
    assert s.issuer.asked == [HOST]


def test_none_kept_one_is_asked_for_with_the_port_open_only_meanwhile(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    cert = s.certs.for_stream(HOST, NOW + 3 * HOUR)
    assert s.issuer.asked == [HOST]
    assert s.issuer.port_open == [True]
    assert s.firewall.calls == ["open", "close"]
    assert cert.not_after == NOW - HOUR + 160 * HOUR
    assert any("there is none yet" in line for line in s.log)
    # The next stream uses it.
    s.certs.for_stream(HOST, NOW + 100 * HOUR)
    assert s.issuer.asked == [HOST]


@pytest.mark.parametrize("left", [0.0, -HOUR, 2 * HOUR])
def test_one_that_does_not_last_the_stream_is_replaced(tmp_path: Path, left: float) -> None:
    s = Setup(tmp_path)
    old = s.keep(NOW + left)
    cert = s.certs.for_stream(HOST, NOW + 3 * HOUR)
    assert s.issuer.asked == [HOST]
    assert cert.not_after == NOW - HOUR + 160 * HOUR
    assert cert.cert_file.parent == old  # in Caddy's place, and the old one is gone
    assert not (s.paths.certificate_dir / "previous").exists()


def test_a_failed_request_keeps_the_old_certificate_and_closes_the_port(tmp_path: Path) -> None:
    s = Setup(tmp_path, fail="urn:ietf:params:acme:error:connection: Timeout during connect")
    s.keep(NOW + HOUR)
    with pytest.raises(CertificateError, match="Timeout during connect"):
        s.certs.for_stream(HOST, NOW + 3 * HOUR)
    assert s.firewall.calls == ["open", "close"]
    kept = s.certs.current(HOST)
    assert kept is not None and kept.not_after == NOW + HOUR
    assert not (s.paths.certificate_dir / "previous").exists()


def test_a_port_that_cannot_be_opened_means_nothing_is_asked(tmp_path: Path) -> None:
    s = Setup(tmp_path, fw_fail="sudo: a password is required")
    with pytest.raises(CertificateError, match="password is required"):
        s.certs.for_stream(HOST, NOW + 3 * HOUR)
    assert s.issuer.asked == []


def test_a_port_that_cannot_be_closed_is_only_warned_about(tmp_path: Path) -> None:
    s = Setup(tmp_path)

    class CloseFails(FakeFirewall):
        def close(self) -> None:
            self.calls.append("close")
            raise FirewallError("ufw failed")

    s.certs.firewall = s.issuer.firewall = CloseFails()
    assert s.certs.for_stream(HOST, NOW + 3 * HOUR).host == HOST
    assert any("WARNING" in line and "ufw failed" in line for line in s.log)


def test_a_stream_longer_than_a_new_certificate_lasts_asks_for_nothing(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.keep(NOW + HOUR)
    longest = certificate.LONGEST_STREAM_SECONDS
    with pytest.raises(CertificateError, match="at most 156 hours"):
        s.certs.for_stream(HOST, NOW + longest + 1)
    assert s.issuer.asked == [] and s.firewall.calls == []
    assert s.certs.for_stream(HOST, NOW + longest).not_after > NOW + longest
    assert s.issuer.asked == [HOST]


def test_a_new_certificate_that_still_does_not_last_is_refused(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.issuer.lifetime = 10 * HOUR
    with pytest.raises(CertificateError, match="expires before the stream ends"):
        s.certs.for_stream(HOST, NOW + 20 * HOUR)


def test_without_a_firewall_helper_nothing_is_opened(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.certs.firewall = None
    assert s.certs.for_stream(HOST, NOW + HOUR).host == HOST


def test_status_says_until_when(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    assert s.certs.status(HOST) is None
    s.keep(NOW + 5 * HOUR)
    assert s.certs.status(HOST) == {
        "host": HOST,
        "not_before": NOW - HOUR,
        "expires": NOW + 5 * HOUR,
    }
