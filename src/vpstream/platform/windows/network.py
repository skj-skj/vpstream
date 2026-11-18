"""The local networks this PC is on: its private addresses with the adapter each is on, and
whether Windows treats that network as Public, which keeps every device on it out.

PowerShell is asked once (Get-NetIPAddress, Get-NetConnectionProfile); nothing here changes
anything, except `make_private`, which the person asks for with a button.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import socket
import sys
from dataclasses import dataclass
from typing import Any

from vpstream import local
from vpstream.platform.windows.shortcut import powershell
from vpstream.tools import Runner, tail

PUBLIC, PRIVATE, DOMAIN = 0, 1, 2  # NetworkCategory
# AddressState: Windows keeps the address of an adapter that is off (Wi-Fi turned off, no
# cable) as Tentative, and nothing can listen on it then; only a Preferred one is in use.
PREFERRED = 4
_APIPA = ipaddress.ip_network("169.254.0.0/16")  # no network handed out an address

LOOK = (
    "$a = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
    "Select-Object IPAddress, InterfaceAlias, InterfaceIndex, AddressState); "
    "$u = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' | "
    "ForEach-Object { $_.ifIndex }); "
    "$p = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | "
    "Select-Object InterfaceIndex, NetworkCategory, Name); "
    "ConvertTo-Json -Compress -Depth 3 @{addresses = $a; profiles = $p; up = $u}"
)


@dataclass(frozen=True)
class Address:
    ip: str
    adapter: str  # Windows' name for it: Wi-Fi, Ethernet, vEthernet (Default Switch)
    index: int
    public: bool  # Windows treats its network as Public: devices on it are kept out
    network: str  # what Windows calls the network (the Wi-Fi's name), or ""

    @property
    def label(self) -> str:
        return f"{self.ip} ({self.adapter})"


class NetworkError(RuntimeError):
    pass


def _rows(value: Any) -> list[dict[str, Any]]:
    """ConvertTo-Json writes one item without its list."""
    if isinstance(value, dict):
        value = [value]
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def parse(text: str) -> list[Address]:
    """The private addresses in use (Preferred, on an adapter that is up) that PowerShell
    listed, those on a network Windows knows first."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise NetworkError(f"Windows' list of networks could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise NetworkError("Windows' list of networks could not be read")
    profiles = {
        p.get("InterfaceIndex"): p for p in _rows(data.get("profiles")) if "InterfaceIndex" in p
    }
    up = data.get("up")
    up_list = up if isinstance(up, list) else [up] if up is not None else None
    found = []
    for row in _rows(data.get("addresses")):
        ip, index = str(row.get("IPAddress") or ""), row.get("InterfaceIndex")
        if local.private_problem(ip) is not None or ipaddress.ip_address(ip) in _APIPA:
            continue
        if row.get("AddressState", PREFERRED) != PREFERRED:
            continue  # its adapter is off
        if up_list is not None and index not in up_list:
            continue
        profile = profiles.get(index, {})
        found.append(
            Address(
                ip=ip,
                adapter=str(row.get("InterfaceAlias") or "?"),
                index=index if isinstance(index, int) else -1,
                public=profile.get("NetworkCategory") == PUBLIC,
                network=str(profile.get("Name") or ""),
            )
        )
    return sorted(found, key=lambda a: (a.index not in profiles, a.adapter.lower()))


def addresses(runner: Runner) -> list[Address]:
    res = runner.run(powershell(LOOK), timeout=30)
    if res.returncode != 0:
        raise NetworkError(f"Windows' list of networks could not be read: {tail(res)}")
    return parse(res.stdout)


def make_private(runner: Runner, index: int) -> None:
    """Have Windows treat the network of adapter `index` as a private (home) one, which
    lets the devices on it reach this PC where a rule allows. Needs administrator rights."""
    script = f"Set-NetConnectionProfile -InterfaceIndex {int(index)} -NetworkCategory Private"
    res = runner.run(powershell(script), timeout=60)
    if res.returncode != 0:
        raise NetworkError(f"Windows did not make the network private: {tail(res)}")


# --- whether an address is still in use, asked of Windows itself (once a second)


class _Unicast(ctypes.Structure):
    """MIB_UNICASTIPADDRESS_ROW (netioapi.h), with the address as SOCKADDR_INET's bytes."""

    _fields_ = [
        ("family", ctypes.c_ushort),
        ("port", ctypes.c_ushort),
        ("ipv4", ctypes.c_ubyte * 4),
        ("rest", ctypes.c_ubyte * 20),  # the rest of SOCKADDR_INET (an IPv6 address)
        ("luid", ctypes.c_ulonglong),
        ("index", ctypes.c_ulong),
        ("prefix_origin", ctypes.c_int),
        ("suffix_origin", ctypes.c_int),
        ("valid_lifetime", ctypes.c_ulong),
        ("preferred_lifetime", ctypes.c_ulong),
        ("prefix_length", ctypes.c_ubyte),
        ("skip_as_source", ctypes.c_ubyte),
        ("dad_state", ctypes.c_int),
        ("scope_id", ctypes.c_ulong),
        ("created", ctypes.c_longlong),
    ]


def address_states() -> dict[str, int]:
    """IPv4 address -> its state (DadState: 1 Tentative, 3 Deprecated, 4 Preferred), from
    Windows' own table; {} where it cannot be asked."""
    if sys.platform != "win32":
        return {}
    iphlpapi = ctypes.WinDLL("iphlpapi")  # type: ignore[attr-defined,unused-ignore]
    table = ctypes.c_void_p()
    if iphlpapi.GetUnicastIpAddressTable(socket.AF_INET, ctypes.byref(table)) != 0:
        return {}
    try:
        count = ctypes.c_ulong.from_address(table.value or 0).value
        first = (table.value or 0) + ctypes.sizeof(ctypes.c_ulonglong)  # NumEntries, aligned
        rows = (_Unicast * count).from_address(first)
        return {socket.inet_ntoa(bytes(row.ipv4)): row.dad_state for row in rows}
    finally:
        iphlpapi.FreeMibTable(table)


def in_use(address: str, states: Any = address_states) -> bool:
    """Whether this PC still uses `address`. Windows keeps the address of a Wi-Fi that was
    turned off, as Deprecated, and a socket can still be bound to it; only a Preferred one
    is in use. Where Windows cannot be asked, a socket is bound to it (`local.is_mine`)."""
    found = states()
    if not found:
        return local.is_mine(address)
    return bool(found.get(address) == PREFERRED)
