from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from vpstream import firewall, state
from vpstream.tests.fakes import FakeRunner

UFW = "/usr/sbin/ufw"
ME = os.getuid()


def _helper(argv: list[str], runner: FakeRunner, config_file: Path, **kwargs: object) -> tuple:
    options = {
        "config_file": config_file,
        "record_file": config_file.parent / "open-ports",
        "euid": 0,
        "ufw": UFW,
        "owner_uid": ME,
        **kwargs,
    }
    return firewall.helper(argv, runner, **options)  # type: ignore[arg-type]


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("[stream]\nport = 2000\n")
    path.chmod(0o644)
    return path


def test_ufw_arguments() -> None:
    assert firewall.ufw_args("open", 1337) == ["allow", "1337/tcp"]
    assert firewall.ufw_args("close", 1337) == ["delete", "allow", "1337/tcp"]
    with pytest.raises(ValueError, match="not an action"):
        firewall.ufw_args("reset", 1337)


def test_helper_opens_and_closes_only_the_configured_port(config_file: Path) -> None:
    runner = FakeRunner()
    runner.on(UFW, stdout="Rule added\nRule added (v6)\n")
    code, message = _helper(["open"], runner, config_file)
    assert (code, message) == (0, "stream port 2000/tcp, open: Rule added")
    assert _helper(["close"], runner, config_file)[0] == 0
    assert runner.calls == [[UFW, "allow", "2000/tcp"], [UFW, "delete", "allow", "2000/tcp"]]


def test_helper_uses_the_default_port_without_a_config_file(tmp_path: Path) -> None:
    runner = FakeRunner()
    assert _helper(["open"], runner, tmp_path / "nope.toml")[0] == 0
    assert runner.calls == [[UFW, "allow", "1337/tcp"]]


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["open", "22"],
        ["open", "22/tcp"],
        ["close", "--port=22"],
        ["status"],
        ["enable"],
        ["disable"],
        ["reset"],
        ["reload"],
        ["allow"],
        ["OPEN"],
        ["open "],
        ["open;reboot"],
    ],
)
def test_helper_refuses_everything_but_open_and_close(argv: list[str], config_file: Path) -> None:
    runner = FakeRunner()
    code, message = _helper(argv, runner, config_file)
    assert code == 2 and "usage" in message
    assert runner.calls == []


def test_helper_must_be_root(config_file: Path) -> None:
    runner = FakeRunner()
    code, message = _helper(["open"], runner, config_file, euid=1000)
    assert code == 1 and "must run as root" in message
    assert runner.calls == []


def test_helper_does_not_trust_a_config_others_can_write(config_file: Path) -> None:
    runner = FakeRunner()
    config_file.chmod(0o666)
    assert "writable by users other than root" in _helper(["open"], runner, config_file)[1]
    config_file.chmod(0o664)
    assert _helper(["open"], runner, config_file)[0] == 1
    config_file.chmod(0o644)
    code, message = _helper(["open"], runner, config_file, owner_uid=ME + 1)
    assert code == 1 and "not owned by root" in message
    assert runner.calls == []


def test_helper_refuses_a_bad_config(config_file: Path) -> None:
    runner = FakeRunner()
    config_file.write_text("[stream]\nport = 22\n")
    code, message = _helper(["open"], runner, config_file)
    assert code == 1 and "from 1024 to 65535" in message
    assert runner.calls == []


def test_helper_reports_a_ufw_failure_and_a_missing_ufw(config_file: Path) -> None:
    runner = FakeRunner()
    runner.on(UFW, returncode=1, stdout="ERROR: problem running\n")
    assert _helper(["close"], runner, config_file) == (1, "ufw failed: ERROR: problem running")
    assert _helper(["close"], runner, config_file, ufw=None) == (1, "ufw was not found")


def test_ufw_is_found_in_system_folders_only(tmp_path: Path) -> None:
    assert firewall.find_ufw([str(tmp_path)]) is None
    ufw = tmp_path / "ufw"
    ufw.write_text("#!/bin/sh\n")
    assert firewall.find_ufw([str(tmp_path)]) is None  # not executable
    ufw.chmod(0o755)
    assert firewall.find_ufw(["/nonexistent", str(tmp_path)]) == str(ufw)
    assert all(folder.startswith("/") for folder in firewall.UFW_DIRS)


def test_main_ignores_the_test_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("VPSTREAM_ROOT", str(tmp_path))
    assert firewall.main(["open", "22"]) == 2
    assert "usage: vpstream-fw open|close" in capsys.readouterr().err
    if os.geteuid() != 0:
        assert firewall.main(["open"]) == 1
        assert "must run as root" in capsys.readouterr().err


def test_daemon_side_calls_the_helper_through_sudo() -> None:
    runner = FakeRunner()
    fw = firewall.SudoFirewall(runner, Path("/opt/x/bin/vpstream-fw"))
    fw.open()
    fw.close()
    assert runner.calls == [
        ["sudo", "-n", "/opt/x/bin/vpstream-fw", "open"],
        ["sudo", "-n", "/opt/x/bin/vpstream-fw", "close"],
    ]
    runner.on("sudo", returncode=1, stderr="sudo: a password is required\n")
    with pytest.raises(firewall.FirewallError, match=r"could not close.*password is required"):
        fw.close()


def test_helper_path_is_next_to_python() -> None:
    assert firewall.helper_path().name == "vpstream-fw"
    assert firewall.helper_path().parent == Path(sys.executable).parent


# --- the state file


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "lib" / "state.json"
    assert state.load(path) == (state.State(), None)
    state.save(path, state.State(stream={"alias": "aaa"}))
    assert state.load(path) == (state.State(stream={"alias": "aaa"}), None)
    state.save(path, state.State())
    assert state.load(path) == (state.State(), None)
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


@pytest.mark.parametrize("text", ["", "{", "[]", '{"stream": 5}', '{"stream": "yes"}'])
def test_an_unusable_state_file_means_no_stream(tmp_path: Path, text: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(text)
    loaded, problem = state.load(path)
    assert loaded.stream is None and problem and str(path) in problem


# --- found in the review before milestone 2: what was opened is what gets closed


class Ufw(FakeRunner):
    """Remembers its rules, like the real ufw."""

    def __init__(self) -> None:
        super().__init__(())
        self.rules: set[str] = set()

    def run(self, args, *, timeout=None):  # type: ignore[no-untyped-def]
        if args[1] == "allow":
            self.rules.add(args[2])
        else:
            self.rules.discard(args[3])
        return super().run(args, timeout=timeout)


def test_open_writes_the_port_down_and_close_forgets_it(config_file: Path) -> None:
    ufw, record = Ufw(), config_file.parent / "open-ports"
    assert _helper(["open"], ufw, config_file)[0] == 0
    assert firewall.recorded_ports(record, ME) == [2000]
    assert record.stat().st_mode & 0o777 == 0o644
    assert _helper(["open"], ufw, config_file)[0] == 0  # twice is once
    assert firewall.recorded_ports(record, ME) == [2000]
    assert _helper(["close"], ufw, config_file)[0] == 0
    assert ufw.rules == set() and not record.exists()


def test_close_after_the_port_was_changed_closes_the_old_port_too(config_file: Path) -> None:
    ufw = Ufw()
    _helper(["open"], ufw, config_file)
    config_file.write_text("[stream]\nport = 2001\n")
    code, message = _helper(["close"], ufw, config_file)
    assert code == 0 and "also closed 2000/tcp" in message
    assert ufw.rules == set()
    assert ufw.calls[-2:] == [
        [UFW, "delete", "allow", "2000/tcp"],
        [UFW, "delete", "allow", "2001/tcp"],
    ]


def test_close_with_a_broken_config_still_closes_what_was_opened(config_file: Path) -> None:
    ufw = Ufw()
    _helper(["open"], ufw, config_file)
    config_file.write_text("[stream]\nprot = 2000\n")
    code, message = _helper(["close"], ufw, config_file)
    assert ufw.rules == set()
    assert code == 1 and "closed 2000/tcp" in message and "unknown setting" in message
    assert _helper(["open"], ufw, config_file)[0] == 1 and ufw.rules == set()  # never opens blind


def test_a_failed_close_keeps_the_record_for_the_next_try(config_file: Path) -> None:
    ufw = Ufw()
    _helper(["open"], ufw, config_file)
    ufw.on(UFW, returncode=1, stdout="ERROR: could not lock\n")
    assert _helper(["close"], ufw, config_file)[0] == 1
    assert firewall.recorded_ports(config_file.parent / "open-ports", ME) == [2000]


def test_the_record_is_trusted_no_more_than_the_config(tmp_path: Path) -> None:
    record = tmp_path / "open-ports"
    record.write_text("2000\n22\n2000\n99999\nx\n3000\n")
    record.chmod(0o644)
    assert firewall.recorded_ports(record, ME) == [2000, 3000]  # only ports the config allows
    assert firewall.recorded_ports(record, ME + 1) == []  # not root's
    record.chmod(0o666)
    assert firewall.recorded_ports(record, ME) == []
    assert firewall.recorded_ports(tmp_path / "nope", ME) == []


def test_a_port_is_not_opened_when_it_cannot_be_written_down(config_file: Path) -> None:
    ufw = Ufw()
    blocked = config_file.parent / "a-file" / "open-ports"
    blocked.parent.write_text("")
    code, message = _helper(["open"], ufw, config_file, record_file=blocked)
    assert code == 1 and "stays closed" in message and ufw.calls == []


def test_a_sudo_that_cannot_be_run_is_a_firewall_error() -> None:
    class NoSudo(FakeRunner):
        def run(self, args, *, timeout=None):  # type: ignore[no-untyped-def]
            raise FileNotFoundError(2, "No such file or directory", "sudo")

    fw = firewall.SudoFirewall(NoSudo(), Path("/opt/x/bin/vpstream-fw"))
    with pytest.raises(firewall.FirewallError, match="could not close the stream port"):
        fw.close()
