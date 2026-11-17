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


def test_local_cookies_work_over_plain_http(tmp_path: Path) -> None:
    response = _log_in(_web(tmp_path, secure=False, ip_header=False))
    assert response.status_code == 200
    jar = _cookies(response)
    assert set(jar) == {login.LOCAL_COOKIE, login.LOCAL_DEVICE_COOKIE}
    for morsel in jar.values():
        assert not morsel["secure"] and morsel["httponly"] and morsel["samesite"] == "Strict"
        assert morsel["path"] == "/"


def test_the_public_dashboard_keeps_its_secure_cookies(tmp_path: Path) -> None:
    jar = _cookies(_log_in(_web(tmp_path, secure=True, ip_header=True), ip="198.51.100.2"))
    assert set(jar) == {login.COOKIE, login.DEVICE_COOKIE}
    assert all(morsel["secure"] for morsel in jar.values())


def test_without_caddy_a_viewer_cannot_choose_its_address(tmp_path: Path) -> None:
    web = _web(tmp_path, secure=False, ip_header=False)
    for n in range(login.FAILS_PER_ADDRESS):
        _log_in(web, "a wrong password", ip=f"198.51.100.{n}")  # a new "address" each time
    blocked = web.post("/api/v1/login/challenge", json={}, headers={"X-Vpstream-Ip": "10.9.9.9"})
    assert blocked.status_code == 429  # counted by the connection's own address


def test_a_local_session_cookie_means_nothing_to_the_public_names(tmp_path: Path) -> None:
    web = _web(tmp_path, secure=False, ip_header=False)
    token = _cookies(_log_in(web))[login.LOCAL_COOKIE].value
    other = _web(tmp_path, secure=True, ip_header=True)
    other.set_cookie(login.COOKIE, token)
    assert other.get("/api/v1/status").status_code == 401


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
    assert _get(LAN, port) == (303, "/login")
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
        assert _get("127.0.0.1", port) == (303, "/login")
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
        assert "not on the local network: the dashboard is not available" in (
            show.dashboard_line(status)
        )
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
    assert _get(LAN, port)[0] == 303


def test_the_socket_keeps_the_password_routes(lan_daemon: Running) -> None:
    from vpstream import client

    answer_ = client.call(lan_daemon.paths.socket, "POST", "dashboard/unblock", {})
    assert isinstance(answer_, dict)
    assert json.dumps(answer_)  # an answer, not a refusal
