"""`vpstream doctor`: check this host for everything vpstream needs, and say what is missing.

Every check runs, so one report lists all the problems at once. What is checked depends on
the platform (`platform.linux.doctor`, `platform.termux.doctor`); the checks and the report
they share are here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vpstream import config
from vpstream.tools import Runner

Status = Literal["ok", "problem", "warning"]

MIN_PYTHON = (3, 12)


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


def check_python(version: Sequence[int]) -> Check:
    shown = ".".join(str(n) for n in version[:3])
    if tuple(version[:2]) >= MIN_PYTHON:
        return Check("python", "ok", shown)
    need = ".".join(str(n) for n in MIN_PYTHON)
    return Check("python", "problem", f"{shown}; vpstream needs {need} or newer")


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


def run_checks(runner: Runner, **options: Any) -> list[Check]:
    """Run every check of this platform. Nothing here changes the host or needs root."""
    from vpstream.platform.linux import doctor as linux

    return linux.run_checks(runner, **options)


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
