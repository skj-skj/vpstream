"""The configuration file, and the fixed places vpstream uses on a host."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_PORT = 1024  # the daemon runs as an unprivileged user, which cannot bind lower ports


class ConfigError(ValueError):
    """The configuration file cannot be used; the message lists every problem."""


@dataclass(frozen=True)
class Paths:
    """Where things live. Tests move everything under a temporary folder with `under`."""

    config_file: Path = Path("/etc/vpstream/config.toml")
    state_dir: Path = Path("/var/lib/vpstream")
    cache_dir: Path = Path("/var/cache/vpstream")
    run_dir: Path = Path("/run/vpstream")

    @property
    def socket(self) -> Path:
        return self.run_dir / "daemon.sock"

    @classmethod
    def under(cls, root: Path) -> Paths:
        default = cls()
        return cls(
            config_file=root / default.config_file.relative_to("/"),
            state_dir=root / default.state_dir.relative_to("/"),
            cache_dir=root / default.cache_dir.relative_to("/"),
            run_dir=root / default.run_dir.relative_to("/"),
        )


@dataclass(frozen=True)
class Config:
    stream_port: int = 1337
    dashboard_port: int = 1338


# Section and key in the file -> field of Config.
_KEYS = {("stream", "port"): "stream_port", ("dashboard", "port"): "dashboard_port"}

DEFAULT_TEXT = """\
# vpstream configuration. Restart the service after a change:
#   sudo systemctl restart vpstream

[stream]
# The public port viewers connect to. It is open in the firewall only while a stream runs.
port = 1337

[dashboard]
# The dashboard port. It listens on the Tailscale address only.
port = 1338
"""


def _port_problem(name: str, value: Any) -> str | None:
    # bool is an int in Python, and `port = true` is a mistake, not port 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{name} must be a whole number, not {value!r}"
    if not MIN_PORT <= value <= 65535:
        return f"{name} must be from {MIN_PORT} to 65535, not {value}"
    return None


def parse(text: str) -> Config:
    """Read configuration text. Missing keys take their defaults; unknown keys are errors."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from exc
    problems: list[str] = []
    values: dict[str, int] = {}
    for section, body in data.items():
        if not isinstance(body, dict):
            problems.append(f"unknown setting {section!r}")
            continue
        for key, value in body.items():
            field = _KEYS.get((section, key))
            name = f"{section}.{key}"
            if field is None:
                problems.append(f"unknown setting {name!r}")
            elif problem := _port_problem(name, value):
                problems.append(problem)
            else:
                values[field] = value
    config = Config(**values)
    if not problems and config.stream_port == config.dashboard_port:
        problems.append(f"stream.port and dashboard.port are both {config.stream_port}")
    if problems:
        raise ConfigError("; ".join(problems))
    return config


def load(path: Path) -> Config:
    """Read the configuration file. A missing file means the defaults."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse(text)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
