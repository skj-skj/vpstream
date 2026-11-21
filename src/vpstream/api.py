"""The daemon's HTTP API: one Flask app, served on the unix socket and on the dashboard port.

The CLI and the dashboard call the same routes, so either can act on what the other started.

The public dashboard gets an app of its own, built with a `Login`: every route but the login
page and its two calls then needs a session cookie, and every change also the session's
CSRF token in a header. The unix socket's app never asks for a password: who may use the
socket is decided by its file mode.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from typing import Any

from flask import Flask, Response, g, jsonify, redirect, request
from werkzeug.exceptions import HTTPException

from vpstream import login as auth
from vpstream.caddy import HEADER_IP
from vpstream.client import API
from vpstream.control import Control, ControlError
from vpstream.gate import GateError
from vpstream.net import normal_ip
from vpstream.stream import NeedsConfirm, StreamError, Unreadable

# The `code` of a refusal the command line can put right: by giving the service access to
# the files, or by asking whether a long prepare should go ahead.
UNREADABLE = Unreadable.CODE
CONFIRM = NeedsConfirm.CODE
# Route -> (file in vpstream.assets, content type).
_HTML, _JS = "text/html; charset=utf-8", "text/javascript; charset=utf-8"
ASSETS = {
    "/": ("dashboard.html", _HTML),
    "/assets/dashboard.js": ("dashboard.js", _JS),
    "/assets/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
}
LOGIN_ASSETS = {
    "/login": ("login.html", _HTML),
    "/assets/login.js": ("login.js", _JS),
    "/assets/pbkdf2.js": ("pbkdf2.js", _JS),
}
# What the public dashboard serves without a session: the login page, what it loads, and its
# two calls.
LOGIN_ROUTES = {*LOGIN_ASSETS, "/assets/dashboard.css", f"{API}/login/challenge", f"{API}/login"}
# Local mode, over plain HTTP: the session and the device as the page sends them (see _guard).
SESSION_HEADER = "X-Vpstream-Session"
DEVICE_HEADER = "X-Vpstream-Device"
MAX_BODY = 64 * 1024  # on the public dashboard; every change sent there is small
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",  # no page of another site shows the dashboard in a frame
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _body() -> dict[str, Any]:
    """The JSON object sent with a change.

    Only JSON is taken: a page on another site cannot make a browser send that without
    asking first, which the daemon never allows. That keeps such a page from starting a
    stream through the browser of someone on the tailnet.
    """
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        raise StreamError("send the request as a JSON object (Content-Type: application/json)")
    return data


def client_ip(ip_header: bool = True) -> str | None:
    """The viewer's address: from Caddy's header on the public dashboard, where Caddy
    replaces whatever the viewer sent under that name. Without Caddy in front (local mode,
    `ip_header` False) only the connection's own address counts."""
    if ip_header and (found := normal_ip(request.headers.get(HEADER_IP))):
        return found
    return normal_ip(request.remote_addr) or None


def create_app(
    status: Callable[[], dict[str, Any]],
    control: Control | None = None,
    login: auth.Login | None = None,
    certificate: Callable[[], dict[str, str] | None] = lambda: None,
    admin: dict[str, Callable[[], Any]] | None = None,
    *,
    secure: bool = True,
    ip_header: bool = True,
) -> Flask:
    """Build the app. `status` returns the daemon's current state as plain data; `control`
    starts, stops and changes the stream; `login`, for the public dashboard, guards it;
    `certificate` is the public dashboard's certificate authority, once there is one;
    `admin` are changes for the command line's socket only, such as a new password.

    In local mode the login is served over plain HTTP (`secure` False), with the session in
    a header instead of a cookie (see `_guard`), and with no Caddy in front (`ip_header`
    False)."""
    app = Flask("vpstream", static_folder=None, template_folder=None)
    files = resources.files("vpstream.assets")

    @app.get(f"{API}/status")
    def api_status() -> Response:
        return jsonify(status())

    @app.get(f"{API}/session")
    def api_session() -> Response:
        if login is None:
            return jsonify(login=False)
        session: auth.Session = g.session
        return jsonify(login=True, csrf=session.csrf, expires_in=login.seconds_left(session))

    @app.get(f"{API}/certificate")
    def api_certificate() -> Response | tuple[Response, int]:
        cert = certificate()
        if cert is None:
            return jsonify(error="the dashboard has no certificate (it is not public)"), 404
        return jsonify(cert)

    def asset(name: str, ctype: str) -> Callable[[], Response]:
        def send() -> Response:
            response = Response(files.joinpath(name).read_bytes(), content_type=ctype)
            response.headers["Cache-Control"] = "no-cache"
            return response

        return send

    for route, (name, ctype) in (ASSETS | (LOGIN_ASSETS if login else {})).items():
        app.add_url_rule(route, endpoint=name, view_func=asset(name, ctype))

    if login is not None:
        _guard(app, login, secure, ip_header)

    @app.after_request
    def harden(response: Response) -> Response:
        response.headers.update(SECURITY_HEADERS)
        if request.path.startswith(f"{API}/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    if control is not None:

        @app.get(f"{API}/prepared")
        def api_prepared() -> Response:
            return jsonify(prepared=control.prepared())

        # Each change answers with the new status, so the caller shows it without asking again.
        changes: dict[str, Callable[[dict[str, Any]], Any]] = {
            "stream/start": control.start,
            "stream/stop": lambda body: control.stop(),
            "stream/alias": lambda body: control.set_alias(body.get("alias")),
            "stream/policy": lambda body: control.set_policy(body.get("policy")),
            "stream/unlock": lambda body: control.unlock(),
            "devices": lambda body: control.set_device(body.get("device"), body.get("allowed")),
            "clients": control.set_clients,
            "together": control.together,
            "prepare": control.prepare,
            "prepare/cancel": control.cancel_prepare,
            "prepared/rm": control.remove_prepared,
        }

        def change(action: Callable[[dict[str, Any]], Any]) -> Callable[[], Response]:
            def run() -> Response:
                extra = action(_body())
                # The new status, and what the change itself answers (a plan, a count).
                return jsonify({**(extra if isinstance(extra, dict) else {}), **status()})

            return run

        def without_body(run: Callable[[], Any]) -> Callable[[dict[str, Any]], Any]:
            return lambda body: run()

        for route, run_admin in (admin or {}).items():
            changes[route] = without_body(run_admin)

        for route, action in changes.items():
            app.add_url_rule(
                f"{API}/{route}", endpoint=route, view_func=change(action), methods=["POST"]
            )

    @app.errorhandler(StreamError)
    @app.errorhandler(GateError)
    def refused(exc: ValueError) -> tuple[Response, int]:
        details = exc.details if isinstance(exc, StreamError) else {}
        return jsonify(error=str(exc), **details), 400

    @app.errorhandler(auth.LoginRefused)
    def login_refused(exc: auth.LoginRefused) -> tuple[Response, int]:
        response = jsonify(error=str(exc))
        if exc.retry_after:
            response.headers["Retry-After"] = str(max(1, round(exc.retry_after)))
        return response, exc.status

    @app.errorhandler(ControlError)
    def failed(exc: ControlError) -> tuple[Response, int]:
        return jsonify(error=str(exc)), 503

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> tuple[Response, int]:
        return jsonify(error=exc.description or exc.name), exc.code or 500

    @app.errorhandler(Exception)
    def broke(exc: Exception) -> tuple[Response, int]:
        app.logger.exception("a request to the daemon failed")
        return jsonify(error=f"the daemon failed: {exc}"), 500

    return app


def _private_cookie(response: Response, name: str, value: str, seconds: int) -> None:
    """__Host-, Secure and Path=/: sent over HTTPS only, so never to the plain-HTTP stream
    port on the same address, and never set by it. HttpOnly: no script reads it. Strict: no
    other site's page makes the browser send it."""
    response.set_cookie(
        name,
        value,
        max_age=seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


def _guard(app: Flask, login: auth.Login, secure: bool = True, ip_header: bool = True) -> None:
    """Put the dashboard behind its password.

    Over HTTPS (`secure`) the session and the device are __Host- cookies. Over plain HTTP
    (local mode) they are no cookies at all: a browser sends a cookie to every port of the
    host, so to any other program's server on 127.0.0.1 or the same address, which could
    take the session with it. There the login answers with both, the page keeps them in its
    own storage, which is the page's alone (scheme, host and port), and sends them as
    headers. The page itself holds nothing, so it is served to anyone; what it asks for is
    not."""
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    open_paths = LOGIN_ROUTES if secure else {*LOGIN_ROUTES, *ASSETS}

    def token() -> str | None:
        return request.cookies.get(auth.COOKIE) if secure else request.headers.get(SESSION_HEADER)

    def device() -> str | None:
        if secure:
            return request.cookies.get(auth.DEVICE_COOKIE)
        return request.headers.get(DEVICE_HEADER)

    @app.before_request
    def check_session() -> Any:
        session = login.session(token())
        if request.path in open_paths:
            # A logged-in browser that opens the login page goes on to the dashboard.
            on_login = secure and session and request.path == "/login"
            return redirect("/", 303) if on_login else None
        if session is None:
            if request.path == "/" and request.method in ("GET", "HEAD"):
                return redirect("/login", 303)
            return jsonify(error="log in first", login=True), 401
        if request.method not in ("GET", "HEAD") and not auth.csrf_ok(
            session, request.headers.get(auth.CSRF_HEADER)
        ):
            return jsonify(error="the page is out of date; reload it", login=True), 403
        g.session = session
        return None

    @app.post(f"{API}/login/challenge")
    def challenge() -> Response:
        # JSON only, like every change: another site's page cannot send it without asking
        # first, so it cannot use up the nonces of a visitor's address.
        _body()
        return jsonify(login.challenge(client_ip(ip_header), device()))

    @app.post(f"{API}/login")
    def log_in() -> Response:
        body = _body()
        granted = login.login(client_ip(ip_header), body.get("nonce"), body.get("proof"), device())
        # Whatever session came along is dropped: a session is only ever one made here, now.
        login.logout(token())
        session = granted.session
        answer = {"csrf": session.csrf, "expires_in": login.seconds_left(session)}
        if not secure:
            return jsonify(**answer, session=granted.token, device=granted.device)
        response = jsonify(**answer)
        _private_cookie(response, auth.COOKIE, granted.token, auth.SESSION_SECONDS)
        _private_cookie(response, auth.DEVICE_COOKIE, granted.device, auth.DEVICE_SECONDS)
        return response

    @app.post(f"{API}/logout")
    def log_out() -> Response:
        login.logout(token())
        response = jsonify(login=True)
        if secure:
            response.delete_cookie(
                auth.COOKIE, path="/", secure=True, httponly=True, samesite="Strict"
            )
        return response
