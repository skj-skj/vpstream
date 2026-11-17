"""`sudo vpstream init --local` on Linux: the local network only, set in the unit file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpstream import cli, config, doctor, local, login
from vpstream.platform.linux import service
from vpstream.tests import test_service
from vpstream.tests.fakes import FakeRunner
from vpstream.tests.test_service import MINE, OTHERS, Setup

PASSWORD = "a long enough password"
LAN = "192.168.1.20"
RULE = "proto tcp from 192.168.0.0/16 to any port 1338"
BIN = "/opt/pipx/venvs/vpstream/bin/vpstream"
setup = test_service.setup  # the same host, users and files


def _local(setup: Setup, address: str = LAN) -> service.Service:
    def find(configured: str) -> str:
        found = configured or address
        if problem := local.private_problem(found):
            raise local.LocalError(problem)
        return found

    return setup.service(find_local=find)


def test_unit_text_local() -> None:
    args = (Path(BIN), Path("/opt/v/bin/vpstream-fw"))
    assert f"ExecStart={BIN} daemon\n" in service.unit_text(*args)
    assert f"ExecStart={BIN} daemon --local\n" in service.unit_text(*args, local="")
    assert f"ExecStart={BIN} daemon --local --address {LAN}\n" in service.unit_text(
        *args, local=LAN
    )


def test_init_local(setup: Setup) -> None:
    _local(setup).init(password=PASSWORD, lan=True)
    host, lay = setup.host, setup.layout
    assert f"ExecStart={BIN} daemon --local\n" in lay.unit_file.read_text()
    assert RULE in host.rules
    assert ["ufw", "allow", *RULE.split()] in host.calls
    assert host.ran("tailscale", "serve", "--bg") == []  # no tailnet dashboard
    assert host.serve == OTHERS
    # The configuration is as plain init writes it: local mode is the unit's, not the file's.
    assert config.load(lay.paths.config_file) == config.Config()
    assert "[network]" not in lay.paths.config_file.read_text()
    assert login.load_verifier(lay.paths.dashboard_key) is not None
    assert json.loads(lay.manifest.read_text())["lan_rule"] == [1338, "192.168.0.0/16"]
    said = "\n".join(setup.said)
    assert f"http://{LAN}:1338/ (password)" in said and local.WARNING in said


def test_init_local_with_an_address(setup: Setup) -> None:
    _local(setup).init(password=PASSWORD, lan=True, address="10.0.0.5")
    assert "daemon --local --address 10.0.0.5\n" in setup.layout.unit_file.read_text()
    assert "proto tcp from 10.0.0.0/8 to any port 1338" in setup.host.rules


def test_init_local_needs_no_tailscale(setup: Setup) -> None:
    setup.host.available.discard("tailscale")
    _local(setup).init(password=PASSWORD, lan=True)
    assert setup.host.ran("tailscale") == []


@pytest.mark.parametrize(
    ("kwargs", "problem"),
    [
        ({}, "the dashboard on the local network needs a password"),
        ({"password": PASSWORD, "address": "100.64.1.2"}, "carrier's shared address"),
        ({"password": PASSWORD, "address": "203.0.113.5"}, "not a private address"),
        ({"password": PASSWORD, "https": True}, "local mode serves plain HTTP"),
        ({"password": PASSWORD, "public": True}, "no public dashboard"),
    ],
)
def test_init_local_refuses_before_it_changes_anything(
    setup: Setup, kwargs: dict[str, object], problem: str
) -> None:
    before = setup.snapshot()
    with pytest.raises(service.ServiceError, match=problem):
        _local(setup).init(lan=True, **kwargs)  # type: ignore[arg-type]
    assert setup.snapshot() == before and setup.files() == []


def test_plain_init_after_local_is_the_public_mode_again(setup: Setup) -> None:
    _local(setup).init(password=PASSWORD, lan=True)
    setup.service().init()
    host, lay = setup.host, setup.layout
    assert RULE not in host.rules
    assert f"ExecStart={BIN} daemon\n" in lay.unit_file.read_text()
    assert host.serve == {**OTHERS, 1338: MINE}  # on the tailnet, as plain init always did
    assert not lay.paths.dashboard_key.exists()
    assert "lan_rule" not in json.loads(lay.manifest.read_text())


def test_plain_init_writes_the_manifest_as_before(setup: Setup) -> None:
    setup.service().init()
    assert set(json.loads(setup.layout.manifest.read_text())) == {
        "user_created",
        "members_added",
        "serve_port",
    }


def test_init_local_again_and_on_another_network(setup: Setup) -> None:
    _local(setup).init(password=PASSWORD, lan=True)
    setup.host.calls.clear()
    _local(setup).init(lan=True)  # the password is there: none needed
    assert setup.host.ran("ufw") == []
    _local(setup, "10.1.2.3").init(lan=True)
    assert RULE not in setup.host.rules
    assert "proto tcp from 10.0.0.0/8 to any port 1338" in setup.host.rules


def test_uninstall_takes_the_rule_back(setup: Setup) -> None:
    before = setup.snapshot()
    _local(setup).init(password=PASSWORD, lan=True)
    setup.service().uninstall()
    assert setup.snapshot() == before and setup.files() == []


def test_dash_pass_works_in_local_mode(setup: Setup) -> None:
    _local(setup).init(password=PASSWORD, lan=True)
    setup.service().set_password("another long password")
    assert setup.asked == ["dashboard/reload"]


def test_cli_init_local(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, object]] = []

    class Svc:
        def dashboard_public(self, public: bool | None = None) -> bool:
            return bool(public)

        def wants_password(self, public: bool | None = None, lan: bool = False) -> bool:
            return False

        def init(self, *args: object, **kwargs: object) -> None:
            calls.append({"args": args, **kwargs})

    monkeypatch.setattr(cli, "_service", lambda: Svc())
    assert cli.main(["init", "--local", "--address", LAN]) == 0
    assert calls[-1] == {"args": (False, None, None), "lan": True, "address": LAN}
    assert cli.main(["init", "--address", LAN]) == 2
    assert "--address goes with --local" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["init", "--local", "--no-tailscale"])


def test_doctor_on_a_local_install(setup: Setup, tmp_path: Path) -> None:
    _local(setup).init(password=PASSWORD, lan=True)
    runner = FakeRunner(("ffmpeg", "ffprobe", "caddy", "ufw", "systemctl", "sudo"))
    checks = {c.name: c for c in doctor.run_checks(runner, paths=setup.layout.paths)}
    assert checks["tailscale"].status == "warning"
    assert "on the local network" in checks["tailscale"].detail
    assert checks["local mode"].detail == "init --local: 1338/tcp from 192.168.0.0/16"
    assert "dashboard password" in checks
    assert "tailscale up" not in checks and "dashboard" not in checks


@pytest.mark.parametrize(
    "given", ["192.168.1.20%x", " 192.168.1.20", "192.168.1.20%\nExecStartPre=/bin/true"]
)
def test_the_unit_gets_the_address_as_checked_never_as_given(setup: Setup, given: str) -> None:
    def find(configured: str) -> str:
        return local.net.normal_ip(configured) or ""

    setup.service(find_local=find).init(password=PASSWORD, lan=True, address=given)
    unit = setup.layout.unit_file.read_text()
    assert f"ExecStart={BIN} daemon --local --address 192.168.1.20\n" in unit
    assert "%" not in unit and "ExecStartPre" not in unit
