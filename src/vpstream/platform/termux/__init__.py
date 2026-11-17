"""What differs on Termux (Android): where vpstream keeps its files, and what to run.

There is no service, no service user and no firewall: the daemon runs as the person who
uses Termux, started with `vpstream up`, and serves the local network only.
"""

from __future__ import annotations

import os
from pathlib import Path

from vpstream.platform.common import Hints

HINTS = Hints(
    start="vpstream up",
    stop="vpstream down",
    restart="vpstream down && vpstream up",
    dash_pass="vpstream dash-pass",
    pip_install='pip install "vpstream[dev]"',
    packages={
        "ffprobe": "ffmpeg",
        "mkvextract": "mkvtoolnix",
        "mkvmerge": "mkvtoolnix",
        "fc-list": "fontconfig-utils",
        "termux-wake-lock": "termux-tools",
    },
    package_manager="pkg install",
)
PACKAGES = "pkg install ffmpeg caddy mkvtoolnix"  # what vpstream needs, at once
STORAGE = Path("/storage/emulated/0")  # the phone's shared storage, after termux-setup-storage


def _xdg(name: str, default: str, environ: os._Environ[str] | dict[str, str]) -> Path:
    found = environ.get(name, "")
    # The specification says a relative path is to be ignored.
    return Path(found) if found.startswith("/") else Path(environ.get("HOME", "~")) / default


def paths(environ: os._Environ[str] | dict[str, str] | None = None) -> tuple[Path, ...]:
    """Config file, state, cache and run folders: the person's own, in Termux's home and
    prefix. Never on /storage, where links and unix sockets cannot be made."""
    env = os.environ if environ is None else environ
    prefix = Path(env.get("PREFIX") or "/data/data/com.termux/files/usr")
    return (
        _xdg("XDG_CONFIG_HOME", ".config", env) / "vpstream" / "config.toml",
        _xdg("XDG_STATE_HOME", ".local/state", env) / "vpstream",
        _xdg("XDG_CACHE_HOME", ".cache", env) / "vpstream",
        prefix / "var" / "run" / "vpstream",
    )
