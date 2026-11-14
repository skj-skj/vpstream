"""The public dashboard over HTTP: what someone on the internet can reach without the
password, and what they cannot forge. Flask's test client, over https:// so that the
browser rules for Secure cookies apply."""

from __future__ import annotations

import re
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from vpstream import api, config, gate, login, public
from vpstream.control import Control
from vpstream.tests.fakes import FakeFirewall, FakeServer
from vpstream.tests.test_control_clients import Viewers
from vpstream.tests.test_login import PASSWORD, Clock, answer, verifier

BASE = "https://203.0.113.5:1338"
IP = {"X-Vpstream-Ip": "198.51.100.20"}


@pytest.fixture
def control(tmp_path: Path) -> Control:
    return Control(
        config.Config(stream_host="203.0.113.5"),
        config.Paths.under(tmp_path / "root"),
        FakeFirewall(),
        FakeServer(),
        log=lambda m: None,
    )


@pytest.fixture
def auth() -> login.Login:
    return login.Login(verifier(), login.Limiter(None, clock=Clock(), log=lambda m: None))


@pytest.fixture
def app(control: Control, auth: login.Login) -> Flask:
    return api.create_app(control.status, control, auth)


@pytest.fixture
def web(app: Flask) -> FlaskClient:
    return app.test_client()


def get(web: FlaskClient, path: str, **kw: Any) -> Any:
    return web.get(path, base_url=BASE, headers={**IP, **kw.pop("headers", {})}, **kw)


def post(web: FlaskClient, path: str, body: Any = None, **kw: Any) -> Any:
    headers = {**IP, **kw.pop("headers", {})}
    return web.post(path, base_url=BASE, json={} if body is None else body, headers=headers, **kw)


def set_cookie(response: Any) -> SimpleCookie:
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.getlist("Set-Cookie"):
        jar.load(header)
    return jar


def log_in(web: FlaskClient, password: str = PASSWORD, **kw: Any) -> tuple[Any, str]:
    """Log in as the page does. Returns the answer and the CSRF token."""
    challenge = post(web, "/api/v1/login/challenge", **kw).get_json()
    proof = answer(challenge, password)
    response = post(web, "/api/v1/login", {"nonce": challenge["nonce"], "proof": proof}, **kw)
    return response, (response.get_json() or {}).get("csrf", "")


def post_routes(app: Flask) -> list[str]:
    return sorted(
        rule.rule
        for rule in app.url_map.iter_rules()
        if "POST" in (rule.methods or ()) and rule.rule not in api.LOGIN_ROUTES
    )


def all_routes(app: Flask) -> list[str]:
    return sorted({rule.rule for rule in app.url_map.iter_rules()} - api.LOGIN_ROUTES)


# --- without the password


def test_only_the_login_page_and_its_two_calls_are_open(web: FlaskClient, app: Flask) -> None:
    page = get(web, "/login").get_data(as_text=True)
    loaded = re.findall(r'(?:src|href)="(/[^"]+)"', page)
    assert {"/assets/dashboard.css", "/assets/login.js", "/assets/pbkdf2.js"} <= set(loaded)
    for path in ("/login", *loaded):  # the page and everything it loads, before any login
        answer_ = get(web, path)
        assert answer_.status_code == 200, path
        assert "json" not in answer_.content_type, path
    assert get(web, "/").status_code == 303 and get(web, "/").location.endswith("/login")
    closed = [p for p in all_routes(app) if p not in ("/", "/static/<path:filename>")]
    assert f"{api.API}/status" in closed and f"{api.API}/stream/start" in closed
    for path in closed:
        answer_ = get(web, path)
        assert answer_.status_code in (401, 405), (path, answer_.status_code)
        assert b"vpstream" not in answer_.data or answer_.status_code == 405


def test_every_change_without_a_session_is_refused_before_it_runs(
    web: FlaskClient, app: Flask, control: Control, tmp_path: Path
) -> None:
    folder = tmp_path / "show"
    folder.mkdir()
    (folder / "a.mkv").write_bytes(b"x")
    routes = post_routes(app)
    assert f"{api.API}/stream/start" in routes and f"{api.API}/logout" in routes
    for route in routes:
        answer_ = post(web, route, {"dir": str(folder)})
        assert answer_.status_code == 401, route
        assert answer_.get_json() == {"error": "log in first", "login": True}
    assert control.stream is None


def test_the_unknown_is_hidden_behind_the_login_too(web: FlaskClient) -> None:
    assert get(web, "/api/v1/nothing-here").status_code == 401
    assert get(web, "/../etc/passwd").status_code in (401, 404)
    assert web.options("/api/v1/status", base_url=BASE).status_code == 401


# --- the login


def test_the_session_cookie_is_https_only_host_only_and_hidden_from_scripts(
    web: FlaskClient,
) -> None:
    response, csrf = log_in(web)
    assert response.status_code == 200 and csrf
    headers = response.headers.getlist("Set-Cookie")
    assert [h.split("=")[0] for h in headers] == ["__Host-vpstream-dash", "__Host-vpstream-device"]
    for header, seconds in zip(headers, (login.SESSION_SECONDS, login.DEVICE_SECONDS), strict=True):
        parts = {p.strip().split("=")[0].lower() for p in header.split(";")}
        assert {"secure", "httponly", "path", "max-age", "samesite"} <= parts
        assert "domain" not in parts, "__Host- cookies must not name a domain"
        assert "SameSite=Strict" in header and "Path=/" in header
        assert f"Max-Age={seconds}" in header
    assert response.headers["Cache-Control"] == "no-store"


def test_after_logging_in_the_dashboard_works_and_changes_need_the_token(
    web: FlaskClient, app: Flask
) -> None:
    _, csrf = log_in(web)
    assert get(web, "/").status_code == 200
    assert get(web, "/login").status_code == 303, "logged in: on to the dashboard"
    status = get(web, "/api/v1/status")
    assert status.status_code == 200 and "stream" in status.get_json()
    session = get(web, "/api/v1/session").get_json()
    assert session["login"] is True and session["csrf"] == csrf
    assert 0 < session["expires_in"] <= login.SESSION_SECONDS
    for route in post_routes(app):
        for headers in ({}, {login.CSRF_HEADER: "wrong"}, {login.CSRF_HEADER: csrf[:-1]}):
            refused = post(web, route, {}, headers=headers)
            assert refused.status_code == 403, (route, headers)
            assert refused.get_json()["login"] is True
    allowed = post(
        web, f"{api.API}/stream/policy", {"policy": "open"}, headers={login.CSRF_HEADER: csrf}
    )
    assert allowed.status_code == 400, "the guard let it through; the stream itself refused it"
    assert "error" in allowed.get_json()


def test_a_wrong_password_gives_no_cookie(web: FlaskClient) -> None:
    response, _ = log_in(web, "a wrong password")
    assert response.status_code == 401 and "Set-Cookie" not in response.headers
    assert get(web, "/api/v1/status").status_code == 401


def test_a_replayed_login_gets_nothing(web: FlaskClient) -> None:
    challenge = post(web, "/api/v1/login/challenge").get_json()
    body = {"nonce": challenge["nonce"], "proof": answer(challenge)}
    assert post(web, "/api/v1/login", body).status_code == 200
    other = FlaskClient(web.application)
    replay = post(other, "/api/v1/login", body)
    assert replay.status_code == 400 and "Set-Cookie" not in replay.headers


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"nonce": None, "proof": None},
        {"nonce": ["a"], "proof": {"b": 1}},
        {"nonce": "é" * 64, "proof": "é" * 64},
    ],
)
def test_a_forged_login_is_refused_cleanly(web: FlaskClient, body: dict[str, Any]) -> None:
    post(web, "/api/v1/login/challenge")
    response = post(web, "/api/v1/login", body)
    assert response.status_code == 400 and "Set-Cookie" not in response.headers


def test_a_login_that_is_not_json_is_refused(web: FlaskClient) -> None:
    response = web.post("/api/v1/login", base_url=BASE, data="nonce=a&proof=b", headers=IP)
    assert response.status_code == 400
    response = web.post(
        "/api/v1/login/challenge",
        base_url=BASE,
        data="x",
        headers={**IP, "Content-Type": "text/plain"},
    )
    assert response.status_code == 400, "a form or plain text from another site is no JSON"


def test_a_session_chosen_by_someone_else_is_never_taken(web: FlaskClient, auth: Any) -> None:
    """Session fixation: a cookie planted before the login is not what the login keeps."""
    planted = "A" * 43
    web.set_cookie(login.COOKIE, planted, domain="203.0.113.5", secure=True)
    response, _ = log_in(web)
    issued = set_cookie(response)[login.COOKIE].value
    assert issued != planted and auth.session(planted) is None
    # A real session that was sent along is ended by the new login.
    other = FlaskClient(web.application)
    old_response, _ = log_in(other)
    old = set_cookie(old_response)[login.COOKIE].value
    log_in(other)
    assert auth.session(old) is None


def test_logout_needs_the_token_and_ends_the_session(web: FlaskClient, auth: Any) -> None:
    response, csrf = log_in(web)
    token = set_cookie(response)[login.COOKIE].value
    assert post(web, "/api/v1/logout").status_code == 403
    assert auth.session(token) is not None
    out = post(web, "/api/v1/logout", headers={login.CSRF_HEADER: csrf})
    assert out.status_code == 200
    cleared = set_cookie(out)[login.COOKIE]
    assert cleared.value == "" and cleared["secure"] and cleared["path"] == "/"
    assert auth.session(token) is None
    # Another tab on the same browser, still holding the cookie, is out too.
    tab = FlaskClient(web.application)
    tab.set_cookie(login.COOKIE, token, domain="203.0.113.5", secure=True)
    assert get(tab, "/api/v1/status").status_code == 401


def test_an_expired_session_is_sent_back_to_the_login(control: Control) -> None:
    mono = Clock()
    auth = login.Login(verifier(), login.Limiter(None, log=lambda m: None), clock=mono)
    web = api.create_app(control.status, control, auth).test_client()
    log_in(web)
    assert get(web, "/api/v1/status").status_code == 200
    mono.now += login.SESSION_SECONDS + 1
    gone = get(web, "/api/v1/status")
    assert gone.status_code == 401 and gone.get_json()["login"] is True
    assert get(web, "/").location.endswith("/login")


# --- limits, and the address they count by


def test_wrong_passwords_lock_out_the_address_caddy_names(web: FlaskClient) -> None:
    for _ in range(login.FAILS_PER_ADDRESS):
        log_in(web, "a wrong password")
    blocked = post(web, "/api/v1/login/challenge")
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
    # Another address (as Caddy sets it) is not held up; what a viewer sends is replaced by
    # Caddy, so the header seen here is always the real address (see test_caddy).
    other, _ = log_in(web, headers={"X-Vpstream-Ip": "198.51.100.21"})
    assert other.status_code == 200


def test_a_big_body_is_refused_before_it_is_read(web: FlaskClient) -> None:
    response = post(web, "/api/v1/login", {"nonce": "a" * (api.MAX_BODY + 10), "proof": "b"})
    assert response.status_code == 413


def test_every_answer_refuses_to_be_framed(web: FlaskClient) -> None:
    for response in (get(web, "/login"), get(web, "/"), post(web, "/api/v1/login/challenge")):
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"


# --- cookies of the other port and the other mode


def test_the_stream_ports_cookie_does_not_open_the_dashboard(tmp_path: Path) -> None:
    """Cookies are not split by port: a browser sends the stream's cookie (set over plain
    HTTP on the same address) to the dashboard too. It must count for nothing there."""
    rig = Viewers(tmp_path)
    rig.start_browser()
    rig.visit("tv")
    rig.let_in("tv")
    stream_cookie = rig.cookies["tv"]
    auth = login.Login(verifier(), login.Limiter(None, log=lambda m: None))
    web = api.create_app(rig.control.status, rig.control, auth).test_client()
    web.set_cookie(gate.COOKIE, stream_cookie, domain="203.0.113.5")
    assert get(web, "/api/v1/status").status_code == 401
    web.set_cookie(login.COOKIE, stream_cookie, domain="203.0.113.5", secure=True)
    assert get(web, "/api/v1/status").status_code == 401, "not even under the dashboard's name"


def test_the_dashboard_cookie_means_nothing_to_the_stream_gate(tmp_path: Path) -> None:
    rig = Viewers(tmp_path)
    rig.start_browser()
    auth = login.Login(verifier(), login.Limiter(None, log=lambda m: None))
    web = api.create_app(rig.control.status, rig.control, auth).test_client()
    response, _ = log_in(web)
    token = set_cookie(response)[login.COOKIE].value
    viewer = public.create_app(rig.control).test_client()
    viewer.set_cookie(login.COOKIE, token)
    first = viewer.get("/aaa", headers={**IP, "Accept": "text/html"})
    assert first.status_code == 403, "a new browser on the stream waits, logged in or not"
    assert gate.COOKIE in set_cookie(first)


def test_the_tailnet_dashboard_and_the_socket_have_no_login(control: Control) -> None:
    web = api.create_app(control.status, control).test_client()
    assert web.get("/api/v1/session").get_json() == {"login": False}
    assert web.get("/api/v1/status").status_code == 200
    assert web.get("/login").status_code == 404
    assert web.post("/api/v1/login/challenge", json={}).status_code == 404
    # Changes work without a token, as the command line sends them.
    changed = web.post("/api/v1/stream/policy", json={"policy": "open"})
    assert changed.status_code == 400 and "stream" in changed.get_json()["error"]
    # A cookie from the public mode does nothing here either way.
    web.set_cookie(login.COOKIE, "A" * 43)
    assert web.get("/api/v1/status").status_code == 200


def test_a_session_of_one_daemon_is_worthless_to_the_next(control: Control) -> None:
    first = login.Login(verifier(), login.Limiter(None, log=lambda m: None))
    web = api.create_app(control.status, control, first).test_client()
    response, _ = log_in(web)
    token = set_cookie(response)[login.COOKIE].value
    restarted = login.Login(first.verifier, login.Limiter(None, log=lambda m: None))
    again = api.create_app(control.status, control, restarted).test_client()
    again.set_cookie(login.COOKIE, token, domain="203.0.113.5", secure=True)
    assert get(again, "/api/v1/status").status_code == 401


def test_a_browser_that_logged_in_before_gets_in_while_strangers_lock_everyone_else_out(
    web: FlaskClient,
) -> None:
    response, _ = log_in(web)
    assert response.status_code == 200
    post(
        web,
        "/api/v1/logout",
        headers={login.CSRF_HEADER: get(web, "/api/v1/session").get_json()["csrf"]},
    )
    stranger = FlaskClient(web.application)
    for n in range(login.FAILS_ALL // login.FAILS_PER_ADDRESS):
        for _ in range(login.FAILS_PER_ADDRESS):
            log_in(stranger, "a wrong password", headers={"X-Vpstream-Ip": f"2001:db8:0:{n}::1"})
    assert post(FlaskClient(web.application), "/api/v1/login/challenge").status_code == 429
    again, _ = log_in(web)  # the device cookie came back with it
    assert again.status_code == 200


def test_a_device_cookie_planted_by_someone_else_counts_for_nothing(web: FlaskClient) -> None:
    for _ in range(login.FAILS_PER_ADDRESS):
        log_in(web, "a wrong password")
    web.set_cookie(
        login.DEVICE_COOKIE, "A" * 22 + "." + "0" * 64, domain="203.0.113.5", secure=True
    )
    assert post(web, "/api/v1/login/challenge").status_code == 429
