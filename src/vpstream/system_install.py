"""What `vpstream install` shares between the programs it installs: running a step as root
(directly, or through sudo), apt without questions, and fetching over HTTPS."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from vpstream import __version__
from vpstream.tools import Runner, tail

COMMAND_TIMEOUT = 900  # apt-get on a slow mirror
# No questions: commands run without a terminal on stdin.
APT_GET = ("env", "DEBIAN_FRONTEND=noninteractive", "apt-get")


class SetupError(RuntimeError):
    pass


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


class Http(Protocol):
    def get_json(self, url: str) -> Any: ...

    def get_text(self, url: str) -> str: ...

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        """Save `url` to `dest` and return its sha256."""
        ...


class UrlHttp:
    timeout = 60

    def _open(self, url: str, accept: str = "*/*") -> Any:
        headers = {"User-Agent": f"vpstream/{__version__}", "Accept": accept}
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=self.timeout
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                msg = "GitHub's API limit for this IP is used up; try again in an hour"
                raise SetupError(msg) from exc
            raise SetupError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SetupError(f"cannot download {url}: {reason}") from exc

    def get_json(self, url: str) -> Any:
        with self._open(url, "application/vnd.github+json") as resp:
            return json.load(resp)

    def get_text(self, url: str) -> str:
        with self._open(url) as resp:
            return str(resp.read().decode("utf-8", errors="replace"))

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        digest = hashlib.sha256()
        with self._open(url) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, total)
        return digest.hexdigest()
