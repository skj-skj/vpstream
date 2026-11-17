"""Local mode: the stream and the dashboard on the local network only.

Everything listens on one private address of this host (192.168.x.x, 10.x.x.x,
172.16.x.x to 172.31.x.x, 169.254.x.x; for IPv6 fc00::/7), never on every interface.
That is what keeps them off the internet where there is no firewall (Termux): an address
a router hands out on a home network is not reachable from outside it. A carrier's shared
address (100.64.0.0/10, mobile data) is not a local network and is refused.

The address is looked at again every second while a stream runs. When it is gone (the
Wi-Fi was turned off, the phone moved to mobile data) the stream is stopped.
"""

from __future__ import annotations

import array
import contextlib
import ipaddress
import socket
import struct

with contextlib.suppress(ImportError):  # Windows: interface_addresses finds none
    import fcntl

from vpstream import net

WARNING = (
    "Local mode: use it only on your own Wi-Fi or a local network you control, never on "
    "public Wi-Fi. Anyone on this network can reach the stream and dashboard ports."
)
REMINDER = "local network only; never on public Wi-Fi"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
    )
)
# IPv6 link-local needs the interface with it (fe80::1%wlan0), which neither a listener
# without it nor an address in a browser carries.
_LINK_LOCAL6 = ipaddress.ip_network("fe80::/10")


class LocalError(RuntimeError):
    """No usable local address; the message says why."""


def private_problem(address: str) -> str | None:
    """Why `address` is not one of a local network, or None when it is."""
    found = net.normal_ip(address)
    if found is None:
        return f"{address!r} is not an IP address"
    ip = ipaddress.ip_address(found)
    if ip in _LINK_LOCAL6:
        return f"{found} is an IPv6 link-local address, which cannot be served; use the IPv4 one"
    if ip in _CGNAT:
        return (
            f"{found} is a carrier's shared address (mobile data, or a VPN such as Tailscale), "
            "not a local network"
        )
    if not any(ip in network for network in _PRIVATE):
        return f"{found} is not a private address of a local network"
    return None


def network_of(address: str) -> str:
    """The private block `address` is in (for example 192.168.0.0/16): what a firewall lets
    in to reach it. Raises LocalError for any other address."""
    if problem := private_problem(address):
        raise LocalError(problem)
    ip = ipaddress.ip_address(net.normal_ip(address) or address)
    return str(next(network for network in _PRIVATE if ip in network))


def is_mine(address: str) -> bool:
    """Whether this host still has `address`: a socket can be bound to it. Nothing is sent."""
    found = net.normal_ip(address)
    if found is None:
        return False
    family = socket.AF_INET6 if ":" in found else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((found, 0))
    except OSError:
        return False
    return True


_IFREQ = 40 if struct.calcsize("P") == 8 else 32  # struct ifreq: a name and a union
_SIOCGIFCONF = 0x8912
_LOCAL_FIRST = ("wlan", "eth", "en", "wl")  # Wi-Fi and wired before anything else


def interface_addresses() -> list[tuple[str, str]]:
    """(interface, IPv4 address) of every interface that has one, from the kernel's list
    (SIOCGIFCONF, which Android allows where it hides netlink and /sys/class/net). Empty
    where that cannot be asked."""
    buffer = array.array("B", bytes(_IFREQ * 64))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            asked = struct.pack("iL", len(buffer), buffer.buffer_info()[0])
            length = struct.unpack("iL", fcntl.ioctl(probe.fileno(), _SIOCGIFCONF, asked))[0]
    except (OSError, NameError):  # NameError: no fcntl (Windows)
        return []
    raw = buffer.tobytes()[:length]
    found = []
    for start in range(0, len(raw) - _IFREQ + 1, _IFREQ):
        name = raw[start : start + 16].split(b"\0", 1)[0].decode("ascii", "replace")
        found.append((name, socket.inet_ntoa(raw[start + 20 : start + 24])))
    return found


def _local_interface() -> str:
    """A private address of a Wi-Fi or wired interface, for when the way to the internet is
    elsewhere (a VPN such as Tailscale, or mobile data while the Wi-Fi has no internet)."""
    private = [(n, a) for n, a in interface_addresses() if private_problem(a) is None]
    private.sort(key=lambda found: not found[0].startswith(_LOCAL_FIRST))
    return private[0][1] if private else ""


def find(configured: str = "") -> str:
    """The address to serve on: the configured one, or the one this host reaches the
    network from, or else a private address of its Wi-Fi. Raises LocalError unless it is
    private and this host has it."""
    address = net.normal_ip(configured) if configured else net.outgoing_address()
    if not configured and (not address or private_problem(address)):
        address = _local_interface() or address
    if not address:
        raise LocalError(
            f"{configured!r} is not an IP address"
            if configured
            else "this device is on no network (no route); connect to the Wi-Fi first"
        )
    if problem := private_problem(address):
        raise LocalError(
            f"{problem}. Local mode serves your own Wi-Fi or local network only; on one, "
            "set its address as [network] address (Termux) or --address"
        )
    if not is_mine(address):
        raise LocalError(f"{address} is not an address of this device now; is the Wi-Fi on?")
    return address
