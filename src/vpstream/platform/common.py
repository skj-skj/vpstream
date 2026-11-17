"""What every platform shares: the firewall as the daemon sees it.

On Linux a root helper opens and closes the ports (`platform.linux.firewall`); elsewhere
there is none, and the daemon is given no firewall at all.
"""

from __future__ import annotations

from typing import Protocol


class FirewallError(RuntimeError):
    pass


class Firewall(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...


def switch(fw: Firewall, action: str) -> str:
    """Open or close the port (`action` "open" or "close"); returns its state now, "open"
    or "closed". Raises FirewallError, after which the state is not known."""
    if action == "open":
        fw.open()
        return "open"
    fw.close()
    return "closed"
