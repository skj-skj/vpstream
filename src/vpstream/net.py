"""Small helpers for addresses."""

from __future__ import annotations

import ipaddress
import socket

UNKNOWN_HOST = "<server-ip>"


def url_host(host: str) -> str:
    """The host as it goes in a URL; a wildcard address becomes a placeholder."""
    if host in ("", "0.0.0.0", "::"):
        return UNKNOWN_HOST
    return f"[{host}]" if ":" in host else host


def normal_ip(text: str | None) -> str | None:
    """One spelling for an IP address, or None when the text is not an address.

    An IPv4 address written as IPv6 (`::ffff:192.0.2.1`) is the same device as its plain
    form, so it becomes that.
    """
    try:
        ip = ipaddress.ip_address((text or "").strip().split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ip)


def outgoing_address() -> str:
    """The address this host uses to reach the internet, or "" when it has no route.

    Connecting a UDP socket only picks a route; nothing is sent.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # an address reserved for documentation
            return str(probe.getsockname()[0])
        except OSError:
            return ""
