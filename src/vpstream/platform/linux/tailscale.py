"""What vpstream needs from Tailscale: this host's tailnet address and name, and `serve`."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any

from vpstream.tools import Runner, tail


class TailscaleDown(RuntimeError):
    """Tailscale is missing, stopped or logged out, so there is no tailnet address."""


def ipv4(runner: Runner) -> str:
    """This host's Tailscale IPv4 address, from `tailscale ip -4`."""
    if not runner.which("tailscale"):
        raise TailscaleDown("tailscale was not found on PATH")
    res = runner.run(["tailscale", "ip", "-4"], timeout=15)
    if res.returncode != 0:
        raise TailscaleDown(f"tailscale ip -4 failed: {tail(res, stdout=True)}")
    lines = res.stdout.split()
    try:
        return str(ipaddress.IPv4Address(lines[0]))
    except (IndexError, ValueError):
        raise TailscaleDown(f"tailscale ip -4 printed no address: {res.stdout.strip()!r}") from None


# --- `tailscale serve`: publishing a loopback port on the tailnet over HTTPS


TIMEOUT = 30.0
# `tailscale status --json` prints its answer and exits 0 in every state, so the state is
# read from the answer. The plain `tailscale status` uses the same words.
_NOT_UP = {
    "Stopped": "Tailscale is stopped",
    "NeedsLogin": "Tailscale is logged out",
    "NeedsMachineAuth": "this machine is not yet approved by the tailnet admin",
}
_UP = ("Running", "Starting")


def _json(runner: Runner, *args: str, timeout: float = TIMEOUT) -> dict[str, Any]:
    if not runner.which("tailscale"):
        raise TailscaleDown("tailscale was not found on PATH")
    res = runner.run(["tailscale", *args], timeout=timeout)
    if res.returncode != 0:
        raise TailscaleDown(f"tailscale {' '.join(args)} failed: {tail(res, stdout=True)}")
    try:
        data = json.loads(res.stdout) if res.stdout.strip() else {}
    except ValueError as exc:
        raise TailscaleDown(f"tailscale {' '.join(args)} did not print JSON") from exc
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Node:
    name: str  # this host's name on the tailnet, without the trailing dot; "" when unknown
    https: bool  # whether the tailnet issues HTTPS certificates for that name


def node(runner: Runner, timeout: float = TIMEOUT) -> Node:
    """This host's tailnet name, from `tailscale status --json`.

    Raises TailscaleDown when Tailscale answers but is stopped or logged out.
    """
    data = _json(runner, "status", "--json", timeout=timeout)
    state = data.get("BackendState")
    if isinstance(state, str) and state not in _UP:
        raise TailscaleDown(_NOT_UP.get(state, f"Tailscale is not running (state {state})"))
    me = data.get("Self")
    name = str(me.get("DNSName") or "").rstrip(".") if isinstance(me, dict) else ""
    domains = data.get("CertDomains") or []
    return Node(name=name, https=bool(name) and name in domains)


def serve_config(runner: Runner, timeout: float = TIMEOUT) -> dict[str, Any]:
    """The serve configuration, from `tailscale serve status --json`. Readable without root."""
    return _json(runner, "serve", "status", "--json", timeout=timeout)


def local_target(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def serve_proxy(cfg: dict[str, Any], port: int) -> str | None:
    """Where serve sends requests for `/` on `port`: a URL, "" when the port is served in
    some other way, or None when serve does not use the port at all."""
    tcp = cfg.get("TCP")
    if not isinstance(tcp, dict) or str(port) not in tcp:
        return None
    web = cfg.get("Web")
    for host, site in web.items() if isinstance(web, dict) else ():
        if not host.endswith(f":{port}") or not isinstance(site, dict):
            continue
        handlers = site.get("Handlers")
        root = handlers.get("/") if isinstance(handlers, dict) else None
        if isinstance(root, dict) and isinstance(root.get("Proxy"), str):
            return str(root["Proxy"])
    return ""


def serve_target(runner: Runner, port: int, timeout: float = TIMEOUT) -> str | None:
    """Where serve sends `port` right now; see `serve_proxy` for the three answers."""
    return serve_proxy(serve_config(runner, timeout), port)


def serve_on_args(port: int) -> list[str]:
    """`tailscale` arguments that publish the loopback port on the same HTTPS port."""
    return ["serve", "--bg", f"--https={port}", local_target(port)]


def serve_off_args(port: int) -> list[str]:
    """`tailscale` arguments that stop serving one port. Fails when the port is not served."""
    return ["serve", f"--https={port}", "off"]
