"""What every platform shares: the firewall as the daemon sees it, the shape of what each
platform tells the person to run (`Hints`), and fetching over HTTPS for the programs
vpstream installs (`UrlHttp`: Linux's `vpstream install`, Windows' `windows-install`).

On Linux a root helper opens and closes the ports (`platform.linux.firewall`); elsewhere
there is none, and the daemon is given no firewall at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vpstream import __version__

# GitHub allows 60 API calls an hour from one address without a token, which hosts behind a
# shared address (CI runners) use up. A token in this variable is sent to the API only.
TOKEN_ENV = "GITHUB_TOKEN"
API_HOST = "api.github.com"


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
    own_installs: tuple[str, ...] = ()  # programs vpstream's own install command puts in
    own_install: str = "vpstream install"  # that command

    def install(self, program: str) -> str:
        """How to install a program vpstream needs."""
        if program in self.own_installs or self.packages.get(program) in self.own_installs:
            return f"{self.own_install} {self.packages.get(program, program)}"
        return f"{self.package_manager} {self.packages.get(program, program)}"


class SetupError(RuntimeError):
    """Installing a program failed; the message says why."""


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
        api = urllib.parse.urlsplit(url).hostname == API_HOST
        token = os.environ.get(TOKEN_ENV) if api else None
        # A stale or narrow token makes GitHub refuse (401) even what it gives anyone, so it
        # is asked once more without one.
        for attempt in (token, None) if token else (None,):
            request = urllib.request.Request(url, headers=headers)
            if attempt:
                # Unredirected: a redirect to another host never carries it.
                request.add_unredirected_header("Authorization", f"Bearer {attempt}")
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                if attempt and exc.code == 401:
                    continue
                if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                    msg = "GitHub's API limit for this IP is used up; try again in an hour"
                    raise SetupError(msg) from exc
                raise SetupError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise SetupError(f"cannot download {url}: {reason}") from exc
        raise AssertionError("unreachable: the last attempt has no token")

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
