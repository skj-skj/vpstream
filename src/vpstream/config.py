"""The configuration file, and the fixed places vpstream uses on a host."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vpstream.checks import is_number, is_whole

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

    @property
    def gate_socket(self) -> Path:
        """Where Caddy asks about every request to the stream port."""
        return self.run_dir / "gate.sock"

    @property
    def caddy_dir(self) -> Path:
        """Caddy's configuration, its own files and its logs."""
        return self.state_dir / "caddy"

    @property
    def videos_dir(self) -> Path:
        """One link per video of the running stream, named by its number."""
        return self.caddy_dir / "videos"

    @property
    def access_log(self) -> Path:
        return self.caddy_dir / "access.log"

    @property
    def open_ports_file(self) -> Path:
        """The firewall helper's record of what it opened. Next to the configuration, in a
        folder only root can write: the helper trusts it as much as the configuration."""
        return self.config_file.parent / "open-ports"

    @classmethod
    def under(cls, root: Path) -> Paths:
        default = cls()
        return cls(
            config_file=root / default.config_file.relative_to("/"),
            state_dir=root / default.state_dir.relative_to("/"),
            cache_dir=root / default.cache_dir.relative_to("/"),
            run_dir=root / default.run_dir.relative_to("/"),
        )


ROOT_ENV = "VPSTREAM_ROOT"


def default_paths() -> Paths:
    """The real paths, or the same layout under $VPSTREAM_ROOT for trying things out."""
    root = os.environ.get(ROOT_ENV)
    return Paths.under(Path(root)) if root else Paths()


@dataclass(frozen=True)
class Config:
    stream_port: int = 1337
    dashboard_port: int = 1338
    stream_host: str = ""  # the address in the URLs shown; "" means: find it
    # How fast this host prepares, in times real time (see prepare.Speeds): a copy, and an
    # encode of 1080p at 24 fps. `vpstream bench` measures them.
    copy_speed: float = 11.0
    encode_speed: float = 1.6
    media_folder: str = "/srv/media"  # what `vpstream ls` lists when it is given no folder


# Section and key in the file -> field of Config.
_KEYS = {
    ("stream", "port"): "stream_port",
    ("stream", "host"): "stream_host",
    ("dashboard", "port"): "dashboard_port",
    ("prepare", "copy_speed"): "copy_speed",
    ("prepare", "encode_speed"): "encode_speed",
    ("media", "folder"): "media_folder",
}

DEFAULT_TEXT = """\
# vpstream configuration. Restart the service after a change:
#   sudo systemctl restart vpstream

[stream]
# The public port viewers connect to. It is open in the firewall only while a stream runs.
port = 1337
# The address or name in the URLs vpstream shows. Left out, it is the address this host
# uses to reach the internet.
# host = "203.0.113.5"

[dashboard]
# The dashboard port. It listens on this host only (127.0.0.1); `vpstream init` publishes it
# on your tailnet with `tailscale serve`, as https://<this host's tailnet name>:<port>/.
port = 1338

[prepare]
# How fast this host prepares videos for browser mode, in times real time, for the time
# estimates: a copy, and an encode of 1080p at 24 fps. `vpstream bench FILE` measures them.
# copy_speed = 11.0
# encode_speed = 1.6

[media]
# The folder `vpstream ls` lists when it is given none.
# folder = "/srv/media"
"""


def _port_problem(name: str, value: Any) -> str | None:
    # bool is an int in Python, and `port = true` is a mistake, not port 1.
    if not is_whole(value):
        return f"{name} must be a whole number, not {value!r}"
    if not MIN_PORT <= value <= 65535:
        return f"{name} must be from {MIN_PORT} to 65535, not {value}"
    return None


def _host_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.:-]*", value):
        return f"{name} must be an address or a host name, not {value!r}"
    return None


def _speed_problem(name: str, value: Any) -> str | None:
    if not is_number(value) or not 0.01 <= value <= 1000:
        return f"{name} must be a number from 0.01 to 1000 (times real time), not {value!r}"
    return None


def _folder_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        return f"{name} must be a full path, starting with /, not {value!r}"
    return None


_CHECKS = {
    "stream_host": _host_problem,
    "media_folder": _folder_problem,
    "copy_speed": _speed_problem,
    "encode_speed": _speed_problem,
}


def parse(text: str) -> Config:
    """Read configuration text. Missing keys take their defaults; unknown keys are errors."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"not valid TOML: {exc}") from exc
    problems: list[str] = []
    values: dict[str, Any] = {}
    for section, body in data.items():
        if not isinstance(body, dict):
            problems.append(f"unknown setting {section!r}")
            continue
        for key, value in body.items():
            field = _KEYS.get((section, key))
            name = f"{section}.{key}"
            if field is None:
                problems.append(f"unknown setting {name!r}")
            elif problem := _CHECKS.get(field, _port_problem)(name, value):
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
