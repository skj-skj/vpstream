"""Local mode's dashboard: on the local network behind the password, and on this device."""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Iterator
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from vpstream import api, config, local, login, show
from vpstream.control import Control
from vpstream.tests.fakes import Clock, FakeRunner, FakeServer, free_port
from vpstream.tests.test_daemon import Running
from vpstream.tests.test_login import NOW, PASSWORD, answer, verifier

LAN = "127.0.0.2"  # stands in for the Wi-Fi address: a loopback address other than 127.0.0.1


def _web(tmp_path: Path, secure: bool, ip_header: bool) -> FlaskClient:
    control = Control(config.Config(local=True), config.Paths.under(tmp_path), log=lambda m: None)
    auth = login.Login(verifier(), login.Limiter(None, clock=Clock(NOW), log=lambda m: None))
    app = api.create_app(control.status, control, auth, secure=secure, ip_header=ip_header)
    return app.test_client()


def _log_in(web: FlaskClient, password: str = PASSWORD, ip: str = "") -> Any:
    headers = {"X-Vpstream-Ip": ip} if ip else {}
    base = {"base_url": "http://192.168.1.69:1338", "headers": headers}
    challenge = web.post("/api/v1/login/challenge", json={}, **base).get_json()
    proof = answer(challenge, password)
    return web.post("/api/v1/login", json={"nonce": challenge["nonce"], "proof": proof}, **base)


def _cookies(response: Any) -> SimpleCookie:
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("Set-Cookie"):
        jar.load(header)
    return jar


def _session(web: FlaskClient, token: str | None = None, cookie: str | None = None) -> int:
    client = web.application.test_client()  # nothing of the browser's but what is given
    if cookie:
        client.set_cookie(*cookie.split("=", 1))
    headers = {api.SESSION_HEADER: token} if token else {}
    return client.get("/api/v1/session", headers=headers).status_code


def test_a_local_login_sets_no_cookie_another_port_could_get(tmp_path: Path) -> None:
    """A browser sends a cookie to every port of the host (RFC 6265 8.5): to any other
    app's server on 127.0.0.1 or the phone's address. So local mode keeps none."""
    web = _web(tmp_path, secure=False, ip_header=False)
    response = _log_in(web)
    assert response.status_code == 200
    assert response.headers.getlist("Set-Cookie") == []
    answer = response.get_json()
    assert answer["csrf"] and answer["session"] and answer["device"]
    assert _session(web, token=answer["session"]) == 200
    # The token as a cookie, as another server would replay what it got: nothing.
    assert _session(web, cookie=f"{login.COOKIE}={answer['session']}") == 401
    assert _session(web, cookie=f"vpstream-dash={answer['session']}") == 401
    assert _session(web) == 401


def test_a_local_session_needs_the_csrf_token_for_changes(tmp_path: Path) -> None:
    web = _web(tmp_path, secure=False, ip_header=False)
    answer = _log_in(web).get_json()
    headers = {api.SESSION_HEADER: answer["session"]}
    refused = web.post("/api/v1/stream/stop", json={}, headers=headers)
    assert refused.status_code == 403
    headers[login.CSRF_HEADER] = answer["csrf"]
    assert web.post("/api/v1/stream/stop", json={}, headers=headers).status_code == 400  # none
    assert web.post("/api/v1/logout", json={}, headers=headers).status_code == 200
    assert _session(web, token=answer["session"]) == 401


def test_the_local_page_is_open_but_what_it_asks_for_is_not(tmp_path: Path) -> None:
    web = _web(tmp_path, secure=False, ip_header=False)
    for path in ("/", "/assets/dashboard.js", "/login", "/assets/login.js"):
        assert web.get(path).status_code == 200, path
    assert web.get("/api/v1/status").status_code == 401
    assert web.get("/api/v1/prepared").status_code == 401


def test_a_local_device_token_counts_from_its_header(tmp_path: Path) -> None:
    web = _web(tmp_path, secure=False, ip_header=False)
    device = _log_in(web).get_json()["device"]
    for _ in range(login.FAILS_PER_ADDRESS):
        _log_in(web, "a wrong password")
    blocked = web.post("/api/v1/login/challenge", json={})
    assert blocked.status_code == 429
    known = web.post("/api/v1/login/challenge", json={}, headers={api.DEVICE_HEADER: device})
    assert known.status_code == 200  # a browser that logged in before is counted on its own


def test_the_public_dashboard_keeps_its_secure_cookies(tmp_path: Path) -> None:
    response = _log_in(_web(tmp_path, secure=True, ip_header=True), ip="198.51.100.2")
    jar = _cookies(response)
    assert set(jar) == {login.COOKIE, login.DEVICE_COOKIE}
    assert all(morsel["secure"] for morsel in jar.values())
    assert "session" not in response.get_json()


def test_without_caddy_a_viewer_cannot_choose_its_address(tmp_path: Path) -> None:
    web = _web(tmp_path, secure=False, ip_header=False)
    for n in range(login.FAILS_PER_ADDRESS):
        _log_in(web, "a wrong password", ip=f"198.51.100.{n}")  # a new "address" each time
    blocked = web.post("/api/v1/login/challenge", json={}, headers={"X-Vpstream-Ip": "10.9.9.9"})
    assert blocked.status_code == 429  # counted by the connection's own address


# --- the daemon's two listeners


class Here:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.on = True
        monkeypatch.setattr(local, "find", self.find)
        monkeypatch.setattr(local, "is_mine", lambda address: self.on and address == LAN)

    def find(self, configured: str = "") -> str:
        if not self.on:
            raise local.LocalError("this device is on no network (no route)")
        return LAN


@pytest.fixture
def here(monkeypatch: pytest.MonkeyPatch) -> Here:
    return Here(monkeypatch)


def _running(root: Path, password: bool = True) -> Running:
    cfg = config.Config(local=True, stream_port=free_port(), dashboard_port=free_port())
    paths = config.Paths.under(root)
    if password:
        paths.dashboard_key.parent.mkdir(parents=True)
        login.write_verifier(paths.dashboard_key, verifier())
    return Running(root, FakeRunner(()), cfg, None, FakeServer())


@pytest.fixture
def lan_daemon(short_tmp: Path, here: Here) -> Iterator[Running]:
    run = _running(short_tmp).start()
    yield run
    run.close()


def _wait(check: Any, what: str, run: Running) -> None:
    for _ in range(200):
        if check():
            return
        time.sleep(0.025)
    raise AssertionError(f"{what}: {run.log}")


def _get(host: str, port: int, path: str = "/") -> tuple[int, str]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.getheader("Location") or ""
    finally:
        conn.close()


def test_the_lan_dashboard_needs_the_password_and_this_host_does_not(lan_daemon: Running) -> None:
    run = lan_daemon
    port = run.daemon.config.dashboard_port
    _wait(lambda: run.daemon.lan_address and run.daemon.dashboard_address, "both up", run)
    assert run.daemon.lan_address == f"{LAN}:{port}"
    assert _get(LAN, port)[0] == 200  # the page holds nothing; what it asks for needs the login
    assert _get(LAN, port, "/api/v1/status")[0] == 401
    assert _get("127.0.0.1", port, "/api/v1/status")[0] == 200  # Linux: the host's own
    status = run.daemon.status()["dashboard"]
    assert status["mode"] == "local" and status["url"] == f"http://{LAN}:{port}/"
    assert status["this_device"] == f"http://127.0.0.1:{port}/"
    assert status["password"] and not status["this_device_password"]
    line = show.dashboard_line(status)
    assert f"http://{LAN}:{port}/ (local network, password)" in line
    assert f"http://127.0.0.1:{port}/ (this device)" in line


def test_on_termux_this_device_needs_the_password_too(
    on_termux: None, short_tmp: Path, here: Here
) -> None:
    run = _running(short_tmp).start()
    try:
        port = run.daemon.config.dashboard_port
        _wait(lambda: run.daemon.lan_address and run.daemon.dashboard_address, "both up", run)
        assert _get("127.0.0.1", port, "/api/v1/status")[0] == 401
        assert run.daemon.status()["dashboard"]["this_device_password"] is True
    finally:
        run.close()


def test_no_lan_dashboard_without_a_password(short_tmp: Path, here: Here) -> None:
    run = _running(short_tmp, password=False).start()
    try:
        _wait(lambda: run.daemon.dashboard_address, "loopback up", run)
        _wait(lambda: "no password is set" in run.daemon.lan_problem, "lan refused", run)
        assert run.daemon.lan_address is None
        status = run.daemon.status()["dashboard"]
        line = show.dashboard_line(status)
        assert line.startswith("the dashboard is not available on the local network: no pass")
        assert "not on the local network" not in line  # said once, not twice
        # dash-pass, then the reload the command line asks for: it comes up.
        run.paths.dashboard_key.parent.mkdir(parents=True, exist_ok=True)
        login.write_verifier(run.paths.dashboard_key, verifier())
        run.daemon.reload_password()
        _wait(lambda: run.daemon.lan_address, "lan up after the password", run)
    finally:
        run.close()


def test_the_lan_dashboard_goes_with_the_address_and_comes_back(
    lan_daemon: Running, here: Here
) -> None:
    run = lan_daemon
    port = run.daemon.config.dashboard_port
    _wait(lambda: run.daemon.lan_address, "lan up", run)
    here.on = False  # the Wi-Fi is turned off
    _wait(lambda: run.daemon.lan_address is None, "lan closed", run)
    assert any("is closed: the address is gone" in m for m in run.log)
    with pytest.raises(OSError):
        _get(LAN, port)
    assert _get("127.0.0.1", port, "/api/v1/status")[0] == 200  # this device's stays
    here.on = True
    _wait(lambda: run.daemon.lan_address, "lan back", run)
    assert _get(LAN, port, "/api/v1/status")[0] == 401


def test_the_socket_keeps_the_password_routes(lan_daemon: Running) -> None:
    from vpstream import client

    answer_ = client.call(lan_daemon.paths.socket, "POST", "dashboard/unblock", {})
    assert isinstance(answer_, dict)
    assert json.dumps(answer_)  # an answer, not a refusal


def test_held_connections_cannot_take_every_thread(
    short_tmp: Path, here: Here, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is in front of the local network's dashboard: a device that opens many
    connections and never finishes a request gets a few threads, the rest a 503 at once,
    and what it holds is closed after a while."""
    import socket
    import threading

    from vpstream import daemon

    monkeypatch.setattr(daemon, "REQUEST_SECONDS", 1.0)
    run = _running(short_tmp).start()
    held: list[socket.socket] = []
    try:
        port = run.daemon.config.dashboard_port
        _wait(lambda: run.daemon.lan_address, "lan up", run)
        before = threading.active_count()
        for _ in range(40):
            conn = socket.create_connection((LAN, port), timeout=5)
            conn.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")  # and never the end of it
            held.append(conn)
        time.sleep(0.3)
        assert threading.active_count() - before <= daemon.PER_ADDRESS + 2  # and the watcher
        held[-1].settimeout(5)
        assert held[-1].recv(64).startswith(b"HTTP/1.1 503")
        time.sleep(1.5)  # the held ones time out
        assert _get(LAN, port, "/api/v1/status")[0] == 401  # served again
        assert threading.active_count() - before <= 2
    finally:
        for conn in held:
            conn.close()
        run.close()


def test_the_dashboard_serves_an_ipv6_local_address(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An IPv6 address of the local network (fc00::/7) is taken, so the dashboard must
    listen on it, under an address with brackets."""
    monkeypatch.setattr(local, "find", lambda configured="": "::1")  # stands in for a ULA
    monkeypatch.setattr(local, "is_mine", lambda address: address == "::1")
    run = _running(short_tmp).start()
    try:
        port = run.daemon.config.dashboard_port
        _wait(lambda: run.daemon.lan_address, "lan up on IPv6", run)
        assert run.daemon.lan_address == f"[::1]:{port}"
        assert run.daemon.status()["dashboard"]["url"] == f"http://[::1]:{port}/"
        assert _get("::1", port, "/api/v1/status")[0] == 401
    finally:
        run.close()


def test_a_request_that_trickles_in_is_closed_all_the_same(
    short_tmp: Path, here: Here, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket's timeout starts again with every byte: a client that sends one now and
    then would keep its slot without end. The request must be read within one deadline."""
    import socket

    from vpstream import daemon

    monkeypatch.setattr(daemon, "REQUEST_SECONDS", 1.0)
    run = _running(short_tmp).start()
    try:
        port = run.daemon.config.dashboard_port
        _wait(lambda: run.daemon.lan_address, "lan up", run)
        conn = socket.create_connection((LAN, port), timeout=5)
        started = time.monotonic()
        closed = False
        try:
            for byte in b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: " + b"a" * 40:
                conn.sendall(bytes([byte]))
                time.sleep(0.3)  # well inside the socket's timeout each time
        except OSError:
            closed = True
        if not closed:
            conn.settimeout(3)
            closed = conn.recv(64) == b""
        assert closed and time.monotonic() - started < 5
        assert _get(LAN, port, "/api/v1/status")[0] == 401  # and the others are served
    finally:
        conn.close()
        run.close()
