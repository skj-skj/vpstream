"""The public dashboard through the real Caddy, over real HTTPS: a daemon in a temporary
folder on free ports of this host, a stand-in firewall. Nothing outside the temporary folder
is touched, and the certificate authority is never put in any trust store."""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from vpstream import caddy, client, config, daemon, login
from vpstream.tests.fakes import FakeFirewall
from vpstream.tests.test_login import PASSWORD, answer, verifier
from vpstream.tools import SystemRunner

pytestmark = pytest.mark.integration
HOST = "127.0.0.1"


def _has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


HOSTS = [HOST, "::1"] if _has_ipv6_loopback() else [HOST]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def wait_for(what: Callable[[], Any], seconds: float = 20.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if found := what():
            return found
        time.sleep(0.1)
    raise AssertionError("waited in vain")


class Public:
    def __init__(self, root: Path) -> None:
        self.paths = config.Paths.under(root)
        self.cfg = config.Config(
            stream_port=_free_port(),
            dashboard_port=_free_port(),
            dashboard_public=True,
        )
        self.paths.config_file.parent.mkdir(parents=True)
        login.write_verifier(self.paths.dashboard_key, verifier())
        self.log: list[str] = []
        self.fw = FakeFirewall()
        runner = SystemRunner()
        self.caddy = caddy.DashboardCaddy(runner, self.cfg.dashboard_port, HOSTS, self.paths)
        self.daemon = daemon.Daemon(
            self.cfg,
            self.paths,
            runner,
            None,
            None,
            dashboard_server=self.caddy,
            dashboard_firewall=self.fw,
            log=self.log.append,
            retry_seconds=0.3,
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.daemon.serve, args=(self.stop,), daemon=True)
        self.cookies: dict[str, str] = {}

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = client.call(self.paths.socket, "GET", "status")
        return dict(info["dashboard"])

    def context(self, trusted: bool = True) -> ssl.SSLContext:
        if trusted:  # trust the authority, as a phone does once it is installed
            return ssl.create_default_context(cafile=str(self.caddy.root_cert))
        unverified = ssl.create_default_context()
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE  # the warning clicked through
        return unverified

    def handshakes(self, host: str) -> bool:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with socket.socket(family) as raw, self.context(False).wrap_socket(raw) as tls:
                tls.settimeout(5)
                tls.connect((host, self.cfg.dashboard_port))
            return True
        except (OSError, ssl.SSLError):
            return False

    def https(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        trusted: bool = True,
    ) -> tuple[int, dict[str, str], Any]:
        conn = http.client.HTTPSConnection(
            HOST, self.cfg.dashboard_port, timeout=10, context=self.context(trusted)
        )
        sent = {"Connection": "close", **(headers or {})}
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = None
        if body is not None:
            data, sent["Content-Type"] = json.dumps(body), "application/json"
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            raw = response.read()
            got = {k.lower(): v for k, v in response.getheaders()}
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    name, _, rest = value.split(";")[0].partition("=")
                    self.cookies[name] = rest
            try:
                return response.status, got, json.loads(raw)
            except ValueError:
                return response.status, got, raw
        finally:
            conn.close()

    def log_in(self, password: str = PASSWORD, headers: dict[str, str] | None = None) -> Any:
        status, _, challenge = self.https("POST", "/api/v1/login/challenge", {}, headers)
        if status != 200:
            return status, challenge
        proof = answer(challenge, password)
        body = {"nonce": challenge["nonce"], "proof": proof}
        status, _, data = self.https("POST", "/api/v1/login", body, headers)
        return status, data


@pytest.fixture
def public(short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Public]:
    if not shutil.which("caddy"):
        pytest.skip("caddy is not installed on this host")
    # This host's "public" addresses, as the daemon finds them: loopback, v4 and v6.
    monkeypatch.setattr(daemon.net, "outgoing_address", lambda: HOST)
    monkeypatch.setattr(
        daemon.net, "outgoing_address6", lambda: HOSTS[-1] if len(HOSTS) > 1 else ""
    )
    run = Public(short_tmp)
    run.thread.start()
    assert run.daemon.ready.wait(10)
    wait_for(lambda: run.status()["address"] and run.caddy.root_cert.exists())
    # Caddy makes the certificates just after it listens: wait until each address has one.
    wait_for(lambda: all(run.handshakes(host) for host in HOSTS))
    yield run
    run.stop.set()
    run.thread.join(15)


def test_the_dashboard_is_https_with_a_certificate_from_its_own_authority(
    public: Public,
) -> None:
    status, headers, _ = public.https("GET", "/login")
    assert status == 200 and "html" in headers["content-type"]
    # Trusting the root that Caddy made is enough, address and all: what a phone gets once
    # the authority is installed. The certificate itself is for the address (no SNI sent).
    with (
        socket.create_connection((HOST, public.cfg.dashboard_port), timeout=5) as raw,
        public.context().wrap_socket(raw, server_hostname=HOST) as tls,
    ):
        cert = tls.getpeercert()
    assert ("IP Address", HOST) in cert["subjectAltName"]
    issuer = dict(item[0] for item in cert["issuer"])
    assert "Caddy Local Authority" in issuer["commonName"]
    # The warning clicked through also works.
    assert public.https("GET", "/login", trusted=False)[0] == 200
    dash = public.status()
    assert (
        dash["mode"] == "public" and dash["url"] == f"https://{HOST}:{public.cfg.dashboard_port}/"
    )
    fingerprint = caddy.certificate_fingerprint(public.caddy.root_cert.read_text())
    assert dash["certificate"] == fingerprint and dash["password"] is True
    assert dash["port_state"] == "open" and public.fw.calls == ["open"]
    # The authority stays out of every trust store, and nothing else listens (no port 80).
    assert "trust store installation disabled" in public.caddy.error_log.read_text()


def test_plain_http_on_the_dashboard_port_gets_no_dashboard(public: Public) -> None:
    conn = http.client.HTTPConnection(HOST, public.cfg.dashboard_port, timeout=5)
    try:
        conn.request("GET", "/api/v1/status")
        response = conn.getresponse()
        body = response.read()
    finally:
        conn.close()
    assert response.status == 400 and b"stream" not in body


def test_logging_in_over_https_and_using_the_dashboard(public: Public) -> None:
    status, headers, _ = public.https("GET", "/")
    assert status == 303 and headers["location"].endswith("/login")
    assert public.https("GET", "/api/v1/status")[0] == 401
    status, data = public.log_in()
    assert status == 200, data
    csrf = data["csrf"]
    status, headers, info = public.https("GET", "/api/v1/status")
    assert status == 200 and "stream" in info
    assert headers["cache-control"] == "no-store" and headers["x-frame-options"] == "DENY"
    assert "server" not in headers, "what the daemon is built with stays its own"
    policy = {"policy": "open"}
    assert public.https("POST", "/api/v1/stream/policy", policy)[0] == 403
    status, _, refused = public.https(
        "POST", "/api/v1/stream/policy", policy, {login.CSRF_HEADER: csrf}
    )
    assert status == 400 and "stream" in refused["error"], "past the guard"
    assert public.https("POST", "/api/v1/logout", {}, {login.CSRF_HEADER: csrf})[0] == 200
    assert public.https("GET", "/api/v1/status")[0] == 401


def test_a_viewer_cannot_pick_the_address_its_failures_count_under(public: Public) -> None:
    """Caddy sets the address header itself, so a guesser who sends a new one with every
    try is still counted under its real address."""
    for n in range(login.FAILS_PER_ADDRESS):
        status, _ = public.log_in("a wrong password", {"X-Vpstream-Ip": f"192.0.2.{n + 1}"})
        assert status == 401
    status, data = public.log_in(PASSWORD, {"X-Vpstream-Ip": "198.51.100.77"})
    assert status == 429, data
    # Counted under the real address, not under one the guesser named, nor as "unknown".
    assert public.daemon.login is not None
    assert list(public.daemon.login.limiter._by_key) == [HOST]


def test_the_socket_needs_no_password_while_the_dashboard_does(public: Public) -> None:
    assert client.call(public.paths.socket, "GET", "session") == {"login": False}
    assert public.https("GET", "/api/v1/session")[0] == 401


def test_a_dashboard_caddy_that_dies_closes_the_port_and_comes_back(public: Public) -> None:
    proc = public.caddy._proc
    assert proc is not None
    proc.kill()
    wait_for(lambda: "close" in public.fw.calls)
    wait_for(lambda: public.fw.calls.count("open") == 2 and public.status()["address"])
    assert public.fw.calls == ["open", "close", "open"]
    assert any("Caddy for the dashboard stopped" in line for line in public.log)
    assert public.https("GET", "/login")[0] == 200


def test_stopping_the_daemon_closes_the_port_before_caddy_goes(public: Public) -> None:
    public.stop.set()
    public.thread.join(15)
    assert public.fw.calls == ["open", "close"]
    assert not public.caddy.running()
    with pytest.raises(OSError):
        socket.create_connection((HOST, public.cfg.dashboard_port), timeout=1)
    assert not public.paths.dashboard_socket.exists()


@pytest.mark.skipif(len(HOSTS) < 2, reason="no IPv6 loopback on this host")
def test_an_ipv6_visitor_gets_a_certificate_for_its_own_address(public: Public) -> None:
    """No SNI is sent for an address: each address has a listener with its own certificate."""
    for host in HOSTS:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with (
            socket.socket(family) as raw,
            public.context().wrap_socket(raw, server_hostname=host) as tls,
        ):
            tls.settimeout(10)
            tls.connect((host, public.cfg.dashboard_port))
            cert = tls.getpeercert()
        expect = "0:0:0:0:0:0:0:1" if host == "::1" else host
        assert ("IP Address", expect) in cert["subjectAltName"] or (
            "IP Address",
            host,
        ) in cert["subjectAltName"], cert["subjectAltName"]
