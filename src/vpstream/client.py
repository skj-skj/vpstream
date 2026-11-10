"""Talk to the running daemon over its unix socket."""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any

from vpstream import api

TIMEOUT = 10.0


class DaemonUnavailable(RuntimeError):
    """The daemon cannot be reached; the message says why and what to do."""


class DaemonRefused(RuntimeError):
    """The daemon answered with an error."""


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


def call(socket_path: Path, method: str, route: str, timeout: float = TIMEOUT) -> Any:
    """Send one request to the daemon and return the decoded JSON answer."""
    conn = _UnixConnection(socket_path, timeout)
    try:
        conn.request(method, f"{api.API}/{route}", headers={"Connection": "close"})
        response = conn.getresponse()
        body = response.read()
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
        data = json.loads(body)
    except ValueError as exc:
        raise DaemonRefused(
            f"the daemon sent an answer that is not JSON ({response.status})"
        ) from exc
    if response.status >= 400:
        message = data.get("error") if isinstance(data, dict) else None
        raise DaemonRefused(f"the daemon refused: {message or response.status}")
    return data
