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

    @property
    def open_dashboard_ports_file(self) -> Path:
        """The same record for the dashboard port, which is open only in public mode."""
        return self.config_file.parent / "open-dashboard-ports"

    @property
    def dashboard_key(self) -> Path:
        """The public dashboard's password verifier: root's, readable by the service's group
        only, because it is enough to log in (see `login`)."""
        return self.config_file.parent / "dashboard.key"

    @property
    def dashboard_socket(self) -> Path:
        """Where the public dashboard's Caddy hands on every request."""
        return self.run_dir / "dashboard.sock"

    @property
    def dashboard_caddy_dir(self) -> Path:
        """The public dashboard's Caddy: its configuration, its certificate authority, its log."""
        return self.state_dir / "caddy-dashboard"

    @property
    def device_key(self) -> Path:
        """What the public dashboard signs its device cookies with. A new password makes a
        new one, so every browser must log in again."""
        return self.state_dir / "dashboard-device.key"

    @property
    def login_record(self) -> Path:
        """Failed logins to the public dashboard, kept across restarts of the daemon."""
        return self.state_dir / "dashboard-logins.json"

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
    # False: the dashboard is on the tailnet only, through `tailscale serve`. True: it is on
    # the internet, over HTTPS through Caddy, behind a password.
    dashboard_public: bool = False
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
    ("dashboard", "public"): "dashboard_public",
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
# The dashboard port. By default it listens on this host only (127.0.0.1), and `vpstream init`
# publishes it on your tailnet with `tailscale serve`, as https://<tailnet name>:<port>/.
port = 1338
# true: the dashboard is on the internet instead, over HTTPS (Caddy, its own certificate)
# with a password. Switch with `sudo vpstream init --no-tailscale` and back with
# `sudo vpstream init --tailscale`, which also set the password and the firewall.
public = false

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


def _bool_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, bool):
        return f"{name} must be true or false, not {value!r}"
    return None


def _folder_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        return f"{name} must be a full path, starting with /, not {value!r}"
    return None


_CHECKS = {
    "stream_host": _host_problem,
    "dashboard_public": _bool_problem,
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


_SECTION = re.compile(r"\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?")
_PUBLIC = re.compile(r"\s*public\s*=")


def with_public_dashboard(text: str, public: bool) -> str:
    """The configuration text with `[dashboard] public` set to `public`. Every other line
    stays as it was written. Raises ConfigError when the result would not say so, for
    example because the setting is written in a way this does not look for."""
    value = f"public = {'true' if public else 'false'}\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    section, header = "", None
    for number, line in enumerate(lines):
        if found := _SECTION.fullmatch(line.rstrip("\n")):
            section = found.group(1)
            header = number if section == "dashboard" and header is None else header
        elif section == "dashboard" and _PUBLIC.match(line):
            lines[number] = value
            break
    else:
        if header is None:
            lines += ["\n" if lines else "", "[dashboard]\n", value]
        else:
            lines.insert(header + 1, value)
    result = "".join(lines)
    try:
        written = parse(result).dashboard_public
    except ConfigError as exc:
        raise ConfigError(f"cannot set [dashboard] public: {exc}") from exc
    if written is not public:
        raise ConfigError("cannot set [dashboard] public; set it by hand")
    return result
