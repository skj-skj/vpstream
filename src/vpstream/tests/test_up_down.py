"""Termux: `vpstream up` and `down`, and a Caddy a killed daemon left behind."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from vpstream import caddy, cli, client, config
from vpstream.platform.termux import commands
from vpstream.tests.fakes import FakeRunner, free_port
from vpstream.tests.test_daemon import Running


def _paths(tmp_path: Path) -> config.Paths:
    return config.Paths.under(tmp_path)


class Proc:
    def __init__(self, code: int | None = None) -> None:
        self.code = code

    def poll(self) -> int | None:
        return self.code


def test_up_starts_the_daemon_apart_and_keeps_the_phone_awake(
    on_termux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([None, None, {"pid": 7}, {"pid": 7, "dashboard": {}}])
    monkeypatch.setattr(commands, "_answers", lambda paths: next(answers))
    spawned: list[tuple[list[str], Path]] = []
    runner = FakeRunner(("termux-wake-lock",))
    said: list[str] = []
    info = commands.up(
        runner,
        _paths(tmp_path),
        said.append,
        spawn=lambda argv, log: spawned.append((argv, log)) or Proc(),
        sleep=lambda s: None,
    )
    assert info["pid"] == 7
    argv, log = spawned[0]
    assert argv == [sys.executable, "-P", "-m", "vpstream", "daemon"]
    assert log == _paths(tmp_path).state_dir / "daemon.log"
    assert ["termux-wake-lock"] in runner.calls
    assert "the daemon is running (pid 7)" in said[-1]


def test_up_says_why_the_daemon_stopped(
    on_termux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "_answers", lambda paths: None)
    log = _paths(tmp_path).state_dir / "daemon.log"

    def spawn(argv: list[str], log: Path) -> Proc:
        log.write_text("error: port 1338 is taken\n")
        return Proc(1)

    with pytest.raises(commands.CommandError, match="stopped at once:\nerror: port 1338"):
        commands.up(FakeRunner(()), _paths(tmp_path), spawn=spawn, sleep=lambda s: None)
    assert log.exists()


def test_up_when_it_runs_already(on_termux: None, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(commands, "_answers", lambda paths: {"pid": 3})
    said: list[str] = []
    commands.up(FakeRunner(()), _paths(tmp_path), said.append, spawn=lambda *a: 1 / 0)
    assert said == ["the daemon is already running (pid 3)"]


def test_up_moves_a_long_log_aside(on_termux: None, tmp_path: Path, monkeypatch: Any) -> None:
    answers = iter([None, {"pid": 1}, {"pid": 1}])
    monkeypatch.setattr(commands, "_answers", lambda paths: next(answers))
    log = _paths(tmp_path).state_dir / "daemon.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * (commands.LOG_KEEP_BYTES + 1))
    commands.up(FakeRunner(()), _paths(tmp_path), spawn=lambda *a: Proc(), sleep=lambda s: None)
    assert log.with_name("daemon.log.1").stat().st_size == commands.LOG_KEEP_BYTES + 1


def test_down_without_a_daemon_still_lets_the_phone_sleep(
    on_termux: None, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(commands, "_answers", lambda paths: None)
    runner, said = FakeRunner(("termux-wake-unlock",)), []
    commands.down(runner, _paths(tmp_path), said.append)
    assert said == ["the daemon is not running"] and ["termux-wake-unlock"] in runner.calls


def test_up_and_down_are_for_termux(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["up"]) == 1
    assert "sudo systemctl start vpstream" in capsys.readouterr().err
    assert cli.main(["down"]) == 1
    assert "sudo systemctl stop vpstream" in capsys.readouterr().err


def test_down_stops_a_real_daemon(on_termux: None, short_tmp: Path) -> None:
    cfg = config.Config(local=True, stream_port=free_port(), dashboard_port=free_port())
    run = Running(short_tmp, FakeRunner(()), cfg).start()
    said: list[str] = []
    commands.down(FakeRunner(()), run.paths, said.append, sleep=time.sleep)
    run.thread.join(10)
    assert not run.thread.is_alive() and said == ["the daemon is stopped"]
    assert "asked to stop (vpstream down)" in run.log


def test_linux_has_no_stop_route(short_tmp: Path) -> None:
    run = Running(short_tmp, FakeRunner(()), config.Config(dashboard_port=free_port())).start()
    try:
        with pytest.raises(client.DaemonRefused):
            client.call(run.paths.socket, "POST", "daemon/stop", {})
        assert run.thread.is_alive()
    finally:
        run.close()


# --- the Caddy a killed daemon left


def _left_over(work: Path, argv_path: Path) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", str(argv_path)],
    )
    work.mkdir(parents=True, exist_ok=True)
    (work / "caddy.pid").write_text(f"{proc.pid}\n")
    return proc


def test_a_left_over_caddy_of_this_install_is_stopped(on_termux: None, tmp_path: Path) -> None:
    server = caddy.CaddyServer(FakeRunner(), free_port(), _paths(tmp_path))
    assert server.reap
    proc = _left_over(server.work_dir, server.config_file)
    try:
        server._stop_left_over()
        assert proc.wait(10) is not None
        assert not server.pid_file.exists()
    finally:
        proc.kill()


def test_another_process_with_the_number_is_left_alone(on_termux: None, tmp_path: Path) -> None:
    server = caddy.CaddyServer(FakeRunner(), free_port(), _paths(tmp_path))
    proc = _left_over(server.work_dir, tmp_path / "something else")
    try:
        server._stop_left_over()
        assert proc.poll() is None  # still running
        assert not server.pid_file.exists()
    finally:
        proc.kill()
        proc.wait()


def test_linux_writes_no_pid_file(tmp_path: Path) -> None:
    assert not caddy.CaddyServer(FakeRunner(), free_port(), _paths(tmp_path)).reap
