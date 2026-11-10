"""The daemon's HTTP API: one Flask app, served on the unix socket and on the dashboard port.

The CLI and the dashboard call the same routes, so either can act on what the other started.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Flask, Response, jsonify
from markupsafe import escape
from werkzeug.exceptions import HTTPException

API = "/api/v1"


def create_app(status: Callable[[], dict[str, Any]]) -> Flask:
    """Build the app. `status` returns the daemon's current state as plain data."""
    app = Flask("vpstream", static_folder=None, template_folder=None)

    @app.get(f"{API}/status")
    def api_status() -> Response:
        return jsonify(status())

    @app.get("/")
    def index() -> str:
        info = status()
        stream = "a stream is running" if info.get("stream") else "no stream is running"
        return (
            "<!doctype html><meta charset=utf-8><title>vpstream</title>"
            f"<h1>vpstream {escape(info.get('version', ''))}</h1><p>The daemon is up; {stream}.</p>"
        )

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> tuple[Response, int]:
        return jsonify(error=exc.description or exc.name), exc.code or 500

    return app
