"""`vpstream init` and `vpstream uninstall`: set the service up on a host, and remove it again.

Both run as root and are safe to repeat. `init` writes down what it created in a manifest, and
`uninstall` removes only that, so a user, a group membership or a served port that was there
before is left alone. Neither ever enables, disables, resets or reloads ufw, and neither
resets `tailscale serve` or touches a port of it that is not vpstream's.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vpstream import config, doctor, firewall, tailscale
from vpstream.files import write_atomic
from vpstream.tools import Result, Runner, tail

USER = "vpstream"  # the service user, and its group
UNIT = "vpstream.service"
TIMEOUT = 120.0
NEEDED = (
    "systemctl",
    "visudo",
    "ufw",
    "getent",
    "useradd",
    "userdel",
    "groupdel",
    "gpasswd",
    "id",
    "chown",
    "tailscale",
)
# A path goes into the sudoers rule and the unit file as it is, so it must need no quoting.
_PLAIN_PATH = re.compile(r"/[A-Za-z0-9_./+-]+")


class ServiceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    """Every file and folder init touches. Tests move all of it under a temporary folder."""

    paths: config.Paths = field(default_factory=config.Paths)
    unit_file: Path = Path("/etc/systemd/system") / UNIT
    sudoers_file: Path = Path("/etc/sudoers.d/vpstream")

    @property
    def etc_dir(self) -> Path:
        return self.paths.config_file.parent

    @property
    def manifest(self) -> Path:
        return self.etc_dir / "install.json"

    @classmethod
    def under(cls, root: Path) -> Layout:
        default = cls()
        return cls(
            paths=config.Paths.under(root),
            unit_file=root / default.unit_file.relative_to("/"),
            sudoers_file=root / default.sudoers_file.relative_to("/"),
        )


@dataclass
class Manifest:
    """What init created on this host, so uninstall removes that and nothing else."""

    user_created: bool = False
    members_added: list[str] = field(default_factory=list)
    serve_port: int | None = None  # the `tailscale serve` port init published, if any

    @classmethod
    def load(cls, path: Path) -> Manifest:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            raise ServiceError(f"cannot read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ServiceError(f"{path} is not a vpstream install record")
        port = data.get("serve_port")
        return cls(
            user_created=data.get("user_created") is True,
            members_added=[m for m in data.get("members_added") or [] if isinstance(m, str)],
            serve_port=port if isinstance(port, int) else None,
        )

    def save(self, path: Path) -> None:
        write_atomic(path, json.dumps(asdict(self), indent=2) + "\n")


def port_busy(port: int) -> bool:
    """Whether a program already listens on this loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def unit_text(vpstream_bin: Path, helper: Path) -> str:
    """The systemd unit. `+` runs ExecStopPost as root, so the stream port is closed after a
    stop or a crash without depending on sudo."""
    return f"""\
# Written by `vpstream init`. Removed by `vpstream uninstall`.
[Unit]
Description=vpstream daemon
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User={USER}
Group={USER}
RuntimeDirectory={USER}
RuntimeDirectoryMode=0750
UMask=0027
ExecStart={vpstream_bin} daemon
ExecStopPost=+{helper} close
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def sudoers_text(helper: Path) -> str:
    """The sudoers rule: the service user may run the helper as root with exactly these
    arguments. sudo itself refuses any other argument."""
    commands = ", ".join(f"{helper} {action}" for action in firewall.ACTIONS)
    return (
        "# Written by `vpstream init`. Removed by `vpstream uninstall`.\n"
        f"{USER} ALL=(root) NOPASSWD: {commands}\n"
    )


@dataclass
class Service:
    runner: Runner
    layout: Layout = field(default_factory=Layout)
    euid: int = field(default_factory=os.geteuid)
    sudo_user: str | None = None
    bin_dir: Path = field(default_factory=lambda: Path(sys.executable).parent)
    install_problem: Callable[[], str | None] = lambda: doctor.install_problem(
        Path(__file__).parent
    )
    program_problem: Callable[[Path], str | None] = doctor.program_problem
    port_busy: Callable[[int], bool] = port_busy
    owner_uid: int = 0  # who must own the files root trusts; tests are not root
    say: Callable[[str], None] = print
    pause: Callable[[float], None] = time.sleep

    # --- small helpers

    def _run(self, *args: str | Path, what: str, ok: Sequence[int] = (0,)) -> Result:
        res = self.runner.run([str(a) for a in args], timeout=TIMEOUT)
        if res.returncode not in ok:
            raise ServiceError(f"{what} failed: {tail(res, stdout=True)}")
        return res

    def _succeeds(self, *args: str) -> bool:
        return self.runner.run(list(args), timeout=TIMEOUT).returncode == 0

    def _require_root(self, command: str) -> None:
        if self.euid != 0:
            raise ServiceError(f"this needs root: sudo vpstream {command}")

    @property
    def helper(self) -> Path:
        return self.bin_dir / firewall.HELPER_NAME

    @property
    def vpstream_bin(self) -> Path:
        return self.bin_dir / "vpstream"

    def _user_exists(self) -> bool:
        return self._succeeds("getent", "passwd", USER)

    def _group_exists(self) -> bool:
        return self._succeeds("getent", "group", USER)

    def _in_group(self, user: str) -> bool:
        res = self.runner.run(["id", "-nG", user], timeout=TIMEOUT)
        return res.returncode == 0 and USER in res.stdout.split()

    # --- init

    def check_host(self) -> None:
        """Refuse before changing anything when init could not finish on this host."""
        missing = [name for name in NEEDED if not self.runner.which(name)]
        if missing:
            raise ServiceError(
                f"missing on this host: {', '.join(missing)}. Run `vpstream doctor` for the fixes"
            )
        # The helper is what sudoers lets the service user run as root, so its own file and
        # folder are checked as well as the package.
        problem = self.install_problem() or self.program_problem(self.helper)
        if problem:
            raise ServiceError(
                f"{problem}. The service runs as its own user and cannot use this install. "
                f"Install vpstream for the whole system and run init from there:\n"
                f"  {doctor.SYSTEM_INSTALL}"
            )
        for path in (self.helper, self.vpstream_bin):
            if not _PLAIN_PATH.fullmatch(str(path)):
                raise ServiceError(f"cannot use the path {path}: it has unusual characters")
            if not path.is_file():
                raise ServiceError(f"{path} is missing; reinstall vpstream")
        try:
            cfg = config.load(self.layout.paths.config_file)
        except config.ConfigError as exc:
            raise ServiceError(f"fix the configuration first: {exc}") from exc
        port = cfg.dashboard_port
        published = self._check_serve(port)
        if self.port_busy(port) and not (
            (published or Manifest.load(self.layout.manifest).serve_port == port)
            and self._succeeds("systemctl", "is-active", "--quiet", UNIT)
        ):
            # Publishing it would put that other program on the tailnet.
            raise ServiceError(
                f"port {port} on 127.0.0.1 is already used by another program. "
                f"Set another port under [dashboard] in {self.layout.paths.config_file}"
            )

    def _served(self, port: int) -> str | None:
        return tailscale.serve_target(self.runner, port)

    def _check_serve(self, port: int) -> bool:
        """The dashboard is published with `tailscale serve`; make sure that can work.
        Returns whether the port is published for vpstream already."""
        try:
            node = tailscale.node(self.runner)
            proxy = self._served(port)
        except tailscale.TailscaleDown as exc:
            raise ServiceError(f"Tailscale is needed for the dashboard: {exc}") from exc
        if not node.https:
            raise ServiceError(
                "this tailnet does not issue HTTPS certificates for this host, which "
                "`tailscale serve` needs. Enable HTTPS certificates in the Tailscale admin "
                "console (DNS page), then run init again"
            )
        if proxy is not None and proxy != tailscale.local_target(port):
            raise ServiceError(
                f"port {port} is already used by tailscale serve ({proxy or 'another service'}). "
                f"Set another port under [dashboard] in {self.layout.paths.config_file}"
            )
        return proxy is not None

    def init(self) -> None:
        self._require_root("init")
        self.check_host()
        lay = self.layout
        lay.etc_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        lay.etc_dir.chmod(0o755)
        manifest = Manifest.load(lay.manifest)
        manifest.save(lay.manifest)

        self._init_user(manifest)
        self._init_config()
        cfg = config.load(lay.paths.config_file)
        for folder in (lay.paths.state_dir, lay.paths.cache_dir):
            folder.mkdir(mode=0o750, parents=True, exist_ok=True)
            folder.chmod(0o750)
            self._run("chown", "-R", f"{USER}:{USER}", folder, what=f"giving {folder} to {USER}")
        self._init_sudoers()
        self._init_serve(manifest, cfg.dashboard_port)
        self._init_member(manifest)
        self._init_unit()
        self.say(
            f"vpstream is set up. The stream port {cfg.stream_port} stays closed until a stream "
            "runs.\nCheck it with: vpstream status"
        )

    def _init_user(self, manifest: Manifest) -> None:
        if self._user_exists():
            self.say(f"user {USER}: already there")
            return
        # Written down before each change, never after: an init that is cut off between the
        # two must not leave something uninstall does not know about. Uninstall looks before
        # it removes, so a record of a change that never happened is harmless.
        manifest.user_created = True
        manifest.save(self.layout.manifest)
        self._run(
            "useradd",
            "--system",
            "--user-group",
            "--home-dir",
            "/nonexistent",
            "--no-create-home",
            "--shell",
            "/usr/sbin/nologin",
            USER,
            what=f"creating the user {USER}",
        )
        self.say(f"user {USER}: created")

    def _init_config(self) -> None:
        path = self.layout.paths.config_file
        if path.exists():
            self.say(f"{path}: kept")
        else:
            path.write_text(config.DEFAULT_TEXT, encoding="utf-8")
            self.say(f"{path}: written")
        # The firewall helper trusts this file, so only root may write it.
        path.chmod(0o644)

    def _init_sudoers(self) -> None:
        """Install the rule only after visudo accepts it, and take it out again if the whole
        sudoers configuration does not check afterwards: a broken file would lock sudo."""
        target = self.layout.sudoers_file
        staged = self.layout.etc_dir / "sudoers.new"
        staged.unlink(missing_ok=True)
        fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o440)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(sudoers_text(self.helper))
        try:
            self._run("visudo", "-cf", staged, what="checking the sudoers rule")
            staged.chmod(0o440)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        try:
            self._run("visudo", "-c", what="checking sudoers after adding the rule")
        except ServiceError:
            target.unlink(missing_ok=True)
            raise
        self.say(f"{target}: written and checked")

    def _serve_off(self, port: int) -> bool:
        """Stop publishing `port`, but only when it is there and points at vpstream. Turning
        off a port that is not served is an error in Tailscale, so it is looked up first."""
        if self._served(port) != tailscale.local_target(port):
            return False
        self._run("tailscale", *tailscale.serve_off_args(port), what="unpublishing the dashboard")
        return True

    def _init_serve(self, manifest: Manifest, port: int) -> None:
        old = manifest.serve_port
        if old is not None and old != port:
            self._serve_off(old)
            manifest.serve_port = None
            manifest.save(self.layout.manifest)
        name = tailscale.node(self.runner).name
        if self._served(port) == tailscale.local_target(port):
            # Either ours from an earlier init (recorded) or set up by hand (not recorded).
            self.say(f"tailscale serve: https://{name}:{port}/ already published")
            return
        manifest.serve_port = port
        manifest.save(self.layout.manifest)
        self._run("tailscale", *tailscale.serve_on_args(port), what="publishing the dashboard")
        self.say(f"tailscale serve: dashboard published at https://{name}:{port}/ (tailnet only)")

    def _init_member(self, manifest: Manifest) -> None:
        user = self.sudo_user
        if not user or user == "root" or not self._succeeds("getent", "passwd", user):
            return
        if self._in_group(user):
            return
        if user not in manifest.members_added:
            manifest.members_added.append(user)
            manifest.save(self.layout.manifest)
        self._run("gpasswd", "-a", user, USER, what=f"adding {user} to the group {USER}")
        self.say(
            f"{user} was added to the group {USER}, to use vpstream without sudo. "
            "Log out and in again for it to take effect."
        )

    def _init_unit(self) -> None:
        unit = self.layout.unit_file
        unit.write_text(unit_text(self.vpstream_bin, self.helper), encoding="utf-8")
        unit.chmod(0o644)
        self._run("systemctl", "daemon-reload", what="reloading systemd")
        self._run("systemctl", "enable", UNIT, what="enabling the service")
        # Restart, not start: after an upgrade the running daemon is still the old version.
        self._run("systemctl", "restart", UNIT, what="starting the service")
        self.pause(2.0)
        if not self._succeeds("systemctl", "is-active", "--quiet", UNIT):
            raise ServiceError(
                "the service was started but did not stay up. See why with: "
                "journalctl -u vpstream -n 50 --no-pager"
            )
        self.say(f"{unit}: written; the service is enabled and running")

    # --- uninstall

    def uninstall(self, purge: bool = False) -> None:
        self._require_root("uninstall")
        lay = self.layout
        manifest = Manifest.load(lay.manifest)
        record = lay.paths.open_ports_file
        # Without any trace of an install, a rule for the stream port is not vpstream's.
        installed = any(p.exists() for p in (lay.manifest, lay.unit_file, lay.sudoers_file, record))
        port: int | None = None
        port_problem = ""
        try:
            port = config.load(lay.paths.config_file).stream_port
        except config.ConfigError as exc:
            port_problem = str(exc)

        have_systemctl = bool(self.runner.which("systemctl"))
        if lay.unit_file.exists():
            if have_systemctl:
                self._run("systemctl", "disable", "--now", UNIT, what="stopping the service")
            lay.unit_file.unlink()
            if have_systemctl:
                self._run("systemctl", "daemon-reload", what="reloading systemd")
                self.runner.run(["systemctl", "reset-failed", UNIT], timeout=TIMEOUT)
            self.say("service: stopped and removed")

        if installed and self.runner.which("ufw"):
            # After the stop: the service's own close has normally emptied the record.
            opened = firewall.recorded_ports(record, self.owner_uid)
            for one in dict.fromkeys([*opened, *([] if port is None else [port])]):
                self._run("ufw", *firewall.ufw_args("close", one), what="closing the stream port")
                self.say(f"ufw: stream port {one} is closed")
            if port is None:
                self.say(
                    f"WARNING: the stream port is not known ({port_problem}), so its ufw rule "
                    "could not be removed. Look for one with: sudo ufw status"
                )

        if manifest.serve_port is not None and self.runner.which("tailscale"):
            try:
                if self._serve_off(manifest.serve_port):
                    self.say(f"tailscale serve: port {manifest.serve_port} is no longer published")
            except tailscale.TailscaleDown as exc:
                self.say(f"note: could not check tailscale serve ({exc})")

        if lay.sudoers_file.exists():
            lay.sudoers_file.unlink()
            self.say(f"{lay.sudoers_file}: removed")

        # Members first: userdel removes the user's group only when nobody else is in it.
        if self._group_exists():
            for member in manifest.members_added:
                if self._in_group(member):
                    self._run("gpasswd", "-d", member, USER, what=f"removing {member} from {USER}")
                    self.say(f"{member}: removed from the group {USER}")
        if manifest.user_created:
            if self._user_exists():
                self._run("userdel", USER, what=f"removing the user {USER}")
                self.say(f"user {USER}: removed")
            if self._group_exists():
                self._run("groupdel", USER, what=f"removing the group {USER}")

        for folder in (lay.paths.state_dir, lay.paths.run_dir, lay.etc_dir):
            _remove_tree(folder)
        cache = lay.paths.cache_dir
        if purge or (cache.is_dir() and not any(cache.iterdir())):
            _remove_tree(cache)
        elif cache.exists():
            self.say(
                f"{cache}: kept (prepared videos). Remove it with: sudo vpstream uninstall --purge"
            )
        self.say("vpstream is removed from this host.")


def _remove_tree(path: Path) -> None:
    """Remove one of vpstream's own folders. A link is removed as a link, never followed."""
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
