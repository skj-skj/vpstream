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
    text = service.unit_text(Path("/opt/v/bin/vpstream"), Path("/opt/v/bin/vpstream-fw"))
    assert "User=vpstream\nGroup=vpstream\n" in text
    assert "RuntimeDirectory=vpstream\nRuntimeDirectoryMode=0750\n" in text
    assert "ExecStart=/opt/v/bin/vpstream daemon\n" in text
    # `+`: closing the port after a stop or crash runs as root and does not depend on sudo.
    assert "ExecStopPost=+/opt/v/bin/vpstream-fw close\n" in text
    assert "After=network-online.target tailscaled.service\n" in text
    assert "WantedBy=multi-user.target\n" in text
    assert "NoNewPrivileges" not in text  # it would stop the daemon's sudo call


def test_sudoers_rule_allows_exactly_the_helper_actions() -> None:
    lines = service.sudoers_text(Path("/opt/v/bin/vpstream-fw")).splitlines()
    assert lines[0].startswith("#")
    helper = "/opt/v/bin/vpstream-fw"
    assert lines[1:] == [
        f"vpstream ALL=(root) NOPASSWD: {helper} open, {helper} close, "
        f"{helper} dash-open, {helper} dash-close"
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


# --- found in the review before milestone 2


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

    cert = caddy.dashboard_root_cert(setup.layout.paths)
    cert.parent.mkdir(parents=True)
    pem = (Path(__file__).parent / "data" / "caddy-root.crt").read_text()
    cert.write_text(pem)
    setup.service().init(public=True, password=GOOD)
    said = "\n".join(setup.said)
    assert caddy.certificate_fingerprint(pem) in said
    assert "vpstream dashboard-cert" in said


def test_init_says_so_when_the_authority_is_not_made_yet(setup: Setup) -> None:
    waited: list[float] = []
    setup.service(pause=waited.append).init(public=True, password=GOOD)
    assert waited.count(1.0) == service.CERT_SECONDS, "a second at a time, then it gives up"
    assert any("not made yet" in line for line in setup.said)


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
