"""`vpstream init` and `vpstream uninstall`: set the service up on a host, and remove it again.

Both run as root and are safe to repeat. `init` writes down what it created in a manifest, and
`uninstall` removes only that, so a user, a group membership or a firewall rule that was there
before is left alone. Neither ever enables, disables, resets or reloads ufw.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vpstream import config, doctor, firewall, tailscale
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
    dashboard_rule_port: int | None = None  # the ufw rule init added, if it was not there before

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
        port = data.get("dashboard_rule_port")
        return cls(
            user_created=data.get("user_created") is True,
            members_added=[m for m in data.get("members_added") or [] if isinstance(m, str)],
            dashboard_rule_port=port if isinstance(port, int) else None,
        )

    def save(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


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


def dashboard_rule(port: int) -> list[str]:
    """ufw arguments (after `allow` or `delete allow`) for the dashboard port on the tailnet."""
    return ["in", "on", tailscale.INTERFACE, "to", "any", "port", str(port), "proto", "tcp"]


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
        problem = self.install_problem()
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
            config.load(self.layout.paths.config_file)
        except config.ConfigError as exc:
            raise ServiceError(f"fix the configuration first: {exc}") from exc

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
        self._init_dashboard_rule(manifest, cfg.dashboard_port)
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
        manifest.user_created = True
        manifest.save(self.layout.manifest)
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

    def _init_dashboard_rule(self, manifest: Manifest, port: int) -> None:
        old = manifest.dashboard_rule_port
        if old is not None and old != port:
            self._run("ufw", "delete", "allow", *dashboard_rule(old), what="removing the old rule")
            manifest.dashboard_rule_port = None
            manifest.save(self.layout.manifest)
        res = self._run("ufw", "allow", *dashboard_rule(port), what="allowing the dashboard port")
        if "existing" in res.stdout.lower():
            # Either ours from an earlier init (recorded) or the user's own (not recorded).
            self.say(f"ufw: dashboard port {port} on {tailscale.INTERFACE} already allowed")
            return
        manifest.dashboard_rule_port = port
        manifest.save(self.layout.manifest)
        self.say(f"ufw: allowed dashboard port {port} on {tailscale.INTERFACE} only")

    def _init_member(self, manifest: Manifest) -> None:
        user = self.sudo_user
        if not user or user == "root" or not self._succeeds("getent", "passwd", user):
            return
        if self._in_group(user):
            return
        self._run("gpasswd", "-a", user, USER, what=f"adding {user} to the group {USER}")
        if user not in manifest.members_added:
            manifest.members_added.append(user)
            manifest.save(self.layout.manifest)
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
        try:
            port: int | None = config.load(lay.paths.config_file).stream_port
        except config.ConfigError:
            port = None

        have_systemctl = bool(self.runner.which("systemctl"))
        if lay.unit_file.exists():
            if have_systemctl:
                self._run("systemctl", "disable", "--now", UNIT, what="stopping the service")
            lay.unit_file.unlink()
            if have_systemctl:
                self._run("systemctl", "daemon-reload", what="reloading systemd")
                self.runner.run(["systemctl", "reset-failed", UNIT], timeout=TIMEOUT)
            self.say("service: stopped and removed")

        if self.runner.which("ufw"):
            if port is not None:
                self._run("ufw", *firewall.ufw_args("close", port), what="closing the stream port")
                self.say(f"ufw: stream port {port} is closed")
            if manifest.dashboard_rule_port is not None:
                rule = dashboard_rule(manifest.dashboard_rule_port)
                self._run("ufw", "delete", "allow", *rule, what="removing the dashboard rule")
                self.say(f"ufw: removed the rule for dashboard port {manifest.dashboard_rule_port}")

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
