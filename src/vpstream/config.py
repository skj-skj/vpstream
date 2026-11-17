"""The configuration file, and the fixed places vpstream uses on a host."""

from __future__ import annotations

import dataclasses
import itertools
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vpstream import local, platform
from vpstream.checks import is_number, is_whole

MIN_PORT = 1024  # the daemon runs as an unprivileged user, which cannot bind lower ports
HTTP_PORT = 80  # what Let's Encrypt checks; the service may bind it when HTTPS is on
LETS_ENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
LETS_ENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


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
    def open_acme_ports_file(self) -> Path:
        """The same record for the port Let's Encrypt checks, open only while a certificate
        for the stream is requested."""
        return self.config_file.parent / "open-acme-ports"

    @property
    def certificate_dir(self) -> Path:
        """The stream's HTTPS certificate: the Caddy that requests it, and where it keeps
        it. Under the stream's Caddy folder, kept across upgrades and `init`; only
        `uninstall` removes it."""
        return self.caddy_dir / "certificate"

    @property
    def dashboard_key(self) -> Path:
        """The public dashboard's password verifier: root's, readable by the service's group
        only, because it is enough to log in (see `login`)."""
        return self.config_file.parent / "dashboard.key"

    @property
    def dashboard_ca_cert(self) -> Path:
        """The public dashboard's certificate authority, which the owner's phone trusts:
        made by `init`, valid only for this host (see `dashboard_ca`)."""
        return self.config_file.parent / "dashboard-ca.crt"

    @property
    def dashboard_ca_key(self) -> Path:
        """Its key: root's, readable by the service's group, whose Caddy signs with it."""
        return self.config_file.parent / "dashboard-ca.key"

    @property
    def dashboard_ca_hosts(self) -> Path:
        """The hosts the authority permits."""
        return self.config_file.parent / "dashboard-ca.json"

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
    """The real paths, or the same layout under $VPSTREAM_ROOT for trying things out. On
    Termux they are the person's own (see `platform.termux.paths`)."""
    root = os.environ.get(ROOT_ENV)
    if root:
        return Paths.under(Path(root))
    if platform.is_termux():
        from vpstream.platform import termux

        return Paths(*termux.paths())
    return Paths()


@dataclass(frozen=True)
class Config:
    stream_port: int = 1337
    dashboard_port: int = 1338
    # False: the dashboard is on the tailnet only, through `tailscale serve`. True: it is on
    # the internet, over HTTPS through Caddy, behind a password.
    dashboard_public: bool = False
    stream_host: str = ""  # the address in the URLs shown; "" means: find it
    # True: streams are served over HTTPS, with a Let's Encrypt certificate for the address.
    stream_https: bool = False
    # Where the certificate comes from, and the port the authority checks (Let's Encrypt
    # checks port 80 only). Another authority, with its own root, is for trying things out.
    acme_ca: str = LETS_ENCRYPT
    acme_ca_root: str = ""
    acme_port: int = 80
    # How fast this host prepares, in times real time (see prepare.Speeds): a copy, and an
    # encode of 1080p at 24 fps. `vpstream bench` measures them.
    copy_speed: float = 11.0
    encode_speed: float = 1.6
    media_folder: str = "/srv/media"  # what `vpstream ls` lists when it is given no folder
    # True: the stream and the dashboard are on the local network only, at a private address
    # (see `local`). Always on Termux, from `[network] mode`; on Linux from `init --local`,
    # which runs `vpstream daemon --local`, never from this file.
    local: bool = False
    local_address: str = ""  # with `local`: the address to serve on; "" means: find it
    # How much the stream may take of the host, 0 for no bound (Linux; see TERMUX_LIMITS):
    # requests Caddy asks the gate at once, Caddy's processors and memory (GOMAXPROCS,
    # GOMEMLIMIT), and the threads of one ffmpeg that prepares.
    gate_connections: int = 0
    caddy_cpus: int = 0
    caddy_memory_mb: int = 0
    prepare_threads: int = 0


# Section and key in the file -> field of Config.
_KEYS = {
    ("stream", "port"): "stream_port",
    ("stream", "host"): "stream_host",
    ("stream", "https"): "stream_https",
    ("acme", "ca"): "acme_ca",
    ("acme", "ca_root"): "acme_ca_root",
    ("acme", "port"): "acme_port",
    ("dashboard", "port"): "dashboard_port",
    ("dashboard", "public"): "dashboard_public",
    ("prepare", "copy_speed"): "copy_speed",
    ("prepare", "encode_speed"): "encode_speed",
    ("media", "folder"): "media_folder",
}
# Only where local mode is set in the file (Termux).
_LOCAL_KEYS = {
    ("network", "mode"): "local",
    ("network", "address"): "local_address",
    ("limits", "gate_connections"): "gate_connections",
    ("limits", "caddy_cpus"): "caddy_cpus",
    ("limits", "caddy_memory_mb"): "caddy_memory_mb",
    ("limits", "prepare_threads"): "prepare_threads",
}
# Termux's defaults: a phone serves one or two devices, and must not be killed for what it
# takes. Measured with scripts/load-probe.py (see the README, Termux).
TERMUX_LIMITS = {
    "gate_connections": 8,
    "caddy_cpus": 2,
    "caddy_memory_mb": 192,
    "prepare_threads": 4,
}
# What Termux cannot do: it has no public address, no firewall and no port 80.
_NOT_LOCAL = {
    "dashboard_public": "the dashboard is on the local network only on Termux",
    "stream_https": "streams are served over plain HTTP on the local network on Termux",
}
TERMUX_MEDIA = "/storage/emulated/0/Movies"

DEFAULT_TEXT = """\
# vpstream configuration. Restart the service after a change:
#   sudo systemctl restart vpstream

[stream]
# The public port viewers connect to. It is open in the firewall only while a stream runs.
port = 1337
# The address or name in the URLs vpstream shows. Left out, it is the address this host
# uses to reach the internet.
# host = "203.0.113.5"
# true: streams are served over HTTPS (https://<ip>:<port>/...) with a Let's Encrypt
# certificate for the address, which needs Caddy 2.10 or newer and port 80 reachable while it
# is requested. Switch with `sudo vpstream init --https` and back with `--no-https`.
https = false

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

# [acme]
# Where the stream's HTTPS certificate comes from. Let's Encrypt by default; its staging
# server, or a test authority such as Pebble with its root, is for trying things out.
# ca = "https://acme-staging-v02.api.letsencrypt.org/directory"
# ca_root = "/path/to/test-root.pem"
# The port the authority checks. Let's Encrypt checks port 80 only.
# port = 80
"""


TERMUX_TEXT = """\
# vpstream configuration on Termux. Start the daemon again after a change:
#   vpstream down && vpstream up

[network]
# Termux serves the local network only. Use it only on your own Wi-Fi or a local network
# you control, never on public Wi-Fi: anyone on the network can reach the stream and the
# dashboard ports.
mode = "local"
# The address viewers use. Left out, it is this phone's address on the network, which
# must be a private one (192.168.x.x, 10.x.x.x, 172.16.x.x to 172.31.x.x). Setting it
# makes vpstream refuse to serve on any other network.
# address = "192.168.1.20"

[stream]
# The port viewers connect to, on the address above.
port = 1337

[dashboard]
# The dashboard port: on this phone (http://127.0.0.1:1338/) and on the address above,
# both behind the password set with `vpstream dash-pass`.
port = 1338

[prepare]
# How fast this phone prepares videos for browser mode, in times real time, for the time
# estimates. `vpstream bench FILE` measures them.
# copy_speed = 11.0
# encode_speed = 1.6

[media]
# The folder `vpstream ls` lists when it is given none.
# folder = "/storage/emulated/0/Movies"

[limits]
# What a stream may take of the phone; 0 for no bound. Requests Caddy hands the daemon at
# once (more wait their turn instead of failing), Caddy's processors and memory, and the
# threads of the one ffmpeg that prepares a video.
# gate_connections = 8
# caddy_cpus = 2
# caddy_memory_mb = 192
# prepare_threads = 4
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


def _limit_problem(name: str, value: Any) -> str | None:
    if not is_whole(value) or not 0 <= value <= 4096:
        return f"{name} must be a whole number from 0 (no bound) to 4096, not {value!r}"
    return None


def _local_address_problem(name: str, value: Any) -> str | None:
    """A private address of the local network, or "" to find one (see `local`)."""
    if value == "":
        return None
    if problem := _host_problem(name, value):
        return problem
    if problem := local.private_problem(value):
        return f"{name}: {problem}"
    return None


def _mode_problem(name: str, value: Any) -> str | None:
    if value != "local":
        return f'{name} must be "local" (Termux serves the local network only), not {value!r}'
    return None


def _bool_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, bool):
        return f"{name} must be true or false, not {value!r}"
    return None


def _folder_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        return f"{name} must be a full path, starting with /, not {value!r}"
    return None


def _ca_problem(name: str, value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"https://[A-Za-z0-9.:/_~%-]+", value):
        return f"{name} must be an https:// address of an ACME directory, not {value!r}"
    return None


def _root_problem(name: str, value: Any) -> str | None:
    return None if value == "" else _folder_problem(name, value)


def usable_port(value: Any, http: bool = False) -> bool:
    """Whether vpstream may use this port: one an unprivileged service can bind, or with
    `http` also port 80, which Let's Encrypt checks."""
    return is_whole(value) and (MIN_PORT <= value <= 65535 or (http and value == HTTP_PORT))


def _acme_port_problem(name: str, value: Any) -> str | None:
    if usable_port(value, http=True):
        return None
    return _port_problem(name, value) or f"{name} must be 80, or from {MIN_PORT} to 65535"


_CHECKS = {
    "stream_host": _host_problem,
    "stream_https": _bool_problem,
    "acme_ca": _ca_problem,
    "acme_ca_root": _root_problem,
    "acme_port": _acme_port_problem,
    "dashboard_public": _bool_problem,
    "media_folder": _folder_problem,
    "copy_speed": _speed_problem,
    "encode_speed": _speed_problem,
    "local": _mode_problem,
    "local_address": _local_address_problem,
    "gate_connections": _limit_problem,
    "caddy_cpus": _limit_problem,
    "caddy_memory_mb": _limit_problem,
    "prepare_threads": _limit_problem,
}


def parse(text: str, local_keys: bool | None = None) -> Config:
    """Read configuration text. Missing keys take their defaults; unknown keys are errors.

    `[network]`, local mode, is read only with `local_keys` (by default: on Termux, where it
    is always on). Elsewhere it is an unknown setting: Linux sets it with `init --local`.
    """
    termux = platform.is_termux() if local_keys is None else local_keys
    keys = {**_KEYS, **_LOCAL_KEYS} if termux else _KEYS
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
            field = keys.get((section, key))
            name = f"{section}.{key}"
            if field is None:
                problems.append(f"unknown setting {name!r}")
            elif problem := _CHECKS.get(field, _port_problem)(name, value):
                problems.append(problem)
            elif termux and value is True and field in _NOT_LOCAL:
                problems.append(f"{name} cannot be true: {_NOT_LOCAL[field]}")
            else:
                values[field] = value
    if termux:
        values["local"] = True  # `mode` says so, or is left out
        values.setdefault("media_folder", TERMUX_MEDIA)
        values = {**TERMUX_LIMITS, **values}
    config = Config(**values)
    if not problems:
        ports = {
            "stream.port": config.stream_port,
            "dashboard.port": config.dashboard_port,
            "acme.port": config.acme_port,
        }
        for one, other in itertools.combinations(ports, 2):
            if ports[one] == ports[other]:
                problems.append(f"{one} and {other} are both {ports[one]}")
    if problems:
        raise ConfigError("; ".join(problems))
    return config


def with_local(config: Config, on: bool, address: str = "") -> Config:
    """The configuration in local mode, as `vpstream daemon --local` asks on Linux (Termux
    reads it from the file). Raises ConfigError for what does not go with it."""
    if not on:
        if address:
            raise ConfigError("--address goes with --local")
        return config
    if problem := _local_address_problem("--address", address):
        raise ConfigError(problem)
    if config.dashboard_public or config.stream_https:
        raise ConfigError(
            "local mode serves the local network over plain HTTP: it cannot go with "
            "[dashboard] public or [stream] https"
        )
    return dataclasses.replace(config, local=True, local_address=address or config.local_address)


def load(path: Path, local_keys: bool | None = None) -> Config:
    """Read the configuration file. A missing file means the defaults (see `parse` for
    `local_keys`)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse(text, local_keys)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


_SECTION = re.compile(r"\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(#.*)?")
# The switches `init` sets: (section, key) -> field of Config.
SWITCHES = {
    ("dashboard", "public"): "dashboard_public",
    ("stream", "https"): "stream_https",
}


def with_switch(text: str, section: str, key: str, on: bool) -> str:
    """The configuration text with the switch `[section] key` (one of SWITCHES) set to
    `on`. Every other line stays as it was written. Raises ConfigError when the result
    would not say so, for example because the setting is written in a way this does not
    look for."""
    field = SWITCHES[(section, key)]
    name = f"[{section}] {key}"
    value = f"{key} = {'true' if on else 'false'}\n"
    setting = re.compile(rf"\s*{re.escape(key)}\s*=")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    current, header = "", None
    for number, line in enumerate(lines):
        if found := _SECTION.fullmatch(line.rstrip("\n")):
            current = found.group(1)
            header = number if current == section and header is None else header
        elif current == section and setting.match(line):
            lines[number] = value
            break
    else:
        if header is None:
            lines += ["\n" if lines else "", f"[{section}]\n", value]
        else:
            lines.insert(header + 1, value)
    result = "".join(lines)
    try:
        written = getattr(parse(result), field)
    except ConfigError as exc:
        raise ConfigError(f"cannot set {name}: {exc}") from exc
    if written is not on:
        raise ConfigError(f"cannot set {name}; set it by hand")
    return result
