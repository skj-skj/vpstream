"""Talk to the running daemon over its unix socket."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any

from vpstream import api

TIMEOUT = 10.0
CHANGE_TIMEOUT = 90.0  # a start or a stop waits for Caddy and for the firewall helper


class DaemonUnavailable(RuntimeError):
    """The daemon cannot be reached; the message says why and what to do."""


class DaemonRefused(RuntimeError):
    """The daemon answered with an error. `data` is its whole answer."""

    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.data = data or {}


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self._path))
        except OSError:
            sock.close()
            raise
        self.sock = sock


def call(
    socket_path: Path,
    method: str,
    route: str,
    payload: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> Any:
    """Send one request to the daemon and return the decoded JSON answer. A `payload` is
    sent as JSON, and makes the request a change, which may take longer."""
    headers = {"Connection": "close"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload)
    conn = _UnixConnection(socket_path, timeout or (TIMEOUT if body is None else CHANGE_TIMEOUT))
    try:
        conn.request(method, f"{api.API}/{route}", body=body, headers=headers)
        response = conn.getresponse()
        answer = response.read()
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonUnavailable(
            f"the vpstream daemon is not running (no answer on {socket_path}). "
            "Start it with: sudo systemctl start vpstream (the first time: sudo vpstream init)"
        ) from exc
    except PermissionError as exc:
        raise DaemonUnavailable(
            f"no permission to use {socket_path}. Your user must be in the vpstream group: "
            "log out and in again if it was just added, or run the command with sudo"
        ) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise DaemonUnavailable(f"the vpstream daemon did not answer: {exc}") from exc
    finally:
        conn.close()
    try:
        data = json.loads(answer)
    except ValueError as exc:
        raise DaemonRefused(
            f"the daemon sent an answer that is not JSON ({response.status})"
        ) from exc
    if response.status >= 400:
        details = data if isinstance(data, dict) else {}
        message = details.get("error") or response.status
        raise DaemonRefused(f"the daemon refused: {message}", details)
    return data
