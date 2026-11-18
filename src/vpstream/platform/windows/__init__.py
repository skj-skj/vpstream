"""What differs on Windows: a home PC on the home network, run from a window.

vpstream runs as the person who uses the PC, on the local network only, like Termux. There
is no daemon to start and no dashboard: the window (`platform.windows.gui`) runs the stream
in its own process and is the dashboard. The window runs as administrator, for the
firewall rule of the stream port and for installers.

The programs vpstream installs itself go to %LOCALAPPDATA%\\vpstream\\tools and are found
there without touching the PATH of the system or of the user: vpstream puts them first on
its own (`prepare_process`).

Everything here imports on Linux too; what only Windows has (ctypes.windll, winreg) is
reached inside functions, after a check of `sys.platform`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from vpstream.platform.common import Hints

WINDOW = "the vpstream window (run vpstream, or open it from the desktop icon)"
INSTALL = "vpstream windows-install"
HINTS = Hints(
    start=WINDOW,
    stop=f"Stop in {WINDOW}",
    restart=f"close {WINDOW} and open it again",
    dash_pass="(Windows has no dashboard: the window is the dashboard)",
    pip_install='pip install --force-reinstall "vpstream[dev]"',
    packages={"ffprobe": "ffmpeg", "mkvextract": "mkvtoolnix", "mkvmerge": "mkvtoolnix"},
    package_manager="winget install",
    own_installs=("ffmpeg", "caddy", "mkvtoolnix"),
    own_install=INSTALL,
)
NAME = "vpstream"
# Where MKVToolNix's installer puts it; it does not add itself to PATH.
MKVTOOLNIX_DIRS = (r"C:\Program Files\MKVToolNix", r"C:\Program Files (x86)\MKVToolNix")


def _folder(env: os._Environ[str] | dict[str, str], name: str, *fallback: str) -> Path:
    found = env.get(name, "")
    if found:
        return Path(found)
    return Path(env.get("USERPROFILE") or Path.home()).joinpath(*fallback)


def app_dirs(environ: os._Environ[str] | dict[str, str] | None = None) -> tuple[Path, Path]:
    """%APPDATA%\\vpstream (the configuration, which roams with the account) and
    %LOCALAPPDATA%\\vpstream (everything else, kept on this PC)."""
    env = os.environ if environ is None else environ
    roaming = _folder(env, "APPDATA", "AppData", "Roaming") / NAME
    local = _folder(env, "LOCALAPPDATA", "AppData", "Local") / NAME
    return roaming, local


def paths(environ: os._Environ[str] | dict[str, str] | None = None) -> tuple[Path, ...]:
    """Config file, state, cache and run folders: the person's own."""
    roaming, local = app_dirs(environ)
    return (roaming / "config.toml", local / "state", local / "cache", local / "run")


def tools_dir(environ: os._Environ[str] | dict[str, str] | None = None) -> Path:
    """Where vpstream puts the programs it downloads; `windows-uninstall` removes it."""
    return app_dirs(environ)[1] / "tools"


def program_dirs(environ: os._Environ[str] | dict[str, str] | None = None) -> list[Path]:
    """The folders vpstream looks in for its programs, before PATH: its own tools, then
    MKVToolNix where its installer puts it."""
    tools = tools_dir(environ)
    return [tools / "ffmpeg" / "bin", tools / "caddy", *map(Path, MKVTOOLNIX_DIRS)]


def search_path(current: str, folders: list[Path]) -> str:
    """PATH with `folders` first, each once. Pure, so it is tested anywhere."""
    first = [str(f) for f in folders]
    rest = [p for p in current.split(os.pathsep) if p and p not in first]
    return os.pathsep.join([*first, *rest])


ORIGINAL_PATH: dict[str, str] = {}  # PATH as this process got it, before prepare_process


def prepare_process(environ: os._Environ[str] | dict[str, str] | None = None) -> None:
    """Make this process find vpstream's programs: its own folders first on its own PATH
    (programs it starts inherit it), and never a program in the current folder, where
    Windows would otherwise look first."""
    env = os.environ if environ is None else environ
    ORIGINAL_PATH.setdefault("PATH", env.get("PATH", ""))
    env["PATH"] = search_path(env.get("PATH", ""), program_dirs(env))
    env["NoDefaultCurrentDirectoryInExePath"] = "1"


def on_windows() -> bool:
    """Whether this really is Windows (not a test that acts as if)."""
    return sys.platform == "win32"


WINDOWS_TEXT = """\
# vpstream configuration on Windows. The vpstream window writes the address and the port
# you choose there; close the window and open it again after a change by hand.

[network]
# Windows serves the local network only. Use it only on your own Wi-Fi or a local network
# you control, never on public Wi-Fi: anyone on the network can reach the stream port.
mode = "local"
# The address viewers use. Left out, it is this PC's address on the network, which must
# be a private one (192.168.x.x, 10.x.x.x, 172.16.x.x to 172.31.x.x). Setting it makes
# vpstream refuse to serve on any other network.
# address = "192.168.1.20"

[stream]
# The port viewers connect to, on the address above. The window picks one that is free,
# easy to type (1337, 1234, 2345, ...), and writes it here.
port = 1337

[prepare]
# How fast this PC prepares videos for browser mode, in times real time, for the time
# estimates.
# copy_speed = 11.0
# encode_speed = 1.6
"""
