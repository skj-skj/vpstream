from __future__ import annotations

import json
import socket
import stat
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from vpstream import __version__, api, cli, client, config, daemon
from vpstream.tests.fakes import FakeRunner


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Running:
    """A daemon serving in a background thread, stopped when the test ends."""

    def __init__(self, root: Path, runner: FakeRunner, cfg: config.Config) -> None:
        self.paths = config.Paths.under(root)
        self.log: list[str] = []
        self.daemon = daemon.Daemon(
            cfg, self.paths, runner, log=self.log.append, retry_seconds=0.05
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.daemon.serve, args=(self.stop,), daemon=True)

    def start(self) -> Running:
        self.thread.start()
        assert self.daemon.ready.wait(5)
        return self

    def wait_for_dashboard(self) -> str:
        for _ in range(100):
            if self.daemon.dashboard_address:
                return self.daemon.dashboard_address
            self.stop.wait(0.05)
        raise AssertionError(f"no dashboard: {self.log}")

    def close(self) -> None:
        self.stop.set()
        self.thread.join(5)
        assert not self.thread.is_alive()


@pytest.fixture
def tailnet() -> FakeRunner:
    """A host whose "Tailscale address" is the loopback address, so tests can bind it."""
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "ip", "-4", stdout="127.0.0.1\n")
    return runner


@pytest.fixture
def running(short_tmp: Path, tailnet: FakeRunner) -> Iterator[Running]:
    cfg = config.Config(stream_port=_free_port(), dashboard_port=_free_port())
    run = Running(short_tmp, tailnet, cfg).start()
    yield run
    run.close()


# --- the API


def test_status_route() -> None:
    app = api.create_app(lambda: {"version": "1.2.3", "stream": None})
    http = app.test_client()
    assert http.get("/api/v1/status").get_json() == {"version": "1.2.3", "stream": None}
    page = http.get("/").get_data(as_text=True)
    assert "vpstream 1.2.3" in page and "no stream is running" in page


def test_errors_are_json() -> None:
    http = api.create_app(dict).test_client()
    answer = http.get("/api/v1/nope")
    assert answer.status_code == 404 and "error" in answer.get_json()
    assert http.post("/api/v1/status").status_code == 405


def test_index_escapes_what_it_shows() -> None:
    app = api.create_app(lambda: {"version": "<b>x</b>"})
    assert "<b>x</b>" not in app.test_client().get("/").get_data(as_text=True)


# --- the socket and the CLI


def test_cli_talks_to_the_daemon_over_the_socket(running: Running) -> None:
    info = client.call(running.paths.socket, "GET", "status")
    assert info["version"] == __version__
    assert info["stream"] is None
    assert info["stream_port"] == running.daemon.config.stream_port


def test_socket_is_for_the_owner_and_group_only(running: Running) -> None:
    mode = running.paths.socket.stat().st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o660


def test_socket_file_is_removed_on_stop(short_tmp: Path, tailnet: FakeRunner) -> None:
    run = Running(short_tmp, tailnet, config.Config(dashboard_port=_free_port())).start()
    assert run.paths.socket.exists()
    run.close()
    assert not run.paths.socket.exists()
    assert run.log[-1] == "vpstream daemon stopped"


def test_a_leftover_socket_file_is_replaced(short_tmp: Path, tailnet: FakeRunner) -> None:
    paths = config.Paths.under(short_tmp)
    paths.run_dir.mkdir(parents=True)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(paths.socket))
    stale.close()  # the file stays, as after a crash
    run = Running(short_tmp, tailnet, config.Config(dashboard_port=_free_port())).start()
    try:
        assert client.call(paths.socket, "GET", "status")["pid"]
    finally:
        run.close()


def test_a_file_that_is_not_a_socket_is_not_removed(short_tmp: Path) -> None:
    path = short_tmp / "daemon.sock"
    path.write_text("mine")
    with pytest.raises(daemon.DaemonError, match="cannot listen"):
        daemon.bind_unix(path)
    assert path.read_text() == "mine"


def test_client_says_when_the_daemon_is_not_running(short_tmp: Path) -> None:
    with pytest.raises(client.DaemonUnavailable, match="not running"):
        client.call(short_tmp / "nope.sock", "GET", "status")


def test_client_says_when_the_user_lacks_the_group(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: socket.socket, address: str) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    with pytest.raises(client.DaemonUnavailable, match="vpstream group"):
        client.call(short_tmp / "daemon.sock", "GET", "status")


def test_client_reports_a_refusal(running: Running) -> None:
    with pytest.raises(client.DaemonRefused, match="refused"):
        client.call(running.paths.socket, "GET", "nope")


def test_cli_status(
    running: Running, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    address = running.wait_for_dashboard()
    monkeypatch.setenv(config.ROOT_ENV, str(running.paths.run_dir.parent.parent))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"vpstream daemon {__version__}" in out
    assert "Stream:       none" in out
    assert f"Dashboard:    http://{address}/" in out
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dashboard"]["address"] == address


def test_cli_status_without_a_daemon(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(config.ROOT_ENV, str(short_tmp))
    assert cli.main(["status"]) == 1
    assert "not running" in capsys.readouterr().err


def test_status_notes_a_version_mismatch() -> None:
    info = {"version": "0.3.0", "uptime_seconds": 3725, "pid": 7, "stream_port": 1337}
    info["dashboard"] = {"address": None, "problem": "the dashboard is not available: x"}
    text = cli.format_status(info, "0.4.0")
    assert "up 1h 02m (pid 7)" in text
    assert "Dashboard:    the dashboard is not available: x" in text
    assert "sudo systemctl restart vpstream" in text
    assert "restart" not in cli.format_status(info, "0.3.0")


@pytest.mark.parametrize(
    ("seconds", "text"), [(5, "5s"), (192, "3m 12s"), (7500, "2h 05m"), (273600, "3d 4h")]
)
def test_duration(seconds: float, text: str) -> None:
    assert cli._duration(seconds) == text


# --- the dashboard port


def test_dashboard_listens_on_the_tailscale_address_only(running: Running) -> None:
    address = running.wait_for_dashboard()
    host, _, port = address.partition(":")
    assert host == "127.0.0.1" and int(port) == running.daemon.config.dashboard_port
    with urllib.request.urlopen(f"http://{address}/api/v1/status", timeout=5) as answer:
        assert json.load(answer)["dashboard"]["address"] == address
    assert "tailscale ip -4" in [" ".join(c) for c in running.daemon.runner.calls]  # type: ignore[attr-defined]


def test_dashboard_is_never_bound_to_every_interface(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch, tailnet: FakeRunner
) -> None:
    bound: list[str] = []
    real = daemon.bind_tcp

    def spy(host: str, port: int) -> socket.socket:
        bound.append(host)
        return real(host, port)

    monkeypatch.setattr(daemon, "bind_tcp", spy)
    run = Running(short_tmp, tailnet, config.Config(dashboard_port=_free_port())).start()
    try:
        run.wait_for_dashboard()
    finally:
        run.close()
    assert bound == ["127.0.0.1"]


def test_without_tailscale_the_socket_works_and_the_dashboard_waits(short_tmp: Path) -> None:
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "ip", "-4", returncode=1, stdout="Tailscale is stopped.\n")
    run = Running(short_tmp, runner, config.Config(dashboard_port=_free_port())).start()
    try:
        for _ in range(100):
            if "Tailscale is stopped" in run.daemon.dashboard_problem:
                break
            run.stop.wait(0.05)
        dash = client.call(run.paths.socket, "GET", "status")["dashboard"]
        assert dash["address"] is None and "Tailscale is stopped" in dash["problem"]
        # The same problem is logged once, not on every retry.
        run.stop.wait(0.3)
        assert sum("Tailscale is stopped" in line for line in run.log) == 1

        # Tailscale comes up: the dashboard starts without a restart.
        runner.on("tailscale", "ip", "-4", stdout="127.0.0.1\n")
        assert run.wait_for_dashboard().startswith("127.0.0.1:")
    finally:
        run.close()


def test_a_busy_dashboard_port_is_reported(short_tmp: Path, tailnet: FakeRunner) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        cfg = config.Config(dashboard_port=busy.getsockname()[1])
        run = Running(short_tmp, tailnet, cfg).start()
        try:
            for _ in range(100):
                if "not available" in run.daemon.dashboard_problem:
                    break
                run.stop.wait(0.05)
            assert "not available" in run.daemon.status()["dashboard"]["problem"]
        finally:
            run.close()
