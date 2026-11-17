"""`vpstream doctor` on Termux: the packages, the phone's storage, the configuration, the
dashboard password and the daemon. Nothing about systemd, ufw or Tailscale, which Termux
neither has nor needs."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vpstream import config
from vpstream.doctor import Check, Tool, check_config, check_python, check_tool
from vpstream.platform import termux
from vpstream.platform.termux import commands
from vpstream.tools import Runner

TOOLS = (
    Tool("ffmpeg", "preparing videos for the browser", "pkg install ffmpeg"),
    Tool("ffprobe", "reading the tracks of a video", "pkg install ffmpeg"),
    Tool("caddy", "serving streams", "pkg install caddy"),
    Tool("mkvmerge", "a full seek index in MKV files, so VLC resumes them quickly", "", False),
    Tool("mkvextract", "WebVTT subtitles stored by mkvmerge", "", False),
    Tool("termux-wake-lock", "keeping the phone awake while it streams (vpstream up)", "", False),
)


def check_storage(storage: Path) -> Check:
    problem = commands.storage_problem(storage)
    if problem is None:
        return Check("storage", "ok", f"{storage} can be read")
    return Check("storage", "problem", problem, "termux-setup-storage")


def check_password(paths: config.Paths, status: dict[str, Any] | None) -> Check:
    name = "dashboard password"
    problem = commands.password_problem(paths)
    if problem is None and (status or {}).get("dashboard", {}).get("password") is False:
        problem = "the daemon has none it can use"
    if problem:
        return Check(
            name, "warning", f"{problem}; the dashboard stays closed", termux.HINTS.dash_pass
        )
    return Check(name, "ok", f"set ({paths.dashboard_key}, only a verifier)")


def check_daemon(status: dict[str, Any] | None) -> Check:
    if status is None:
        return Check("daemon", "warning", "not running", termux.HINTS.start)
    return Check("daemon", "ok", f"vpstream {status.get('version')}, pid {status.get('pid')}")


def run_checks(
    runner: Runner,
    *,
    python_version: Sequence[int] | None = None,
    paths: config.Paths | None = None,
    status: dict[str, Any] | None = None,
    storage: Path = termux.STORAGE,
    **_: Any,  # what only the Linux checks take
) -> list[Check]:
    """Run every check. Nothing here changes anything."""
    paths = paths or config.default_paths()
    checks = [check_python(python_version or tuple(sys.version_info[:3]))]
    checks += [check_tool(runner, tool) for tool in TOOLS]
    checks.append(check_storage(storage))
    checks.append(check_config(paths.config_file))
    checks.append(check_password(paths, status))
    checks.append(check_daemon(status))
    return checks
