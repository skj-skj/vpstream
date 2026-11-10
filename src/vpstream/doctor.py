"""`vpstream doctor`: check this host for everything vpstream needs, and say what is missing.

Every check runs, so one report lists all the problems at once.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vpstream import config, tailscale
from vpstream.tools import PACKAGES, Runner

Status = Literal["ok", "problem", "warning"]

MIN_PYTHON = (3, 12)
SYSTEM_INSTALL = 'sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install "vpstream[dev]"'


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""
    fix: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    needed_for: str
    fix: str
    required: bool = True


TOOLS = (
    Tool("ffmpeg", "preparing videos for the browser", "vpstream install ffmpeg"),
    Tool("ffprobe", "reading the tracks of a video", "vpstream install ffmpeg"),
    Tool("caddy", "serving streams", "sudo apt install caddy"),
    Tool("ufw", "opening the stream port only while a stream runs", "sudo apt install ufw"),
    Tool("tailscale", "publishing the dashboard", "see https://tailscale.com/download/linux"),
    Tool("systemctl", "running the service", "vpstream needs a host that runs systemd"),
    Tool("sudo", "the firewall helper", "as root: apt install sudo"),
    Tool("mkvextract", "WebVTT subtitles stored by mkvmerge", "", required=False),
    Tool("mkvmerge", "WebVTT subtitles stored by mkvmerge", "", required=False),
)

# Burned-in subtitles fall back to these when a file does not carry its font.
FONTS = (("Noto Sans CJK", "fonts-noto-cjk"), ("Liberation Sans", "fonts-liberation"))


def check_python(version: Sequence[int]) -> Check:
    shown = ".".join(str(n) for n in version[:3])
    if tuple(version[:2]) >= MIN_PYTHON:
        return Check("python", "ok", shown)
    need = ".".join(str(n) for n in MIN_PYTHON)
    return Check("python", "problem", f"{shown}; vpstream needs {need} or newer")


def check_tool(runner: Runner, tool: Tool) -> Check:
    path = runner.which(tool.name)
    if path:
        return Check(tool.name, "ok", path)
    fix = tool.fix or f"sudo apt install {PACKAGES.get(tool.name, tool.name)}"
    status: Status = "problem" if tool.required else "warning"
    return Check(tool.name, status, f"not found; needed for {tool.needed_for}", fix)


def check_systemd(marker: Path) -> Check:
    """systemd is the running init system when it has made its runtime folder."""
    if marker.is_dir():
        return Check("systemd", "ok", "running")
    return Check("systemd", "problem", f"not running this host ({marker} is missing)")


def ufw_enabled(conf_text: str) -> bool:
    """Whether ufw.conf says the firewall starts on boot: a line `ENABLED=yes`."""
    for line in conf_text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "ENABLED":
            return value.strip().strip("\"'").lower() == "yes"
    return False


def check_ufw_enabled(conf: Path) -> Check:
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Check("ufw enabled", "warning", f"cannot read {conf}: {exc.strerror}")
    if ufw_enabled(text):
        return Check("ufw enabled", "ok", "yes")
    return Check(
        "ufw enabled",
        "problem",
        "ufw is not enabled, so no port is closed by it",
        "enable ufw yourself, after allowing your SSH access; vpstream never enables it",
    )


def check_tailscale(runner: Runner) -> Check:
    try:
        return Check("tailscale up", "ok", tailscale.ipv4(runner))
    except tailscale.TailscaleDown as exc:
        return Check("tailscale up", "problem", str(exc), "sudo tailscale up")


def check_tailnet_https(runner: Runner) -> Check:
    """The dashboard is published with `tailscale serve`, which needs a certificate."""
    try:
        node = tailscale.node(runner)
    except tailscale.TailscaleDown as exc:
        return Check("tailnet https", "problem", str(exc), "sudo tailscale up")
    if node.https:
        return Check("tailnet https", "ok", node.name)
    return Check(
        "tailnet https",
        "problem",
        "this tailnet issues no HTTPS certificate for this host; the dashboard needs one",
        "enable HTTPS certificates in the Tailscale admin console (DNS page)",
    )


def check_dashboard(runner: Runner, port: int) -> Check:
    try:
        proxy = tailscale.serve_proxy(tailscale.serve_config(runner), port)
    except tailscale.TailscaleDown as exc:
        return Check("dashboard", "problem", str(exc))
    if proxy == tailscale.local_target(port):
        return Check("dashboard", "ok", f"published by tailscale serve on port {port}")
    if proxy is None:
        return Check("dashboard", "warning", "not published on the tailnet", "sudo vpstream init")
    return Check(
        "dashboard",
        "problem",
        f"tailscale serve already uses port {port} for {proxy or 'another service'}",
        "set another port under [dashboard] in the configuration, then: sudo vpstream init",
    )


def check_fonts(runner: Runner) -> list[Check]:
    if not runner.which("fc-list"):
        return [
            Check("fonts", "warning", "fc-list not found", "sudo apt install fontconfig"),
        ]
    families = runner.run(["fc-list", ":", "family"], timeout=30).stdout.lower()
    checks = []
    for family, package in FONTS:
        if family.lower() in families:
            checks.append(Check(f"font {family}", "ok", "installed"))
        else:
            checks.append(
                Check(
                    f"font {family}",
                    "warning",
                    "not installed; burned-in subtitles may show boxes or another font",
                    f"sudo apt install {package}",
                )
            )
    return checks


def check_config(path: Path) -> Check:
    if not path.exists():
        return Check("config", "ok", f"no {path}; the defaults are used")
    try:
        cfg = config.load(path)
    except config.ConfigError as exc:
        return Check("config", "problem", str(exc), f"edit {path}")
    return Check(
        "config", "ok", f"{path} (stream port {cfg.stream_port}, dashboard {cfg.dashboard_port})"
    )


def check_service(runner: Runner, unit_file: Path) -> Check:
    if not unit_file.exists():
        return Check("service", "warning", "not set up on this host", "sudo vpstream init")
    res = runner.run(["systemctl", "is-active", unit_file.name], timeout=30)
    state = res.stdout.strip() or "unknown"
    if res.returncode == 0:
        return Check("service", "ok", state)
    return Check("service", "problem", state, "see: journalctl -u vpstream -n 50 --no-pager")


def folder_problem(folder: Path, st: os.stat_result, owner_uid: int = 0) -> str | None:
    """Why one folder on the way to the package is not safe for the service, or None."""
    if not st.st_mode & stat.S_IXOTH:
        return f"other users cannot enter {folder}"
    if st.st_uid != owner_uid:
        return f"{folder} is not owned by root"
    if st.st_mode & stat.S_ISVTX:
        return None  # sticky, like /tmp: others cannot replace what root put there
    if st.st_mode & stat.S_IWOTH:
        return f"{folder} is writable by every user"
    if st.st_mode & stat.S_IWGRP and st.st_gid != 0:
        return f"{folder} is writable by a group other than root"
    return None


def install_problem(package_dir: Path, owner_uid: int = 0) -> str | None:
    """Why the service cannot safely run the package from `package_dir`, or None.

    The service runs as its own user and its firewall helper runs as root, so every folder
    down to the package must be open to other users, owned by root and writable by nobody else.
    """
    try:
        package_dir = package_dir.resolve()
        for folder in (package_dir, *package_dir.parents):
            if problem := folder_problem(folder, folder.stat(), owner_uid):
                return problem
        if not package_dir.stat().st_mode & stat.S_IROTH:
            return f"other users cannot read {package_dir}"
    except OSError as exc:
        return f"cannot check {package_dir}: {exc.strerror}"
    return None


def check_install(package_dir: Path, owner_uid: int = 0) -> Check:
    problem = install_problem(package_dir, owner_uid)
    if problem is None:
        return Check("install", "ok", str(package_dir))
    return Check(
        "install",
        "problem",
        f"{problem}; the service cannot run from a per-user install",
        f"{SYSTEM_INSTALL}  (then remove the per-user copy: pipx uninstall vpstream)",
    )


def run_checks(
    runner: Runner,
    *,
    python_version: Sequence[int] | None = None,
    package_dir: Path | None = None,
    paths: config.Paths | None = None,
    systemd_marker: Path = Path("/run/systemd/system"),
    ufw_conf: Path = Path("/etc/ufw/ufw.conf"),
    unit_file: Path = Path("/etc/systemd/system/vpstream.service"),
) -> list[Check]:
    """Run every check. Nothing here changes the host or needs root."""
    paths = paths or config.Paths()
    checks = [check_python(python_version or tuple(sys.version_info[:3]))]
    checks += [check_tool(runner, tool) for tool in TOOLS]
    found = {c.name for c in checks if c.status == "ok"}
    if "systemctl" in found:
        checks.append(check_systemd(systemd_marker))
    if "ufw" in found:
        checks.append(check_ufw_enabled(ufw_conf))
    if "tailscale" in found:
        checks.append(check_tailscale(runner))
        checks.append(check_tailnet_https(runner))
    checks += check_fonts(runner)
    checks.append(check_install(package_dir or Path(__file__).parent))
    checks.append(check_config(paths.config_file))
    if "systemctl" in found:
        checks.append(check_service(runner, unit_file))
    if "tailscale" in found and unit_file.exists():
        try:
            port = config.load(paths.config_file).dashboard_port
        except config.ConfigError:
            port = None
        if port is not None:
            checks.append(check_dashboard(runner, port))
    return checks


_LABELS = {"ok": "ok", "problem": "PROBLEM", "warning": "warning"}


def format_report(checks: Sequence[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = []
    for c in checks:
        lines.append(f"{_LABELS[c.status]:<8} {c.name:<{width}}  {c.detail}".rstrip())
        if c.fix and c.status != "ok":
            lines.append(f"{'':<8} {'':<{width}}  fix: {c.fix}")
    problems = sum(c.status == "problem" for c in checks)
    warnings = sum(c.status == "warning" for c in checks)
    summary = (
        "Everything vpstream needs is here." if not problems else f"{_count(problems, 'problem')}."
    )
    if warnings:
        summary += f" {_count(warnings, 'warning')}."
    return "\n".join([*lines, "", summary])


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def exit_code(checks: Sequence[Check]) -> int:
    return 1 if any(c.status == "problem" for c in checks) else 0
