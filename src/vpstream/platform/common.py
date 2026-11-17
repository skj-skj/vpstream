"""What every platform shares: the firewall as the daemon sees it, and the shape of what
each platform tells the person to run (`Hints`).

On Linux a root helper opens and closes the ports (`platform.linux.firewall`); elsewhere
there is none, and the daemon is given no firewall at all.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class Hints:
    """What the messages of every platform tell the person to run."""

    start: str  # start the daemon
    stop: str  # stop it
    restart: str  # start it again, for a new version or a new configuration
    dash_pass: str  # set the dashboard password
    pip_install: str  # install vpstream with the tests
    packages: dict[str, str]  # program -> the package that has it, where they differ
    package_manager: str  # what installs a package: "sudo apt install", "pkg install"
    own_installs: tuple[str, ...] = ()  # programs `vpstream install` puts in

    def install(self, program: str) -> str:
        """How to install a program vpstream needs."""
        if program in self.own_installs or self.packages.get(program) in self.own_installs:
            return f"vpstream install {self.packages.get(program, program)}"
        return f"{self.package_manager} {self.packages.get(program, program)}"
