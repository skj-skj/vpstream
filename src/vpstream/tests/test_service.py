from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from vpstream import cli, config, service, tailscale
from vpstream.tests.fakes import NAME, node_status, serve_status
from vpstream.tools import Result

OTHERS = {443: "http://127.0.0.1:2283", 8443: "http://127.0.0.1:3000"}  # not vpstream's
MINE = "http://127.0.0.1:1338"


CA_PEM = (Path(__file__).parent / "data" / "caddy-root.crt").read_text()


class FakeHost:
    """A host that remembers its users, groups and ufw rules, like the real commands do."""

    def __init__(self) -> None:
        self.available = {*service.NEEDED, *(n for v in service.NEEDED_FOR.values() for n in v)}
        self.users = {"root", "alice"}
        self.groups: dict[str, set[str]] = {"alice": set()}
        self.rules = {"in on tailscale0", "41641/udp"}
        self.serve = dict(OTHERS)
        self.https = True
        self.active = True
        self.caddy = "v2.11.9 h1:x=\n"  # what `caddy version` prints
        self.fail: dict[tuple[str, ...], str] = {}
        self.calls: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}" if name in self.available else None

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        cmd = list(args)
        self.calls.append(cmd)
        for prefix, message in self.fail.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return Result(1, "", message)
        if cmd[0].endswith("/caddy") and cmd[1:] == ["version"]:
            return Result(0, self.caddy, "")
        return getattr(self, f"_{cmd[0]}", lambda _: Result(0, "", ""))(cmd)

    def _getent(self, cmd: list[str]) -> Result:
        known = self.users if cmd[1] == "passwd" else self.groups
        return Result(0 if cmd[2] in known else 2, "", "")

    def _id(self, cmd: list[str]) -> Result:
        mine = [g for g, members in self.groups.items() if cmd[2] in members or g == cmd[2]]
        return Result(0, " ".join(mine) + "\n", "")

    def _useradd(self, cmd: list[str]) -> Result:
        name = cmd[-1]
        if name in self.users:
            return Result(9, "", f"useradd: user '{name}' already exists")
        self.users.add(name)
        self.groups[name] = set()
        return Result(0, "", "")

    def _userdel(self, cmd: list[str]) -> Result:
        self.users.discard(cmd[1])
        if not self.groups.get(cmd[1]):  # like the real one: only when nobody else is in it
            self.groups.pop(cmd[1], None)
        return Result(0, "", "")

    def _groupdel(self, cmd: list[str]) -> Result:
        self.groups.pop(cmd[1], None)
        return Result(0, "", "")

    def _gpasswd(self, cmd: list[str]) -> Result:
        members = self.groups[cmd[3]]
        members.add(cmd[2]) if cmd[1] == "-a" else members.discard(cmd[2])
        return Result(0, "", "")

    def _ufw(self, cmd: list[str]) -> Result:
        if cmd[1] == "allow":
            rule = " ".join(cmd[2:])
            if rule in self.rules:
                return Result(0, "Skipping adding existing rule\n", "")
            self.rules.add(rule)
            return Result(0, "Rule added\nRule added (v6)\n", "")
        if cmd[1:3] == ["delete", "allow"]:
            rule = " ".join(cmd[3:])
            if rule not in self.rules:
                return Result(0, "Could not delete non-existent rule\n", "")
            self.rules.discard(rule)
            return Result(0, "Rule deleted\n", "")
        raise AssertionError(f"vpstream must never run: {' '.join(cmd)}")

    def _tailscale(self, cmd: list[str]) -> Result:
        if cmd[1:] == ["status", "--json"]:
            return Result(0, json.dumps(node_status(self.https)), "")
        if cmd[1:] == ["serve", "status", "--json"]:
            return Result(0, json.dumps(serve_status(self.serve)), "")
        if cmd[1:3] == ["serve", "--bg"] and cmd[3].startswith("--https=") and len(cmd) == 5:
            self.serve[int(cmd[3].partition("=")[2])] = cmd[4]
            return Result(0, "Serve started and running in the background.\n", "")
        if cmd[1] == "serve" and cmd[2].startswith("--https=") and cmd[3:] == ["off"]:
            port = int(cmd[2].partition("=")[2])
            if port not in self.serve:  # like the real one
                return Result(1, "", "error: failed to remove web serve: handler does not exist")
            del self.serve[port]
            return Result(0, "", "")
        raise AssertionError(f"vpstream must never run: {' '.join(cmd)}")

    def _openssl(self, cmd: list[str]) -> Result:
        """Writes what `req -x509` would: a key and a certificate (a real one, for its
        fingerprint)."""
        if cmd[1:3] != ["req", "-x509"]:
            raise AssertionError(f"vpstream must never run: {' '.join(cmd)}")
        Path(cmd[cmd.index("-keyout") + 1]).write_text("KEY\n")
        Path(cmd[cmd.index("-out") + 1]).write_text(CA_PEM)
        return Result(0, "", "")

    def _systemctl(self, cmd: list[str]) -> Result:
        if cmd[1] == "is-active":
            return Result(0 if self.active else 3, "", "")
        return Result(0, "", "")


class Setup:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.host = FakeHost()
        self.layout = service.Layout.under(root)
        self.said: list[str] = []
        self.asked: list[str] = []
        bin_dir = Path("/opt/pipx/venvs/vpstream/bin")
        self.kwargs = {
            "layout": self.layout,
            "euid": 0,
            "sudo_user": "alice",
            "bin_dir": bin_dir,
            "install_problem": lambda: None,
            "program_problem": lambda path: None,
            "port_busy": lambda port: False,
            "outgoing_address": lambda: "203.0.113.5",
            "outgoing_address6": lambda: "",
            "ask_daemon": lambda socket, route: self.asked.append(route),
            "owner_uid": os.getuid(),
            "say": self.said.append,
            "pause": lambda seconds: None,
        }
        for folder in (self.layout.unit_file.parent, self.layout.sudoers_file.parent):
            folder.mkdir(parents=True)

    def service(self, **changes: object) -> service.Service:
        return service.Service(self.host, **{**self.kwargs, **changes})  # type: ignore[arg-type]

    def files(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())

    def snapshot(self) -> tuple[set[str], dict[str, set[str]], set[str], dict[int, str]]:
        groups = {g: set(m) for g, m in self.host.groups.items()}
        return set(self.host.users), groups, set(self.host.rules), dict(self.host.serve)


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    # The programs init points at do not exist on the test host.
    real = Path.is_file
    monkeypatch.setattr(
        Path, "is_file", lambda p: str(p).startswith("/opt/pipx/venvs/vpstream/bin/") or real(p)
    )
    return Setup(tmp_path)


# --- pure parts


def test_unit_text() -> None:
    args = (Path("/opt/v/bin/vpstream"), Path("/opt/v/bin/vpstream-fw"))
    text = service.unit_text(*args)
    assert "User=vpstream\nGroup=vpstream\n" in text
    assert "RuntimeDirectory=vpstream\nRuntimeDirectoryMode=0750\n" in text
    assert "ExecStart=/opt/v/bin/vpstream daemon\n" in text
    # `+`: closing the port after a stop or crash runs as root and does not depend on sudo.
    assert "ExecStopPost=+/opt/v/bin/vpstream-fw close\n" in text
    assert "After=network-online.target tailscaled.service\n" in text
    assert "WantedBy=multi-user.target\n" in text
    # The service may bind port 80 only with HTTPS, where Let's Encrypt checks.
    assert "CAP_NET_BIND_SERVICE" not in text
    https = service.unit_text(*args, https=True)
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE\n" in https
    # A bounding set would hold sudo, which runs the firewall helper, to that one capability.
    assert "CapabilityBoundingSet" not in https
    for unit in (text, https):
        # Whatever the mode, a stop closes what the helper opened.
        assert "ExecStopPost=+/opt/v/bin/vpstream-fw acme-close\n" in unit
        assert "NoNewPrivileges" not in unit  # it would stop the daemon's sudo call


def test_sudoers_rule_allows_exactly_the_helper_actions() -> None:
    lines = service.sudoers_text(Path("/opt/v/bin/vpstream-fw")).splitlines()
    assert lines[0].startswith("#")
    helper = "/opt/v/bin/vpstream-fw"
    assert lines[1:] == [
        f"vpstream ALL=(root) NOPASSWD: {helper} open, {helper} close, "
        f"{helper} dash-open, {helper} dash-close, {helper} acme-open, {helper} acme-close"
    ]


def test_serve_arguments_name_one_port_and_never_reset() -> None:
    assert tailscale.serve_on_args(1338) == ["serve", "--bg", "--https=1338", MINE]
    assert tailscale.serve_off_args(1338) == ["serve", "--https=1338", "off"]


def test_serve_proxy_reads_the_configuration() -> None:
    cfg = {
        "TCP": {"443": {"HTTPS": True}, "5000": {"TCPForward": "127.0.0.1:22"}},
        "Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:2283"}}}},
    }
    assert tailscale.serve_proxy(cfg, 443) == "http://127.0.0.1:2283"
    assert tailscale.serve_proxy(cfg, 5000) == ""  # used, but not as a proxy for /
    assert tailscale.serve_proxy(cfg, 1338) is None
    assert tailscale.serve_proxy({}, 443) is None


# --- init


def test_init_creates_everything(setup: Setup) -> None:
    before = setup.snapshot()
    setup.service().init()
    host, lay = setup.host, setup.layout

    assert "vpstream" in host.users and host.groups["vpstream"] == {"alice"}
    assert host.ran("useradd")[0][1:] == [
        "--system",
        "--user-group",
        "--home-dir",
        "/nonexistent",
        "--no-create-home",
        "--shell",
        "/usr/sbin/nologin",
        "vpstream",
    ]
    assert host.rules == before[2]  # no ufw rule: the dashboard is published by Tailscale
    assert host.ran("ufw") == []
    assert host.serve == {**OTHERS, 1338: MINE}
    assert ["tailscale", "serve", "--bg", "--https=1338", MINE] in host.calls
    assert config.load(lay.paths.config_file) == config.Config()
    assert lay.paths.config_file.stat().st_mode & 0o777 == 0o644
    assert lay.sudoers_file.read_text() == service.sudoers_text(
        setup.kwargs["bin_dir"] / "vpstream-fw"
    )  # type: ignore[operator]
    assert lay.sudoers_file.stat().st_mode & 0o777 == 0o440
    assert "ExecStart=/opt/pipx/venvs/vpstream/bin/vpstream daemon" in lay.unit_file.read_text()
    assert lay.paths.state_dir.is_dir() and lay.paths.cache_dir.is_dir()
    assert json.loads(lay.manifest.read_text()) == {
        "user_created": True,
        "members_added": ["alice"],
        "serve_port": 1338,
    }
    systemctl = [c[1:] for c in host.ran("systemctl")]
    assert systemctl == [
        ["daemon-reload"],
        ["enable", "vpstream.service"],
        ["restart", "vpstream.service"],
        ["is-active", "--quiet", "vpstream.service"],
    ]
    assert any("Log out and in again" in line for line in setup.said)
    assert any(f"https://{NAME}:1338/" in line for line in setup.said)


def test_the_sudoers_rule_is_checked_before_and_after_it_is_installed(setup: Setup) -> None:
    setup.service().init()
    staged = str(setup.layout.etc_dir / "sudoers.new")
    assert [c[1:] for c in setup.host.ran("visudo")] == [["-cf", staged], ["-c"]]
    assert not Path(staged).exists()


def test_init_is_safe_to_run_again(setup: Setup) -> None:
    setup.service().init()
    files, state = setup.files(), setup.snapshot()
    manifest = setup.layout.manifest.read_text()
    setup.layout.paths.config_file.write_text("[stream]\nport = 2000\n")
    setup.host.calls.clear()

    setup.service().init()
    assert setup.files() == files and setup.snapshot() == state
    assert setup.layout.manifest.read_text() == manifest
    assert setup.host.ran("useradd") == [] and setup.host.ran("gpasswd") == []
    assert setup.host.ran("tailscale", "serve", "--bg") == []
    assert setup.layout.paths.config_file.read_text() == "[stream]\nport = 2000\n"  # kept
    assert setup.host.ran("systemctl", "restart")  # an upgrade needs the new code running


def test_init_then_uninstall_leaves_no_trace(setup: Setup) -> None:
    before = setup.snapshot()
    setup.service().init()
    setup.service().uninstall()
    assert setup.files() == []
    assert setup.snapshot() == before
    assert not setup.layout.etc_dir.exists()
    assert not setup.layout.paths.state_dir.exists()
    assert not setup.layout.paths.cache_dir.exists()
    assert setup.host.ran("systemctl", "disable") == [
        ["systemctl", "disable", "--now", "vpstream.service"]
    ]
    assert ["ufw", "delete", "allow", "1337/tcp"] in setup.host.calls


def test_uninstall_is_safe_to_run_again_and_without_init(setup: Setup) -> None:
    before = setup.snapshot()
    setup.service().uninstall()
    assert setup.snapshot() == before and setup.files() == []
    setup.service().init()
    setup.service().uninstall()
    setup.service().uninstall()
    assert setup.snapshot() == before and setup.files() == []


def test_uninstall_leaves_what_was_there_before_init(setup: Setup) -> None:
    host = setup.host
    host.users.add("vpstream")
    host.groups["vpstream"] = {"alice"}
    host.serve[1338] = MINE  # published by hand before init
    before = setup.snapshot()

    setup.service().init()
    assert json.loads(setup.layout.manifest.read_text()) == {
        "user_created": False,
        "members_added": [],
        "serve_port": None,
    }
    setup.service().uninstall()
    assert setup.snapshot() == before
    assert host.ran("userdel") == [] and host.ran("groupdel") == []
    assert host.ran("tailscale", "serve", "--https=1338") == []


def test_only_the_stream_port_is_touched_in_ufw_and_only_one_port_in_serve(setup: Setup) -> None:
    setup.service().init()
    setup.service().uninstall(purge=True)
    setup.service().uninstall(purge=True)
    assert {" ".join(c[1:]) for c in setup.host.ran("ufw")} == {"delete allow 1337/tcp"}
    changes = [c[2:] for c in setup.host.ran("tailscale", "serve") if "status" not in c]
    # Published once, turned off once: the second uninstall looks first and finds nothing.
    assert changes == [["--bg", "--https=1338", MINE], ["--https=1338", "off"]]
    assert setup.host.serve == OTHERS


def test_init_refuses_a_port_that_serve_uses_for_something_else(setup: Setup) -> None:
    cfg = setup.layout.paths.config_file
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[dashboard]\nport = 8443\n")
    with pytest.raises(service.ServiceError, match="8443 is already used by tailscale serve"):
        setup.service().init()
    assert setup.host.serve == OTHERS
    assert setup.host.ran("useradd") == [] and setup.host.ran("systemctl") == []


def test_init_needs_https_certificates_on_the_tailnet(setup: Setup) -> None:
    setup.host.https = False
    with pytest.raises(service.ServiceError, match="HTTPS certificates"):
        setup.service().init()
    assert setup.host.ran("useradd") == [] and setup.files() == []


def test_init_needs_tailscale_running(setup: Setup) -> None:
    setup.host.fail[("tailscale", "status")] = "Tailscale is stopped."
    with pytest.raises(service.ServiceError, match=r"Tailscale is needed.*stopped"):
        setup.service().init()
    assert setup.host.ran("useradd") == [] and setup.files() == []


def test_uninstall_leaves_a_port_that_was_given_to_something_else(setup: Setup) -> None:
    setup.service().init()
    setup.host.serve[1338] = "http://127.0.0.1:9000"  # changed by hand after init
    setup.service().uninstall()
    assert setup.host.serve[1338] == "http://127.0.0.1:9000"
    assert setup.host.ran("tailscale", "serve", "--https=1338") == []


def test_uninstall_keeps_prepared_videos_unless_purged(setup: Setup) -> None:
    setup.service().init()
    video = setup.layout.paths.cache_dir / "film" / "base.m3u8"
    video.parent.mkdir()
    video.write_text("#EXTM3U\n")
    setup.service().uninstall()
    assert setup.files() == [str(video.relative_to(setup.root))]
    assert any("--purge" in line for line in setup.said)
    setup.service().uninstall(purge=True)
    assert setup.files() == [] and not setup.layout.paths.cache_dir.exists()


def _installed_ffmpeg(setup: Setup) -> tuple[Path, Path]:
    """What `vpstream install ffmpeg` leaves: its folder, a partial new build, two links."""
    ff = setup.layout.ffmpeg
    for folder in (ff.install_dir, ff.staging_dir):
        (folder / "bin").mkdir(parents=True)
        (folder / "bin" / "ffmpeg").write_text("elf")
    (ff.install_dir / "bin" / "ffprobe").write_text("elf")
    ff.bin_dir.mkdir(parents=True)
    for prog in ("ffmpeg", "ffprobe"):
        (ff.bin_dir / prog).symlink_to(ff.install_dir / "bin" / prog)
    return ff.install_dir, ff.bin_dir


def test_uninstall_purge_removes_the_ffmpeg_vpstream_installed(setup: Setup) -> None:
    folder, bin_dir = _installed_ffmpeg(setup)
    (bin_dir / "caddy").write_text("someone else's")  # never ours
    setup.service().init()
    setup.service().uninstall()
    assert (bin_dir / "ffmpeg").is_symlink() and folder.is_dir()  # kept without --purge
    setup.service().uninstall(purge=True)
    assert not folder.exists() and not setup.layout.ffmpeg.staging_dir.exists()
    assert sorted(p.name for p in bin_dir.iterdir()) == ["caddy"]
    assert f"{bin_dir / 'ffprobe'}: removed" in setup.said
    assert any("is not put back" in line for line in setup.said)


def test_uninstall_purge_leaves_an_ffmpeg_that_is_not_vpstreams(setup: Setup) -> None:
    other = setup.root / "usr/bin"
    other.mkdir(parents=True)
    (other / "ffmpeg").write_text("apt's")
    bin_dir = setup.layout.ffmpeg.bin_dir
    bin_dir.mkdir(parents=True)
    (bin_dir / "ffmpeg").symlink_to(other / "ffmpeg")
    (bin_dir / "ffprobe").write_text("built by hand")
    setup.service().uninstall(purge=True)
    assert (bin_dir / "ffmpeg").is_symlink() and (other / "ffmpeg").read_text() == "apt's"
    assert (bin_dir / "ffprobe").read_text() == "built by hand"
    assert not any("is not put back" in line for line in setup.said)  # nothing was removed


def test_uninstall_purge_never_follows_an_install_folder_that_is_a_link(setup: Setup) -> None:
    usr = setup.root / "usr"
    (usr / "bin").mkdir(parents=True)
    (usr / "bin" / "ffmpeg").write_text("apt's")
    ff = setup.layout.ffmpeg
    ff.install_dir.parent.mkdir(parents=True)
    ff.install_dir.symlink_to(usr)
    ff.bin_dir.mkdir(parents=True)
    (ff.bin_dir / "ffmpeg").symlink_to(usr / "bin" / "ffmpeg")
    setup.service().uninstall(purge=True)
    assert (ff.bin_dir / "ffmpeg").is_symlink() and (usr / "bin" / "ffmpeg").read_text() == "apt's"
    assert not ff.install_dir.is_symlink()  # the link itself goes, never what it points at


def test_a_changed_dashboard_port_moves_the_published_port(setup: Setup) -> None:
    setup.service().init()
    setup.layout.paths.config_file.write_text("[dashboard]\nport = 4000\n")
    setup.service().init()
    assert setup.host.serve == {**OTHERS, 4000: "http://127.0.0.1:4000"}
    setup.service().uninstall()
    assert setup.host.serve == OTHERS


def test_root_without_sudo_adds_nobody_to_the_group(setup: Setup) -> None:
    setup.service(sudo_user=None).init()
    assert setup.host.groups["vpstream"] == set()
    setup.service(sudo_user="root").init()
    setup.service(sudo_user="nobody-here").init()
    assert setup.host.ran("gpasswd") == []


# --- refusals: nothing may be changed


@pytest.mark.parametrize("command", ["init", "uninstall"])
def test_root_is_required(setup: Setup, command: str) -> None:
    with pytest.raises(service.ServiceError, match=f"sudo vpstream {command}"):
        getattr(setup.service(euid=1000), command)()
    assert setup.host.calls == [] and setup.files() == []


def test_init_refuses_a_per_user_install(setup: Setup) -> None:
    problem = "other users cannot enter /home/alice"
    with pytest.raises(service.ServiceError, match="PIPX_HOME=/opt/pipx") as exc:
        setup.service(install_problem=lambda: problem).init()
    assert problem in str(exc.value)
    assert setup.host.calls == [] and setup.files() == []


def test_init_lists_missing_programs(setup: Setup) -> None:
    setup.host.available -= {"ufw", "visudo", "tailscale"}
    with pytest.raises(service.ServiceError, match="visudo, ufw, tailscale"):
        setup.service().init()
    assert setup.host.calls == [] and setup.files() == []


def test_init_refuses_a_path_that_would_need_quoting(setup: Setup) -> None:
    with pytest.raises(service.ServiceError, match="unusual characters"):
        setup.service(bin_dir=Path("/opt/my venv/bin")).init()
    with pytest.raises(service.ServiceError, match="unusual characters"):
        setup.service(bin_dir=Path("/opt/a,b/bin")).init()
    assert setup.host.calls == []


def test_init_refuses_a_bad_config_before_changing_anything(setup: Setup) -> None:
    cfg = setup.layout.paths.config_file
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[stream]\nport = 22\n")
    with pytest.raises(service.ServiceError, match="fix the configuration first"):
        setup.service().init()
    assert setup.host.calls == []


def test_a_rule_visudo_rejects_is_never_installed(setup: Setup) -> None:
    setup.host.fail[("visudo", "-cf")] = "syntax error near line 2"
    with pytest.raises(service.ServiceError, match="syntax error"):
        setup.service().init()
    assert not setup.layout.sudoers_file.exists()
    assert not (setup.layout.etc_dir / "sudoers.new").exists()
    assert setup.host.ran("systemctl") == []


def test_the_rule_is_removed_if_sudoers_does_not_check_afterwards(setup: Setup) -> None:
    setup.host.fail[("visudo", "-c")] = "/etc/sudoers.d/other: syntax error"
    with pytest.raises(service.ServiceError, match="after adding the rule"):
        setup.service().init()
    assert not setup.layout.sudoers_file.exists()


def test_init_says_when_the_service_does_not_stay_up(setup: Setup) -> None:
    setup.host.active = False
    with pytest.raises(service.ServiceError, match="journalctl -u vpstream"):
        setup.service().init()


def test_a_failed_init_can_be_undone(setup: Setup) -> None:
    before = setup.snapshot()
    setup.host.fail[("systemctl", "enable")] = "boom"
    with pytest.raises(service.ServiceError, match="enabling the service failed: boom"):
        setup.service().init()
    del setup.host.fail[("systemctl", "enable")]
    setup.service().uninstall()
    assert setup.snapshot() == before and setup.files() == []


def test_a_link_in_place_of_a_folder_is_not_followed(setup: Setup) -> None:
    precious = setup.root / "precious"
    precious.mkdir()
    (precious / "photo.jpg").write_text("x")
    cache = setup.layout.paths.cache_dir
    cache.parent.mkdir(parents=True)
    cache.symlink_to(precious)
    setup.service().uninstall(purge=True)
    assert not cache.is_symlink() and (precious / "photo.jpg").read_text() == "x"


# --- the command line


def test_cli_init_and_uninstall_need_root(capsys: pytest.CaptureFixture[str]) -> None:
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    assert cli.main(["init"]) == 1
    assert "sudo vpstream init" in capsys.readouterr().err
    assert cli.main(["uninstall", "--yes"]) == 1
    assert "sudo vpstream uninstall" in capsys.readouterr().err


# --- interrupted inits, busy ports and a helper others can change


def test_uninstall_on_a_host_that_was_never_set_up_touches_no_ufw_rule(setup: Setup) -> None:
    setup.host.rules.add("1337/tcp")  # made by hand, for something else
    setup.service().uninstall()
    assert "1337/tcp" in setup.host.rules and setup.host.ran("ufw") == []


def test_uninstall_closes_what_the_helper_recorded_and_warns_about_a_bad_config(
    setup: Setup,
) -> None:
    setup.service().init()
    setup.host.rules |= {"1337/tcp", "2000/tcp"}
    setup.layout.paths.open_ports_file.write_text("2000\n")  # opened before the config broke
    setup.layout.paths.open_ports_file.chmod(0o644)
    setup.layout.paths.config_file.write_text("[stream]\nprot = 1337\n")
    setup.said.clear()
    setup.service().uninstall()
    assert "2000/tcp" not in setup.host.rules
    assert "1337/tcp" in setup.host.rules  # unknown to vpstream now, so it says so
    assert any(s.startswith("WARNING") and "sudo ufw status" in s for s in setup.said)


@pytest.mark.parametrize("cut_after", ["useradd", "gpasswd", "tailscale serve --bg"])
def test_an_init_cut_off_right_after_a_change_can_still_be_undone(
    setup: Setup, cut_after: str
) -> None:
    before = setup.snapshot()
    real = setup.host.run

    def run(args: Sequence[str], *, timeout: float | None = None) -> Result:
        res = real(args, timeout=timeout)
        if " ".join(args).startswith(cut_after):
            raise KeyboardInterrupt  # the change happened; init never got to its next line
        return res

    setup.host.run = run  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        setup.service().init()
    setup.host.run = real  # type: ignore[method-assign]
    setup.service().uninstall()
    assert setup.snapshot() == before


def test_init_refuses_a_dashboard_port_another_program_listens_on(setup: Setup) -> None:
    setup.host.active = False  # vpstream is not running, so the listener is someone else
    with pytest.raises(service.ServiceError, match=r"1338 on 127\.0\.0\.1 is already used"):
        setup.service(port_busy=lambda port: True).init()
    assert setup.host.serve == OTHERS and setup.host.ran("useradd") == []


def test_init_again_accepts_the_port_its_own_daemon_listens_on(setup: Setup) -> None:
    setup.service().init()
    setup.service(port_busy=lambda port: True).init()
    assert setup.host.serve == {**OTHERS, 1338: MINE}


def test_port_busy_sees_a_listener() -> None:
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert service.port_busy(port)
    assert not service.port_busy(port)


def test_init_refuses_a_helper_that_others_can_change(setup: Setup) -> None:
    problem = "/opt/pipx/venvs/vpstream/bin/vpstream-fw is writable by users other than root"
    with pytest.raises(service.ServiceError, match="writable by users other than root"):
        setup.service(program_problem=lambda path: problem).init()
    assert setup.host.calls == [] and setup.files() == []


# --- the public dashboard: switching on and off


GOOD = "a long enough password"


def _dash_ports(setup: Setup) -> list[int]:
    from vpstream import firewall

    return firewall.recorded_ports(setup.layout.paths.open_dashboard_ports_file, os.getuid())


def _helper_opened_dashboard(setup: Setup, port: int = 1338) -> None:
    """What the daemon's `dash-open` leaves behind: the rule, and the record of it."""
    setup.host.rules.add(f"{port}/tcp")
    record = setup.layout.paths.open_dashboard_ports_file
    record.write_text(f"{port}\n")
    record.chmod(0o644)


def test_init_no_tailscale_makes_the_dashboard_public_with_only_a_verifier(
    setup: Setup,
) -> None:
    from vpstream import login

    setup.service().init()
    assert setup.host.serve[1338] == MINE
    setup.host.calls.clear()
    setup.service().init(public=True, password=GOOD)
    paths = setup.layout.paths
    assert config.load(paths.config_file).dashboard_public is True
    assert 1338 not in setup.host.serve and setup.host.serve == OTHERS, "unpublished, ours only"
    assert json.loads(setup.layout.manifest.read_text())["serve_port"] is None
    key = paths.dashboard_key
    assert key.stat().st_mode & 0o777 == 0o640
    # Given to the group before it took its place, so a cut-off init leaves no root-only file.
    assert ["chown", "root:vpstream", str(key) + ".tmp"] in setup.host.calls
    text = key.read_text()
    assert GOOD not in text and GOOD.encode().hex() not in text
    verifier = login.load_verifier(key)
    assert verifier is not None and verifier.iterations == login.ITERATIONS
    assert login.derive_key(GOOD, verifier.salt, verifier.iterations) == verifier.key
    assert "ExecStopPost=+/opt/pipx/venvs/vpstream/bin/vpstream-fw dash-close" in (
        setup.layout.unit_file.read_text()
    )
    assert setup.host.ran("ufw") == [], "the daemon opens the port while it serves"
    said = "\n".join(setup.said)
    assert "https://203.0.113.5:1338/" in said and "provider" in said


def test_init_prints_the_authority_the_browser_must_trust(setup: Setup) -> None:
    from vpstream import caddy

    cert = setup.layout.paths.dashboard_ca_cert
    cert.parent.mkdir(parents=True)
    pem = (Path(__file__).parent / "data" / "caddy-root.crt").read_text()
    cert.write_text(pem)
    setup.service().init(public=True, password=GOOD)
    said = "\n".join(setup.said)
    assert caddy.certificate_fingerprint(pem) in said
    assert "vpstream dashboard-cert" in said


def test_init_makes_an_authority_for_this_host_only(setup: Setup) -> None:
    from vpstream import dashboard_ca

    setup.service(outgoing_address6=lambda: "2001:db8::5").init(public=True, password=GOOD)
    paths = setup.layout.paths
    [make] = setup.host.ran("openssl")
    assert make[1:4] == ["req", "-x509", "-new"]
    assert dashboard_ca.covered(paths) == ["203.0.113.5", "2001:db8::5"]
    key = paths.dashboard_ca_key
    assert key.stat().st_mode & 0o777 == 0o640 and paths.dashboard_ca_cert.read_text() == CA_PEM
    assert paths.dashboard_ca_cert.stat().st_mode & 0o777 == 0o644
    # Given to the group before it took its place.
    chown = [c for c in setup.host.ran("chown") if c[1] == "root:vpstream" and "ca.key" in c[2]]
    assert chown and not chown[0][2].startswith(str(key))
    assert not paths.dashboard_ca_cert.with_name("dashboard-ca.new").exists()
    said = "\n".join(setup.said)
    assert "A new authority was made" in said and "Caddy Local Authority" in said


def test_init_keeps_the_authority_while_it_covers_the_host(setup: Setup) -> None:
    setup.service().init(public=True, password=GOOD)
    setup.service().init()
    assert len(setup.host.ran("openssl")) == 1
    assert "A new authority was made" not in setup.said[-8:]
    # A new address of this host needs a new authority: a phone would refuse the old one.
    setup.service(outgoing_address=lambda: "198.51.100.7").init()
    assert len(setup.host.ran("openssl")) == 2


def test_init_refuses_when_the_authority_cannot_be_made(setup: Setup) -> None:
    setup.host.fail[("openssl",)] = "unable to load config"
    with pytest.raises(service.ServiceError, match="certificate authority"):
        setup.service().init(public=True, password=GOOD)
    assert not setup.layout.paths.dashboard_ca_hosts.exists()


def test_going_back_to_the_tailnet_removes_the_authority(setup: Setup) -> None:
    setup.service().init(public=True, password=GOOD)
    setup.service().init(public=False)
    paths = setup.layout.paths
    assert not any(
        p.exists()
        for p in (paths.dashboard_ca_cert, paths.dashboard_ca_key, paths.dashboard_ca_hosts)
    )


def test_init_without_a_switch_keeps_the_dashboard_where_it_is(setup: Setup) -> None:
    setup.service().init(public=True, password=GOOD)
    key = setup.layout.paths.dashboard_key.read_text()
    setup.host.calls.clear()
    svc = setup.service()
    assert svc.dashboard_public() is True and svc.wants_password() is False
    svc.init()  # an upgrade: no password asked, none changed, still public
    assert setup.layout.paths.dashboard_key.read_text() == key
    assert config.load(setup.layout.paths.config_file).dashboard_public is True
    assert setup.host.ran("tailscale", "serve", "--bg") == []


def test_going_public_needs_a_good_password_before_anything_changes(setup: Setup) -> None:
    before_files, before = setup.files(), setup.snapshot()
    assert setup.service().wants_password(True) is True
    with pytest.raises(service.ServiceError, match="needs a password"):
        setup.service().init(public=True)
    with pytest.raises(service.ServiceError, match="at least"):
        setup.service().init(public=True, password="short")
    assert setup.files() == before_files and setup.snapshot() == before


def test_going_public_needs_no_tailscale_but_needs_caddy_and_the_address(setup: Setup) -> None:
    setup.host.available.discard("tailscale")
    setup.service().init(public=True, password=GOOD)
    setup.service().uninstall()
    setup.host.available.discard("caddy")
    with pytest.raises(service.ServiceError, match="missing on this host: caddy"):
        setup.service().init(public=True, password=GOOD)
    setup.host.available.add("caddy")
    with pytest.raises(service.ServiceError, match=r"public address is not known"):
        setup.service(outgoing_address=lambda: "").init(public=True, password=GOOD)
    setup.layout.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    setup.layout.paths.config_file.write_text('[stream]\nhost = "203.0.113.9"\n')
    setup.service(outgoing_address=lambda: "").init(public=True, password=GOOD)
    assert any("https://203.0.113.9:1338/" in line for line in setup.said)


def test_back_to_the_tailnet_removes_the_password_and_closes_the_port(setup: Setup) -> None:
    before = setup.snapshot()
    setup.service().init(public=True, password=GOOD)
    _helper_opened_dashboard(setup)
    paths = setup.layout.paths
    paths.login_record.write_text("{}")
    setup.service().init(public=False)
    assert config.load(paths.config_file).dashboard_public is False
    assert not paths.dashboard_key.exists() and not paths.login_record.exists()
    assert "1338/tcp" not in setup.host.rules and _dash_ports(setup) == []
    assert setup.host.serve[1338] == MINE, "published on the tailnet again"
    setup.service().uninstall()
    assert setup.snapshot() == before and setup.files() == []


def test_a_password_is_kept_only_for_the_public_dashboard(setup: Setup) -> None:
    setup.service().init(public=False)
    assert setup.service().wants_password(False) is False
    assert not setup.layout.paths.dashboard_key.exists()


def test_uninstall_closes_the_dashboard_port_the_helper_opened(setup: Setup) -> None:
    before = setup.snapshot()
    setup.service().init(public=True, password=GOOD)
    _helper_opened_dashboard(setup)
    setup.service().uninstall()
    assert ["ufw", "delete", "allow", "1338/tcp"] in setup.host.calls
    assert setup.snapshot() == before and setup.files() == []


def test_uninstall_leaves_a_dashboard_rule_it_did_not_add(setup: Setup) -> None:
    setup.host.rules.add("1338/tcp")  # added by hand, never by the helper
    setup.service().init(public=True, password=GOOD)
    setup.service().uninstall()
    assert "1338/tcp" in setup.host.rules


def test_dash_pass_sets_a_new_password_and_logs_everyone_out(setup: Setup) -> None:
    from vpstream import login

    setup.service().init(public=True, password=GOOD)
    paths = setup.layout.paths
    old = paths.dashboard_key.read_text()
    paths.login_record.write_text('{"addresses": {}, "all": [1]}')
    paths.device_key.write_bytes(b"k" * 32)
    setup.host.calls.clear()
    setup.service().set_password("another good password")
    assert paths.dashboard_key.read_text() != old
    verifier = login.load_verifier(paths.dashboard_key)
    assert verifier and verifier.key == login.derive_key(
        "another good password", verifier.salt, verifier.iterations
    )
    assert paths.dashboard_key.stat().st_mode & 0o777 == 0o640
    assert not paths.login_record.exists(), "the old password's failures go with it"
    assert not paths.device_key.exists(), "and every browser it knew"
    # The daemon takes it over its socket: no restart, so a running stream goes on.
    assert setup.host.ran("systemctl") == [] and setup.asked == ["dashboard/reload"]
    assert any("a stream goes on" in line for line in setup.said)


def test_dash_pass_refuses_on_the_tailnet_and_without_root(setup: Setup) -> None:
    setup.service().init()
    with pytest.raises(service.ServiceError, match="not public"):
        setup.service().set_password(GOOD)
    assert not setup.layout.paths.dashboard_key.exists()
    with pytest.raises(service.ServiceError, match="needs root"):
        setup.service(euid=1000).set_password(GOOD)


def test_a_public_dashboard_port_busy_with_our_own_daemon_is_fine(setup: Setup) -> None:
    setup.service().init(public=True, password=GOOD)
    setup.service(port_busy=lambda port: True).init()
    setup.host.active = False
    with pytest.raises(service.ServiceError, match="already used by another program"):
        setup.service(port_busy=lambda port: True).init()


def test_dash_pass_with_the_daemon_down_says_when_it_takes_effect(setup: Setup) -> None:
    from vpstream import client

    setup.service().init(public=True, password=GOOD)

    def down(socket: Path, route: str) -> None:
        raise client.DaemonUnavailable("the vpstream daemon is not running")

    setup.service(ask_daemon=down).set_password("another good password")
    assert any("when it starts" in line for line in setup.said)


@pytest.mark.parametrize("text", ["garbage", ""])
def test_init_asks_again_for_a_password_file_that_cannot_be_used(setup: Setup, text: str) -> None:
    setup.service().init(public=True, password=GOOD)
    setup.layout.paths.dashboard_key.write_text(text)
    svc = setup.service()
    assert svc.wants_password() is True
    with pytest.raises(service.ServiceError, match="cannot be used"):
        svc.init()
    svc.init(password="a new good password")
    assert svc.wants_password() is False


def test_back_on_the_tailnet_the_known_devices_go_too(setup: Setup) -> None:
    setup.service().init(public=True, password=GOOD)
    setup.layout.paths.device_key.write_bytes(b"k" * 32)
    setup.service().init(public=False)
    assert not setup.layout.paths.device_key.exists()


# --- HTTPS for streams

# Let's Encrypt certifies public addresses only, and the tests use documentation ones: a test
# authority, written before init, as an owner trying things out would.
# The rule as the fake ufw keeps it: IPv4 only.
PORT_80 = "proto tcp from 0.0.0.0/0 to any port 80"
TEST_CA = '[acme]\nca = "https://ca.test/dir"\n'


def _https_ready(setup: Setup, **changes: object) -> service.Service:
    config_file = setup.layout.paths.config_file
    config_file.parent.mkdir(parents=True, exist_ok=True)
    if not config_file.exists():
        config_file.write_text(TEST_CA)
    return setup.service(**changes)


def test_lets_encrypt_needs_a_public_address(setup: Setup) -> None:
    with pytest.raises(service.ServiceError, match=r"203\.0\.113\.5 is not a public address"):
        setup.service().init(https=True)
    assert setup.files() == [], "refused before anything changed"


def _acme_opened(setup: Setup) -> None:
    """What the daemon's `acme-open` leaves when it is cut off: the rule, and its record."""
    setup.host.rules.add(PORT_80)
    record = setup.layout.paths.open_acme_ports_file
    record.write_text("80\n")
    record.chmod(0o644)


def _keep_certificate(setup: Setup) -> Path:
    from vpstream.tests.fakes import store_certificate

    store = setup.layout.paths.certificate_dir / "data" / "caddy"
    return store_certificate(store, config.LETS_ENCRYPT, "203.0.113.5", 0.0, 1.0)


def test_init_https_switches_streams_to_https_and_says_what_to_open(setup: Setup) -> None:
    _https_ready(setup).init(https=True)
    paths = setup.layout.paths
    assert config.load(paths.config_file).stream_https is True
    assert "CAP_NET_BIND_SERVICE" in setup.layout.unit_file.read_text()
    said = "\n".join(setup.said)
    assert "streams are now served over HTTPS" in said
    assert "https://203.0.113.5:1337/<alias>" in said
    assert "80/tcp" in said and "firewall of its own" in said
    assert not any(c[:2] == ["ufw", "allow"] for c in setup.host.calls), "init opens nothing"
    # Run again, it stays as it is; and the dashboard is not moved by it.
    setup.service().init()
    assert config.load(paths.config_file).stream_https is True
    assert config.load(paths.config_file).dashboard_public is False


def test_init_never_removes_the_certificate_and_uninstall_does(setup: Setup) -> None:
    before = setup.snapshot()
    _https_ready(setup).init(https=True)
    kept = _keep_certificate(setup)
    setup.service().init()  # an upgrade
    setup.service().init(https=False)
    _https_ready(setup).init(https=True)
    assert (kept / "203.0.113.5.crt").is_file()
    setup.service().uninstall()
    assert not kept.exists() and setup.files() == []
    assert setup.snapshot() == before


def test_no_https_closes_port_80_if_a_request_left_it_open(setup: Setup) -> None:
    before = setup.snapshot()
    _https_ready(setup).init(https=True)
    _acme_opened(setup)
    setup.service().init(https=False)
    assert config.load(setup.layout.paths.config_file).stream_https is False
    assert PORT_80 not in setup.host.rules
    assert not setup.layout.paths.open_acme_ports_file.exists()
    assert "CAP_NET_BIND_SERVICE" not in setup.layout.unit_file.read_text()
    setup.service().uninstall()
    assert setup.snapshot() == before


def test_uninstall_closes_port_80_the_helper_opened_and_leaves_one_it_did_not(
    setup: Setup,
) -> None:
    _https_ready(setup).init(https=True)
    _acme_opened(setup)
    setup.service().uninstall()
    assert PORT_80 not in setup.host.rules
    setup.host.rules.add(PORT_80)  # a web server's, by hand
    _https_ready(setup).init(https=True)
    setup.service().uninstall()
    assert PORT_80 in setup.host.rules


def test_https_needs_a_caddy_with_acme_profiles(setup: Setup) -> None:
    setup.host.caddy = "2.6.2\n"
    with pytest.raises(service.ServiceError, match=r"Caddy 2\.6\.2 cannot.*2\.10 or newer"):
        _https_ready(setup).init(https=True)
    assert setup.files() == ["etc/vpstream/config.toml"], "refused before anything changed"
    setup.service().init()  # plain HTTP works with it
    setup.host.available.discard("caddy")
    with pytest.raises(service.ServiceError, match="missing on this host: caddy"):
        _https_ready(setup).init(https=True)


@pytest.mark.parametrize("found", ["", "2001:db8::5"])
def test_https_needs_the_public_ipv4_address(setup: Setup, found: str) -> None:
    with pytest.raises(service.ServiceError, match="public IPv4 address"):
        _https_ready(setup, outgoing_address=lambda: found).init(https=True)
    paths = setup.layout.paths
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text('[stream]\nhost = "media.example.org"\n' + TEST_CA)
    with pytest.raises(service.ServiceError, match=r"media\.example\.org is not an IPv4 address"):
        _https_ready(setup).init(https=True)
    paths.config_file.write_text('[stream]\nhost = "203.0.113.9"\n' + TEST_CA)
    _https_ready(setup, outgoing_address=lambda: found).init(https=True)
    assert any("https://203.0.113.9:1337/" in line for line in setup.said)


def test_https_and_the_public_dashboard_go_together(setup: Setup) -> None:
    _https_ready(setup).init(public=True, password=GOOD, https=True)
    cfg = config.load(setup.layout.paths.config_file)
    assert cfg.dashboard_public is True and cfg.stream_https is True


@pytest.mark.parametrize("theirs", ["80/tcp", PORT_80])
def test_a_port_80_rule_that_was_there_before_vpstream_stays(setup: Setup, theirs: str) -> None:
    """Opened by hand before vpstream came (a web server's `ufw allow 80/tcp`, or a rule just
    like vpstream's): requests, --no-https and uninstall all leave it."""
    from vpstream import firewall

    setup.host.rules.add(theirs)
    _https_ready(setup).init(https=True)
    paths = setup.layout.paths
    helper = dict(
        config_file=paths.config_file,
        record_file=paths.open_ports_file,
        dashboard_record_file=paths.open_dashboard_ports_file,
        acme_record_file=paths.open_acme_ports_file,
        euid=0,
        ufw="ufw",
        owner_uid=os.getuid(),
    )
    for action in ("acme-open", "acme-close", "acme-open"):  # a request, then one cut off
        assert firewall.helper([action], setup.host, **helper)[0] == 0  # type: ignore[arg-type]
    setup.service().init(https=False)
    assert theirs in setup.host.rules
    assert PORT_80 not in setup.host.rules or theirs == PORT_80, "only ours is taken out"
    setup.service().uninstall()
    assert theirs in setup.host.rules
