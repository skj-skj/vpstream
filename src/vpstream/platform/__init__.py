"""Which platform vpstream runs on, and what differs between them.

Linux is the VPS vpstream was built for: a systemd service under its own user, a root
firewall helper, ufw, and the dashboard on the tailnet or public behind a password. Termux
(Android) runs everything as the person who uses it, on the local network only (see
`vpstream.local`). Windows does too, from a window instead of the command line (see
`platform.windows`).

The platform is found once. `$VPSTREAM_PLATFORM` (linux, termux or windows) overrides it for the
command line and the daemon; tests use `override`. The firewall helper never asks: it is
Linux-only and always checks what it trusts as root.
"""

from __future__ import annotations

import functools
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vpstream.platform.common import Hints

LINUX = "linux"
TERMUX = "termux"
WINDOWS = "windows"
PLATFORMS = (LINUX, TERMUX, WINDOWS)
ENV = "VPSTREAM_PLATFORM"


class Unsupported(RuntimeError):
    """vpstream does not run here (yet)."""


def detect(
    system: str | None = None, environ: dict[str, str] | os._Environ[str] | None = None
) -> str:
    """The platform of this host: Termux by its Python (sys.platform "android" from 3.13)
    or by the variables Termux sets; Windows; Linux otherwise. Raises Unsupported elsewhere."""
    system = sys.platform if system is None else system
    env = os.environ if environ is None else environ
    if system == "android" or env.get("TERMUX_VERSION"):
        return TERMUX
    if system.startswith("linux"):
        return LINUX
    if system == "win32":
        return WINDOWS
    if system == "cygwin":
        raise Unsupported("vpstream does not run on Cygwin; use the Windows Python from python.org")
    raise Unsupported(f"vpstream does not run on this system ({system}) yet")


_override: str | None = None


@functools.cache
def _found() -> str:
    chosen = os.environ.get(ENV, "")
    if chosen:
        if chosen not in PLATFORMS:
            raise Unsupported(f"{ENV} must be one of {', '.join(PLATFORMS)}, not {chosen!r}")
        return chosen
    return detect()


def current() -> str:
    """The platform vpstream acts for: found once, or what `override` set."""
    return _override or _found()


def override(name: str | None) -> None:
    """Act as on another platform (None: as found). For tests."""
    global _override
    if name is not None and name not in PLATFORMS:
        raise ValueError(f"not a platform: {name!r}")
    _override = name
    _found.cache_clear()


def is_termux() -> bool:
    return current() == TERMUX


def is_windows() -> bool:
    return current() == WINDOWS


def per_user() -> bool:
    """Whether vpstream runs as the person who uses it, on the local network only, with no
    service, no service user and no dashboard password of root's: Termux and Windows."""
    return current() in (TERMUX, WINDOWS)


def hints() -> Hints:
    """What this platform's messages tell the person to run."""
    if is_windows():
        from vpstream.platform import windows

        return windows.HINTS
    if is_termux():
        from vpstream.platform import termux

        return termux.HINTS
    from vpstream.platform import linux

    return linux.HINTS
