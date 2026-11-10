from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from vpstream import cli, config, service, tailscale
from vpstream.tools import Result

NAME = "host.example.ts.net"
OTHERS = {443: "http://127.0.0.1:2283", 8443: "http://127.0.0.1:3000"}  # not vpstream's
MINE = "http://127.0.0.1:1338"


class FakeHost:
    """A host that remembers its users, groups and ufw rules, like the real commands do."""

    def __init__(self) -> None:
        self.available = set(service.NEEDED)
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
            status = {"Self": {"DNSName": NAME + "."}, "CertDomains": [NAME] if self.https else []}
            return Result(0, json.dumps(status), "")
        if cmd[1:] == ["serve", "status", "--json"]:
            web = {f"{NAME}:{p}": {"Handlers": {"/": {"Proxy": t}}} for p, t in self.serve.items()}
            tcp = {str(p): {"HTTPS": True} for p in self.serve}
            return Result(0, json.dumps({"TCP": tcp, "Web": web} if tcp else {}), "")
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
        bin_dir = Path("/opt/pipx/venvs/vpstream/bin")
        self.kwargs = {
            "layout": self.layout,
            "euid": 0,
            "sudo_user": "alice",
            "bin_dir": bin_dir,
            "install_problem": lambda: None,
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


def test_sudoers_rule_allows_exactly_open_and_close() -> None:
    lines = service.sudoers_text(Path("/opt/v/bin/vpstream-fw")).splitlines()
    assert lines[0].startswith("#")
    assert lines[1:] == [
        "vpstream ALL=(root) NOPASSWD: /opt/v/bin/vpstream-fw open, /opt/v/bin/vpstream-fw close"
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
    import os

    if os.geteuid() == 0:
        pytest.skip("runs as root")
    assert cli.main(["init"]) == 1
    assert "sudo vpstream init" in capsys.readouterr().err
    assert cli.main(["uninstall", "--yes"]) == 1
    assert "sudo vpstream uninstall" in capsys.readouterr().err
