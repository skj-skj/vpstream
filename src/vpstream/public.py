"""What viewers get on the stream port. Caddy passes every request here, over a unix socket.

This app is all a viewer can reach of the daemon. The one thing a viewer changes is its own
client record, by the reports of its player (see `clients`); nothing else here changes
anything. For a video it answers with the name of the file, and Caddy sends the file itself.
"""

from __future__ import annotations

import os
import re
from html import escape
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from vpstream import caddy, gate, media, player, text
from vpstream.control import Control
from vpstream.net import normal_ip
from vpstream.stream import Answer

M3U_TYPE = "audio/x-mpegurl"
COOKIE_SECONDS = 30 * 24 * 3600  # the cookie outlives a TV turned off; its stream may not
MAX_VALUES, MAX_VALUE = 16, 32  # what is read of a report's query
_HOST = re.compile(r"[A-Za-z0-9.:\[\]-]{1,255}")

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>{head}
<style>
html, body {{ margin: 0; background: #111; color: #eee; font: 26px/1.4 sans-serif; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 16px; }}
a {{ display: block; margin: 12px 0; padding: 16px 22px; background: #2a2a2a; color: #fff;
     border: 3px solid #555; border-radius: 10px; text-decoration: none; }}
a:focus {{ outline: none; border-color: #ffd400; background: #3a3a10; }}
b {{ display: inline-block; min-width: 2.2em; color: #ffd400; }}
i {{ color: #aaa; }}
p {{ margin: 18px 0; }}
.code {{ font-size: 64px; letter-spacing: 12px; color: #ffd400; }}
</style></head><body><main>{body}</main></body></html>
"""
REFRESH_SECONDS = 10


def html_page(title: str, body: str, refresh: int = 0) -> str:
    """A page in the style of the index. `body` is HTML; `refresh` reloads it."""
    head = f'\n<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return _PAGE.format(title=escape(title), head=head, body=body)


def index_page(alias: str, names: list[str], ready: list[bool] | None = None) -> str:
    """The episode list for a browser: one large link per video, for a TV remote. A video
    that cannot be played yet says so."""
    marks = ready or [True] * len(names)
    links = "".join(
        f'<a href="/{escape(alias)}/{n}"><b>{n}</b> {escape(name)}'
        + ("" if ok else " <i>(not ready)</i>")
        + "</a>"
        for n, (name, ok) in enumerate(zip(names, marks, strict=True), 1)
    )
    return html_page(alias, links)


def waiting_page(code: str) -> str:
    """What a browser the owner has not approved yet sees. It asks again by itself."""
    return html_page(
        "Waiting",
        "<p>Waiting to be let in. Tell the person who sent the link this code:</p>"
        f'<p class="code">{escape(code)}</p><p><i>This page reloads by itself.</i></p>',
        REFRESH_SECONDS,
    )


def preparing_page(job: dict[str, Any] | None) -> str:
    """A video whose base version is being made, or not made at all."""
    if job is None:
        return html_page(
            "Not ready",
            "<p>This video is not prepared for browsers yet.</p>"
            "<p><i>Ask the person who sent the link to prepare it.</i></p>",
        )
    left = job.get("seconds_left")
    when = f", about {text.duration(left)} left" if isinstance(left, int | float) else ""
    done = round(100 * float(job.get("progress") or 0))
    state = "waiting for its turn" if job.get("state") == "queued" else f"{done}% done{when}"
    return html_page(
        "Preparing",
        f"<p>This video is being prepared for browsers: {escape(state)}.</p>"
        "<p><i>This page reloads by itself and plays the video when it is ready.</i></p>",
        REFRESH_SECONDS,
    )


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


def _html(status: int, page: str) -> Response:
    return Response(page, status, mimetype="text/html")


def respond(answer: Answer, host: str, wants_html: bool = False) -> Response:
    if answer.kind in ("video", "file"):
        # Looked at again on every request: a file that was swapped for a link or a folder
        # after the start is not followed to wherever it leads now.
        if os.path.realpath(answer.file) != answer.file or not os.path.isfile(answer.file):
            return _text(404, "Nothing here.")
        headers = {
            caddy.HEADER_FILE: answer.rel or str(answer.number),
            caddy.HEADER_TYPE: media.content_type(answer.file),
        }
        return Response(b"", 200, headers=headers)
    if answer.kind == "index":
        return _html(200, index_page(answer.alias, answer.names, answer.ready))
    if answer.kind == "playlist":
        return Response(playlist(answer.alias, answer.names, host), mimetype=M3U_TYPE)
    if answer.kind == "player":
        return _html(200, player.page(answer.data))
    if answer.kind == "asset":
        return Response(player.asset(answer.asset), content_type=player.ASSETS[answer.asset])
    if answer.kind == "client":
        return jsonify(answer.data)
    if answer.kind == "redirect":
        return Response(b"", 302, headers={"Location": answer.location})
    if answer.kind == "waiting":
        return _html(answer.status, waiting_page(str(answer.data.get("code", ""))))
    if answer.kind in ("preparing", "notready"):
        if wants_html and (answer.data or answer.kind == "notready"):
            return _html(answer.status, preparing_page(answer.data or None))
        response = _text(
            answer.status,
            "This video is being prepared. Try again in a minute."
            if answer.kind == "preparing"
            else "This video is not prepared for browsers yet.",
        )
        response.headers["Retry-After"] = "30"
        return response
    if answer.kind == "denied":
        return _text(
            answer.status, "This device is not allowed to watch. Ask the person who sent the link."
        )
    return _text(answer.status, "Nothing here.")


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
        wants_html = "text/html" in request.headers.get("Accept", "").lower()
        answer = control.visit(
            path,
            ip,
            request.headers.get("User-Agent", ""),
            wants_html,
            request.cookies.get(gate.COOKIE),
            # One value per name: a report is a handful of short numbers.
            {k: v[:MAX_VALUE] for k, v in list(request.args.items())[:MAX_VALUES]}
            if request.headers.get(player.REPORT_HEADER) == "1"
            else None,
        )
        response = respond(answer, request.host, wants_html)
        response.headers["Cache-Control"] = "no-store"
        if answer.cookie:
            # Plain HTTP, so not Secure. HttpOnly: no script needs it. Lax: a page on another
            # site cannot fetch with it, but a link to the stream still carries it.
            response.set_cookie(
                gate.COOKIE,
                answer.cookie,
                max_age=COOKIE_SECONDS,
                path="/",
                httponly=True,
                samesite="Lax",
            )
        return response

    @app.errorhandler(Exception)
    def failed(exc: Exception) -> Any:
        if isinstance(exc, HTTPException):
            return _text(exc.code or 500, exc.name + ".")
        app.logger.exception("a viewer's request failed")
        return _text(500, "Something went wrong.")

    return app
