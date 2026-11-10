"""What viewers get on the stream port. Caddy passes every request here, over a unix socket.

This app is all a viewer can reach of the daemon: it has no route that changes anything. For
a video it answers with the name of the file, and Caddy sends the file itself.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any

from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException

from vpstream import caddy, media
from vpstream.control import Control
from vpstream.net import normal_ip
from vpstream.stream import Answer

M3U_TYPE = "audio/x-mpegurl"
_HOST = re.compile(r"[A-Za-z0-9.:\[\]-]{1,255}")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
html, body {{ margin: 0; background: #111; color: #eee; font: 26px/1.4 sans-serif; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 16px; }}
a {{ display: block; margin: 12px 0; padding: 16px 22px; background: #2a2a2a; color: #fff;
     border: 3px solid #555; border-radius: 10px; text-decoration: none; }}
a:focus {{ outline: none; border-color: #ffd400; background: #3a3a10; }}
b {{ display: inline-block; min-width: 2.2em; color: #ffd400; }}
</style></head><body><main>{body}</main></body></html>
"""


def index_page(alias: str, names: list[str]) -> str:
    """The episode list for a browser: one large link per video, for a TV remote."""
    links = "".join(
        f'<a href="/{escape(alias)}/{n}"><b>{n}</b> {escape(name)}</a>'
        for n, name in enumerate(names, 1)
    )
    return _PAGE.format(title=escape(alias), body=links)


def playlist(alias: str, names: list[str], host: str) -> str:
    """An M3U playlist of every video. `host` is the address the viewer used; without a
    usable one the entries are paths on the same server."""
    base = f"http://{host}" if _HOST.fullmatch(host) else ""
    lines = ["#EXTM3U"]
    for n, name in enumerate(names, 1):
        title = " ".join(name.split())  # a file name may hold a line break
        lines += [f"#EXTINF:-1,{title}", f"{base}/{alias}/{n}"]
    return "\n".join(lines) + "\n"


def _text(status: int, message: str) -> Response:
    return Response(message + "\n", status, mimetype="text/plain")


def respond(answer: Answer, host: str) -> Response:
    if answer.kind == "video":
        headers = {
            caddy.HEADER_FILE: caddy.file_header(answer.file),
            caddy.HEADER_TYPE: media.content_type(answer.file),
        }
        return Response(b"", 200, headers=headers)
    if answer.kind == "index":
        return Response(index_page(answer.alias, answer.names), mimetype="text/html")
    if answer.kind == "playlist":
        return Response(playlist(answer.alias, answer.names, host), mimetype=M3U_TYPE)
    if answer.kind == "denied":
        return _text(403, "This device is not allowed to watch. Ask the person who sent the link.")
    return _text(404, "Nothing here.")


def create_app(control: Control) -> Flask:
    app = Flask("vpstream-public", static_folder=None, template_folder=None)
    # A path is taken as it comes: no redirect may reveal that an alias exists.
    app.url_map.merge_slashes = False
    app.url_map.strict_slashes = False

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def visit(path: str) -> Response:
        ip = normal_ip(request.headers.get(caddy.HEADER_IP))
        if ip is None:  # not through Caddy
            return _text(400, "Bad request.")
        answer = control.visit(
            path,
            ip,
            request.headers.get("User-Agent", ""),
            "text/html" in request.headers.get("Accept", "").lower(),
        )
        response = respond(answer, request.host)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Exception)
    def failed(exc: Exception) -> Any:
        if isinstance(exc, HTTPException):
            return _text(exc.code or 500, exc.name + ".")
        app.logger.exception("a viewer's request failed")
        return _text(500, "Something went wrong.")

    return app
