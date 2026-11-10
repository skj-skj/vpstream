"""What vpstream needs to know from Tailscale: this host's tailnet address."""

from __future__ import annotations

import ipaddress

from vpstream.tools import Runner, tail

INTERFACE = "tailscale0"


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
