"""`vpstream doctor` on Windows: Python and tkinter, the programs, the configuration.

Nothing about systemd, ufw, Tailscale or a daemon: on Windows the window runs the stream.
"""

from __future__ import annotations

import importlib.util
import ntpath
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from vpstream import config
from vpstream.doctor import Check, Tool, check_python, check_tool
from vpstream.platform import windows
from vpstream.tools import Runner

TOOLS = (
    Tool("ffmpeg", "preparing videos for the browser", ""),
    Tool("ffprobe", "reading the tracks of a video", ""),
    Tool("caddy", "serving streams", ""),
    Tool("mkvmerge", "a full seek index in MKV files, so VLC resumes them quickly", "", False),
    Tool("mkvextract", "WebVTT subtitles stored by mkvmerge", "", False),
)
TKINTER_FIX = (
    'run the Python installer from python.org again, choose Modify, and tick "tcl/tk and IDLE"'
)


def check_tkinter(find: Callable[[str], Any] = importlib.util.find_spec) -> Check:
    if find("tkinter") is not None:
        return Check("tkinter", "ok", "the window can open")
    return Check("tkinter", "problem", "missing; the vpstream window needs it", TKINTER_FIX)


def path_hint(found: str | None, original_path: str) -> str:
    """For a program found in MKVToolNix's own folder, which its installer does not put on
    PATH: vpstream finds it there, but a terminal does not."""
    if found is None:
        return ""
    folder = ntpath.dirname(found)
    if folder.lower() not in (d.lower() for d in windows.MKVTOOLNIX_DIRS):
        return ""
    if any(p.lower().rstrip("\\") == folder.lower() for p in original_path.split(";") if p):
        return ""
    return f"; vpstream finds it there, but a terminal does not: add {folder} to PATH to use it"


def check_tools(runner: Runner, original_path: str) -> list[Check]:
    checks = []
    for tool in TOOLS:
        check = check_tool(runner, tool)
        if check.status == "ok" and tool.name.startswith("mkv"):
            check = Check(check.name, "ok", check.detail + path_hint(check.detail, original_path))
        checks.append(check)
    return checks


def check_config(path: Path) -> Check:
    if not path.exists():
        return Check("config", "ok", f"no {path} yet; the window writes it")
    try:
        cfg = config.load(path)
    except config.ConfigError as exc:
        return Check("config", "problem", str(exc), f"edit {path}")
    address = cfg.local_address or "found by itself"
    return Check("config", "ok", f"{path} (stream port {cfg.stream_port}, address {address})")


def run_checks(
    runner: Runner,
    *,
    python_version: Sequence[int] | None = None,
    paths: config.Paths | None = None,
    original_path: str | None = None,
    find: Callable[[str], Any] = importlib.util.find_spec,
    **_: Any,  # what only the Linux checks take
) -> list[Check]:
    """Run every check. Nothing here changes anything."""
    paths = paths or config.default_paths()
    if original_path is None:
        original_path = windows.ORIGINAL_PATH.get("PATH", os.environ.get("PATH", ""))
    checks = [check_python(python_version or tuple(sys.version_info[:3])), check_tkinter(find)]
    checks += check_tools(runner, original_path)
    checks.append(check_config(paths.config_file))
    return checks
