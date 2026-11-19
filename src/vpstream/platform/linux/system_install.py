"""What `vpstream install` shares between the programs it installs: running a step as root
(directly, or through sudo) and apt without questions. Fetching over HTTPS is shared with
Windows (`platform.common.UrlHttp`)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Shared with Windows; still named from here by the Linux installers and their tests.
from vpstream.platform.common import API_HOST as API_HOST
from vpstream.platform.common import TOKEN_ENV as TOKEN_ENV
from vpstream.platform.common import Http as Http
from vpstream.platform.common import SetupError as SetupError
from vpstream.platform.common import UrlHttp as UrlHttp
from vpstream.tools import Runner, tail

if sys.platform == "win32":
    raise ImportError("vpstream.platform.linux is for Linux; Windows has platform.windows")

COMMAND_TIMEOUT = 900  # apt-get on a slow mirror
# No questions: commands run without a terminal on stdin.
APT_GET = ("env", "DEBIAN_FRONTEND=noninteractive", "apt-get")


class Cancelled(Exception):
    pass


def sudo_prefix(runner: Runner, is_root: bool | None = None) -> list[str]:
    """What goes before a command that needs root: nothing as root, else sudo."""
    root = os.geteuid() == 0 if is_root is None else is_root
    if root:
        return []
    if not runner.which("sudo"):
        raise SetupError("installing needs root: run it as root, or install sudo")
    return ["sudo"]


def as_root(runner: Runner, is_root: bool | None, *args: str | Path, what: str) -> str:
    """Run one step as root; returns what it printed. Raises SetupError when it fails."""
    cmd = [*sudo_prefix(runner, is_root), *map(str, args)]
    res = runner.run(cmd, timeout=COMMAND_TIMEOUT)
    if res.returncode != 0:
        raise SetupError(f"{what} failed ({' '.join(cmd)}): {tail(res, stdout=True)}")
    return res.stdout


@dataclass
class Installer:
    """What installing one program needs: programs to run, files to fetch, a question and a
    progress line for the user, a folder to download to, and whether this runs as root
    (None: look)."""

    runner: Runner
    http: Http
    confirm: Callable[[str], bool]
    progress: Callable[[str], None]
    tmp_dir: str | None = None
    is_root: bool | None = None

    def _root(self, *args: str | Path, what: str) -> str:
        """Run one step as root (see `as_root`)."""
        return as_root(self.runner, self.is_root, *args, what=what)
