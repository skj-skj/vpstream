"""The daemon's HTTP API: one Flask app, served on the unix socket and on the dashboard port.

The CLI and the dashboard call the same routes, so either can act on what the other started.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import resources
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from vpstream.control import Control, ControlError
from vpstream.gate import GateError
from vpstream.stream import NeedsConfirm, StreamError, Unreadable

API = "/api/v1"
# The `code` of a refusal the command line can put right: by giving the service access to
# the files, or by asking whether a long prepare should go ahead.
UNREADABLE = Unreadable.CODE
CONFIRM = NeedsConfirm.CODE
# Route -> (file in vpstream.assets, content type).
ASSETS = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/assets/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
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


def create_app(status: Callable[[], dict[str, Any]], control: Control | None = None) -> Flask:
    """Build the app. `status` returns the daemon's current state as plain data; `control`
    starts, stops and changes the stream."""
    app = Flask("vpstream", static_folder=None, template_folder=None)
    files = resources.files("vpstream.assets")

    @app.get(f"{API}/status")
    def api_status() -> Response:
        return jsonify(status())

    def asset(name: str, ctype: str) -> Callable[[], Response]:
        def send() -> Response:
            response = Response(files.joinpath(name).read_bytes(), content_type=ctype)
            response.headers["Cache-Control"] = "no-cache"
            return response

        return send

    for route, (name, ctype) in ASSETS.items():
        app.add_url_rule(route, endpoint=name, view_func=asset(name, ctype))

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

        for route, action in changes.items():
            app.add_url_rule(
                f"{API}/{route}", endpoint=route, view_func=change(action), methods=["POST"]
            )

    @app.errorhandler(StreamError)
    @app.errorhandler(GateError)
    def refused(exc: ValueError) -> tuple[Response, int]:
        details = exc.details if isinstance(exc, StreamError) else {}
        return jsonify(error=str(exc), **details), 400

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
