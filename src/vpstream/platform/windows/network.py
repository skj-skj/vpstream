"""The local networks this PC is on: its private addresses with the adapter each is on, and
whether Windows treats that network as Public, which keeps every device on it out.

PowerShell is asked once (Get-NetIPAddress, Get-NetConnectionProfile); nothing here changes
anything, except `make_private`, which the person asks for with a button.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from typing import Any

from vpstream import local
from vpstream.platform.windows.shortcut import powershell
from vpstream.tools import Runner, tail

PUBLIC, PRIVATE, DOMAIN = 0, 1, 2  # NetworkCategory
_APIPA = ipaddress.ip_network("169.254.0.0/16")  # no network handed out an address

LOOK = (
    "$a = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
    "Select-Object IPAddress, InterfaceAlias, InterfaceIndex); "
    "$p = @(Get-NetConnectionProfile -ErrorAction SilentlyContinue | "
    "Select-Object InterfaceIndex, NetworkCategory, Name); "
    "ConvertTo-Json -Compress -Depth 3 @{addresses = $a; profiles = $p}"
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
    """The private addresses PowerShell listed, those on a network Windows knows first."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise NetworkError(f"Windows' list of networks could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise NetworkError("Windows' list of networks could not be read")
    profiles = {
        p.get("InterfaceIndex"): p for p in _rows(data.get("profiles")) if "InterfaceIndex" in p
    }
    found = []
    for row in _rows(data.get("addresses")):
        ip, index = str(row.get("IPAddress") or ""), row.get("InterfaceIndex")
        if local.private_problem(ip) is not None or ipaddress.ip_address(ip) in _APIPA:
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
