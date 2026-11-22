"""A stream over HTTPS through the real Caddy, as a viewer sees it.

Two ways in. A certificate made here with openssl and kept where Caddy keeps one is served
by whatever Caddy the host has (Ubuntu's 2.6.2 too). A certificate requested from Pebble,
Let's Encrypt's test authority, run here on free ports, needs a Caddy with ACME profiles
(2.10 or newer) and Pebble: `pebble` on PATH, or its path in $VPSTREAM_PEBBLE. Nothing ever
asks Let's Encrypt itself.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from vpstream import caddy, certificate, config
from vpstream.certificate import CertificateError, Certificates
from vpstream.tests.fakes import free_port, store_certificate
from vpstream.tests.test_stream_integration import BODY, HTML, Live, _cookie
from vpstream.tools import SystemRunner

pytestmark = pytest.mark.integration

HOST = "127.0.0.1"
# Where a request would go if a test asked for a certificate by mistake: nothing listens
# there, so it fails at once and never reaches Let's Encrypt.
NOWHERE = "https://127.0.0.1:9/directory"


def _need(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        pytest.skip(f"{tool} is not installed on this host")
    return path


def _self_signed(folder: Path, days: int = 2) -> tuple[Path, Path]:
    """A certificate for 127.0.0.1 by openssl, its own root, like the one a viewer gets."""
    folder.mkdir(parents=True, exist_ok=True)
    cert, key = folder / "cert.pem", folder / "key.pem"
    subprocess.run(
        [_need("openssl"), "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-keyout", str(key), "-out", str(cert), "-days", str(days),
         "-subj", "/CN=vpstream test", "-addext", f"subjectAltName=IP:{HOST}"],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


def _trusting(cafile: Path) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(cafile))
    context.check_hostname = True  # the address must be in the certificate
    return context


@pytest.fixture
def kept(short_tmp: Path, tmp_path: Path) -> Iterator[Live]:
    """A daemon serving HTTPS with a kept certificate, which lasts two days."""
    _need("caddy")
    cert, key = _self_signed(tmp_path / "cert")
    run = Live(short_tmp, _trusting(cert), stream_https=True, stream_host=HOST, acme_ca=NOWHERE)
    run.cafile = cert
    store = run.paths.certificate_dir / "data" / "caddy"
    store_certificate(store, NOWHERE, HOST, pem=cert.read_text(), key=key.read_text())
    run.thread.start()
    assert run.daemon.ready.wait(10)
    yield run
    run.stop.set()
    run.thread.join(15)


def test_a_kept_certificate_is_served_and_checks_out(kept: Live, tmp_path: Path) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(BODY)
    info = kept.api("stream/start", {"file": str(video), "alias": "aaa", "ttl": 60})
    assert info["stream"]["url"] == f"https://{HOST}:{kept.cfg.stream_port}/aaa"
    # Python, like a browser, sends no server name for an address: Caddy's default_sni.
    status, headers, body = kept.get("/aaa")
    assert status == 200 and body == BODY and headers["content-type"] == "video/mp4"
    status, _, body = kept.get("/aaa", headers={"Range": "bytes=100-199"})
    assert status == 206 and body == BODY[100:200]
    status, headers, body = kept.get("/aaa/playlist.m3u")
    assert status == 404  # one file: no playlist; a folder's names https (test below)
    assert not any("requesting" in line for line in kept.log), kept.log
    status = kept.api("status")
    assert status["https"]["enabled"] is True
    assert status["https"]["certificate"]["host"] == HOST


def test_plain_http_on_the_https_port_gets_nothing(kept: Live, tmp_path: Path) -> None:
    import http.client

    video = tmp_path / "film.mp4"
    video.write_bytes(BODY)
    kept.api("stream/start", {"file": str(video), "alias": "aaa", "ttl": 60})
    conn = http.client.HTTPConnection(HOST, kept.cfg.stream_port, timeout=10)
    try:
        conn.request("GET", "/aaa")
        answer = conn.getresponse()
        assert answer.status == 400 and BODY not in answer.read()
    finally:
        conn.close()


def test_a_folder_s_playlist_names_https(kept: Live, tmp_path: Path) -> None:
    show = tmp_path / "show"
    show.mkdir()
    (show / "e1.mp4").write_bytes(BODY)
    kept.api("stream/start", {"dir": str(show), "alias": "aaa", "ttl": 60})
    _, headers, body = kept.get("/aaa")
    assert headers["content-type"].startswith("audio/x-mpegurl")
    assert f"https://{HOST}:{kept.cfg.stream_port}/aaa/1".encode() in body


def test_the_approval_cookie_is_secure_over_https(kept: Live, sample: Path, tmp_path: Path) -> None:
    film = tmp_path / "copy.mkv"
    shutil.copy(sample, film)
    kept.api("stream/start", {"file": str(film), "alias": "aaa", "mode": "browser", "ttl": 60})
    status, headers, _ = kept.get("/aaa", headers=HTML)
    assert status == 403
    cookie = headers["set-cookie"]
    assert cookie.startswith("__Host-vpstream=")
    for part in ("; Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
        assert part in cookie, cookie
    # A viewer cannot make itself look plain: Caddy sets the scheme over what it sent.
    status, headers, _ = kept.get("/aaa", headers={**HTML, caddy.HEADER_SCHEME: "http"})
    assert "; Secure" in headers["set-cookie"]
    assert kept.get("/aaa", headers={**HTML, "Cookie": _cookie(headers)})[0] == 403


def test_a_plain_http_stream_on_an_https_host_has_no_secure_cookie(
    kept: Live, sample: Path, tmp_path: Path
) -> None:
    film = tmp_path / "copy.mkv"
    shutil.copy(sample, film)
    kept.tls = None
    info = kept.api(
        "stream/start", {"file": str(film), "alias": "aaa", "mode": "browser", "http": True}
    )
    assert info["stream"]["url"].startswith("http://") and info["stream"]["https"] is False
    status, headers, _ = kept.get("/aaa", headers=HTML)
    assert status == 403 and "Secure" not in headers["set-cookie"]
    assert headers["set-cookie"].startswith("vpstream=")


# --- a certificate from Pebble


class Pebble:
    """Pebble on free ports, checking http-01 on `http_port`, issuing 160-hour certificates
    with the shortlived profile, as Let's Encrypt does for an address."""

    def __init__(self, binary: str, folder: Path) -> None:
        self.cert, key = _self_signed(folder / "listener")
        self.port, self.manage, self.http_port = free_port(), free_port(), free_port()
        config = {
            "pebble": {
                "listenAddress": f"{HOST}:{self.port}",
                "managementListenAddress": f"{HOST}:{self.manage}",
                "certificate": str(self.cert),
                "privateKey": str(key),
                "httpPort": self.http_port,
                "tlsPort": free_port(),
                "ocspResponderURL": "",
                "externalAccountBindingRequired": False,
                "retryAfter": {"authz": 1, "order": 1},
                "profiles": {
                    "default": {"description": "90 days", "validityPeriod": 7776000},
                    "shortlived": {"description": "160 hours", "validityPeriod": 576000},
                },
            }
        }
        (folder / "pebble.json").write_text(json.dumps(config))
        env = {**os.environ, "PEBBLE_VA_NOSLEEP": "1", "PEBBLE_WFE_NONCEREJECT": "0"}
        self.log = (folder / "pebble.log").open("wb")
        self.proc = subprocess.Popen(
            [binary, "-config", str(folder / "pebble.json")],
            stdout=self.log, stderr=self.log, env=env, cwd=folder,
        )  # fmt: skip
        self.directory = f"https://{HOST}:{self.port}/dir"
        self.root = folder / "issuer-root.pem"
        deadline = time.monotonic() + 15
        while True:
            try:
                context = ssl.create_default_context(cafile=str(self.cert))
                url = f"https://{HOST}:{self.manage}/roots/0"
                with urllib.request.urlopen(url, context=context, timeout=2) as answer:
                    self.root.write_bytes(answer.read())
                return
            except OSError:
                if time.monotonic() > deadline or self.proc.poll() is not None:
                    self.stop()
                    raise
                time.sleep(0.2)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()


@pytest.fixture
def pebble(tmp_path: Path) -> Iterator[Pebble]:
    # Given Pebble (as in CI), the test must run: nothing may skip it there.
    given = os.environ.get("VPSTREAM_PEBBLE")
    if given is None:
        _need("caddy")
    problem = caddy.profiles_problem(SystemRunner())
    if problem and given is None:
        pytest.skip(problem)
    assert shutil.which("caddy") and not problem, problem or "caddy is not installed"
    binary = given or _need("pebble")
    run = Pebble(binary, tmp_path)
    yield run
    run.stop()


def _issued(pebble: Pebble, short_tmp: Path, port: int | None = None) -> Live:
    run = Live(
        short_tmp,
        _trusting(pebble.root),
        stream_https=True,
        stream_host=HOST,
        acme_ca=pebble.directory,
        acme_ca_root=str(pebble.cert),
        acme_port=port or pebble.http_port,
    )
    run.thread.start()
    assert run.daemon.ready.wait(10)
    return run


def test_a_certificate_is_requested_once_and_used_again(
    pebble: Pebble, short_tmp: Path, tmp_path: Path
) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(BODY)
    live = _issued(pebble, short_tmp)
    try:
        live.api("stream/start", {"file": str(video), "alias": "aaa", "ttl": 60})
        status, _, body = live.get("/aaa")
        assert status == 200 and body == BODY
        cert = live.api("status")["https"]["certificate"]
        assert 159 * 3600 < cert["expires"] - cert["not_before"] <= 160 * 3600  # shortlived
        assert sum("requesting a certificate" in line for line in live.log) == 1
        # Nothing of the request is left running or listening.
        assert not caddy._accepts(pebble.http_port, HOST)
        live.api("stream/stop", {})
        live.api("stream/start", {"file": str(video), "alias": "bbb", "ttl": 120})
        assert live.get("/bbb")[0] == 200
        assert sum("requesting a certificate" in line for line in live.log) == 1, live.log
        assert live.api("status")["https"]["certificate"] == cert
    finally:
        live.stop.set()
        live.thread.join(15)


def test_a_refused_request_starts_nothing_and_plain_http_still_can(
    pebble: Pebble, short_tmp: Path, tmp_path: Path
) -> None:
    video = tmp_path / "film.mp4"
    video.write_bytes(BODY)
    # Caddy answers the check on a port Pebble does not look at: as with port 80 blocked.
    live = _issued(pebble, short_tmp, port=free_port())
    try:
        started = time.monotonic()
        with pytest.raises(Exception, match=r"refused.*--http") as failure:
            live.api("stream/start", {"file": str(video), "alias": "aaa", "ttl": 60})
        assert time.monotonic() - started < 30, "given up at the first failure"
        assert "connection refused" in str(failure.value).lower() or "connect" in str(failure.value)
        status = live.api("status")
        assert status["stream"] is None and status["https"]["certificate"] is None
        live.tls = None
        info = live.api("stream/start", {"file": str(video), "alias": "aaa", "http": True})
        assert info["stream"]["url"].startswith("http://")
        assert live.get("/aaa")[0] == 200
    finally:
        live.stop.set()
        live.thread.join(15)


def _requester(pebble: Pebble, root: Path, port: int) -> Certificates:
    """The stream's certificates alone, through the real Caddy, without a daemon."""
    settings = config.Config(
        stream_https=True,
        acme_ca=pebble.directory,
        acme_ca_root=str(pebble.cert),
        acme_port=port,
    )
    paths = config.Paths.under(root)
    issuer = caddy.CertificateCaddy(SystemRunner(), settings, paths)
    return Certificates(settings, paths, issuer, None, log=lambda m: None)


def _orders(pebble: Pebble) -> int:
    pebble.log.flush()
    return Path(pebble.log.name).read_text(errors="replace").count("POST /order-plz")


def test_a_kept_certificate_that_does_not_last_is_replaced_through_caddy(
    pebble: Pebble, short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old one is moved out of Caddy's way, so Caddy asks for a new one. Pebble does not
    date certificates back as Let's Encrypt does, so no margin here."""
    monkeypatch.setattr(certificate, "MARGIN_SECONDS", 0)
    monkeypatch.setattr(certificate, "LONGEST_STREAM_SECONDS", 200 * 3600)
    certs = _requester(pebble, short_tmp, pebble.http_port)
    first = certs.for_stream(HOST, time.time() + 3600)
    old = first.cert_file.read_text()
    time.sleep(1.1)  # a new certificate, a new second
    second = certs.for_stream(HOST, first.not_after + 0.5)
    assert second.cert_file.read_text() != old and second.not_after > first.not_after
    assert not (certs.paths.certificate_dir / "previous").exists()
    assert _orders(pebble) == 2


@pytest.mark.parametrize("address", ["127.0.0.2", "0.0.0.0"])
def test_a_program_on_the_check_port_means_nothing_is_asked(
    pebble: Pebble, short_tmp: Path, address: str
) -> None:
    """Caddy would go on regardless ("assuming conflicting listener is correctly
    configured") and spend a failed check on the other program."""
    other = socket.socket()
    other.bind((address, pebble.http_port))
    other.listen()
    try:
        certs = _requester(pebble, short_tmp, pebble.http_port)
        with pytest.raises(CertificateError, match="already used by another program"):
            certs.for_stream(HOST, time.time() + 3600)
        assert _orders(pebble) == 0
    finally:
        other.close()
