"""Small helpers for addresses."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable

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


def _route_from(family: socket.AddressFamily, target: str) -> str:
    """The address this host would send from to `target`, or "" without a route. Connecting
    a UDP socket only picks a route; nothing is sent."""
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((target, 9))
            return str(probe.getsockname()[0])
    except OSError:
        return ""


def outgoing_address() -> str:
    """The IPv4 address this host uses to reach the internet, or "" when it has no route."""
    return _route_from(socket.AF_INET, "192.0.2.1")  # reserved for documentation


def outgoing_address6() -> str:
    """The same for IPv6, when it is a global address; "" otherwise."""
    found = normal_ip(_route_from(socket.AF_INET6, "2001:db8::1"))
    return found if found and ipaddress.ip_address(found).is_global else ""


def public_host(configured: str, find: Callable[[], str] | None = None) -> str:
    """The address URLs name: the configured one, or the IPv4 address found."""
    return configured or (find or outgoing_address)()


def public_hosts(
    configured: str,
    find: Callable[[], str] | None = None,
    find6: Callable[[], str] | None = None,
) -> list[str]:
    """Every address a viewer may reach this host by, the one URLs name first: what the
    public dashboard's certificates are made for."""
    found = ((find or outgoing_address)(), (find6 or outgoing_address6)())
    return list(dict.fromkeys(h for h in (configured, *found) if h))


def url(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{url_host(host)}:{port}"
