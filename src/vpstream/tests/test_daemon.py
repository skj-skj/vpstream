from __future__ import annotations

import json
import re
import socket
import stat
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from vpstream import __version__, api, cli, client, config, daemon, state
from vpstream.tests.fakes import (
    NAME,
    FakeFirewall,
    FakeRunner,
    FakeServer,
    node_status,
    serve_status,
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Running:
    """A daemon serving in a background thread, stopped when the test ends."""

    def __init__(
        self,
        root: Path,
        runner: FakeRunner,
        cfg: config.Config,
        fw: FakeFirewall | None = None,
        server: FakeServer | None = None,
    ) -> None:
        self.paths = config.Paths.under(root)
        self.log: list[str] = []
        self.daemon = daemon.Daemon(
            cfg, self.paths, runner, fw, server, log=self.log.append, retry_seconds=0.05
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


def publish(runner: FakeRunner, port: int, target: str | None = None) -> None:
    """Make the fake Tailscale say that serve publishes `port` (to vpstream by default)."""
    serve = serve_status({port: target or f"http://127.0.0.1:{port}"})
    runner.on("tailscale", "serve", "status", "--json", stdout=json.dumps(serve))


@pytest.fixture
def tailnet() -> FakeRunner:
    """A host on a tailnet, with nothing published by `tailscale serve` yet."""
    runner = FakeRunner(("tailscale",))
    runner.on("tailscale", "status", "--json", stdout=json.dumps(node_status()))
    runner.on("tailscale", "serve", "status", "--json", stdout="{}")
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


def test_errors_are_json() -> None:
    http = api.create_app(dict).test_client()
    answer = http.get("/api/v1/nope")
    assert answer.status_code == 404 and "error" in answer.get_json()
    assert http.post("/api/v1/status").status_code == 405


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
    port = running.daemon.config.dashboard_port
    publish(running.daemon.runner, port)  # type: ignore[arg-type]
    monkeypatch.setenv(config.ROOT_ENV, str(running.paths.run_dir.parent.parent))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"vpstream daemon {__version__}" in out
    assert "Stream:       none" in out
    assert "Stream port:  " in out and "(unknown)" in out
    assert f"Dashboard:    https://{NAME}:{port}/ (Tailscale only)" in out
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
    info["dashboard"] = {
        "address": None,
        "url": None,
        "problem": "the dashboard is not available: x",
    }
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


def test_dashboard_listens_on_loopback_only(running: Running) -> None:
    address = running.wait_for_dashboard()
    assert address == f"127.0.0.1:{running.daemon.config.dashboard_port}"
    with urllib.request.urlopen(f"http://{address}/api/v1/status", timeout=5) as answer:
        assert json.load(answer)["dashboard"]["address"] == address


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


def _dash(root: Path, runner: FakeRunner) -> dict[str, object]:
    d = daemon.Daemon(config.Config(), config.Paths.under(root), runner, log=lambda m: None)
    d.dashboard_address = "127.0.0.1:1338"
    return d.status()["dashboard"]  # type: ignore[no-any-return]


def test_status_gives_the_tailnet_address_when_serve_publishes_it(
    tmp_path: Path, tailnet: FakeRunner
) -> None:
    publish(tailnet, 1338)
    dash = _dash(tmp_path, tailnet)
    assert dash["url"] == f"https://{NAME}:1338/" and dash["problem"] is None


def test_status_says_when_the_dashboard_is_not_published(
    tmp_path: Path, tailnet: FakeRunner
) -> None:
    dash = _dash(tmp_path, tailnet)
    assert dash["url"] is None and "sudo vpstream init" in str(dash["problem"])
    text = cli.format_status({"dashboard": dash, "version": "1"}, "1")
    assert "http://127.0.0.1:1338/ on this host only; not published" in text

    publish(tailnet, 1338, target="http://127.0.0.1:3000")  # the port serves something else
    assert _dash(tmp_path, tailnet)["url"] is None
    assert "tailscale was not found" in str(_dash(tmp_path, FakeRunner(()))["problem"])


def test_the_tailnet_address_is_remembered_between_status_requests(
    tmp_path: Path, tailnet: FakeRunner
) -> None:
    publish(tailnet, 1338)
    d = daemon.Daemon(config.Config(), config.Paths.under(tmp_path), tailnet, log=lambda m: None)
    d.dashboard_address = "127.0.0.1:1338"
    for _ in range(5):
        d.status()
    assert len(tailnet.calls) == 2  # one serve lookup and one name lookup


def test_a_busy_dashboard_port_is_reported_and_retried(
    short_tmp: Path, tailnet: FakeRunner
) -> None:
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    cfg = config.Config(dashboard_port=busy.getsockname()[1])
    run = Running(short_tmp, tailnet, cfg).start()
    try:
        for _ in range(100):
            if "not available" in run.daemon.dashboard_problem:
                break
            run.stop.wait(0.05)
        dash = client.call(run.paths.socket, "GET", "status")["dashboard"]
        assert dash["address"] is None and "not available" in dash["problem"]
        # The same problem is logged once, not on every retry.
        run.stop.wait(0.3)
        assert sum("not available" in line for line in run.log) == 1
        # The port becomes free: the dashboard starts without a restart.
        busy.close()
        assert run.wait_for_dashboard() == f"127.0.0.1:{cfg.dashboard_port}"
    finally:
        busy.close()
        run.close()


# --- the firewall when the daemon starts


def _daemon(root: Path, fw: FakeFirewall | None) -> tuple[daemon.Daemon, list[str]]:
    log: list[str] = []
    d = daemon.Daemon(config.Config(), config.Paths.under(root), FakeRunner(()), fw, log=log.append)
    return d, log


def test_start_closes_the_stream_port_when_no_stream_is_recorded(tmp_path: Path) -> None:
    fw = FakeFirewall()
    d, log = _daemon(tmp_path, fw)
    d.control.reconcile()
    assert fw.calls == ["close"]
    assert d.status()["stream_port_state"] == "closed"
    assert log == ["stream port 1337 is closed (no stream is running)"]


def test_start_closes_the_port_when_the_recorded_stream_is_not_usable(tmp_path: Path) -> None:
    fw = FakeFirewall()
    d, log = _daemon(tmp_path, fw)
    state.save(d.paths.state_dir / state.FILE_NAME, state.State(stream={"alias": "aaa"}))
    d.control.reconcile()
    assert fw.calls == ["close"]
    assert d.status()["stream"] is None
    assert "not usable" in log[0]
    # The record is dropped, so the next start does not read it again.
    assert state.load(d.paths.state_dir / state.FILE_NAME)[0].stream is None


def test_start_closes_the_port_when_the_state_file_is_unusable(tmp_path: Path) -> None:
    fw = FakeFirewall()
    d, log = _daemon(tmp_path, fw)
    d.paths.state_dir.mkdir(parents=True)
    (d.paths.state_dir / state.FILE_NAME).write_text("{")
    d.control.reconcile()
    assert fw.calls == ["close"]
    assert "treating it as no stream running" in log[0]


def test_a_firewall_failure_is_reported_not_hidden(tmp_path: Path) -> None:
    fw = FakeFirewall(fail="could not close the stream port: sudo: a password is required")
    d, log = _daemon(tmp_path, fw)
    d.control.reconcile()
    info = d.status()
    assert info["stream_port_state"] == "unknown"
    assert "password is required" in info["firewall_problem"]
    assert log[0].startswith("WARNING: could not close")
    assert "WARNING: could not close" in cli.format_status(info, info["version"])


def test_serving_reconciles_first(short_tmp: Path, tailnet: FakeRunner) -> None:
    fw = FakeFirewall()
    run = Running(short_tmp, tailnet, config.Config(dashboard_port=_free_port()), fw).start()
    try:
        assert fw.calls == ["close"]
        assert client.call(run.paths.socket, "GET", "status")["stream_port_state"] == "closed"
    finally:
        run.close()
    assert fw.calls == ["close"]  # stopping is the service's job (ExecStopPost), not the daemon's


# --- found in the review before milestone 2


def test_a_second_daemon_does_not_take_over_a_socket_that_is_in_use(running: Running) -> None:
    with pytest.raises(daemon.DaemonError, match="already listening"):
        daemon.bind_unix(running.paths.socket)
    second = daemon.Daemon(running.daemon.config, running.paths, FakeRunner(()), log=lambda m: None)
    with pytest.raises(daemon.DaemonError, match="already listening"):
        second.serve(threading.Event())
    assert client.call(running.paths.socket, "GET", "status")["pid"]  # the first still answers


def test_status_waits_for_tailscale_less_than_the_cli_waits_for_status(tmp_path: Path) -> None:
    waits: list[float | None] = []

    class Timed(FakeRunner):
        def run(self, args, *, timeout=None):  # type: ignore[no-untyped-def]
            waits.append(timeout)
            return super().run(args, timeout=timeout)

    _dash(tmp_path, Timed(("tailscale",)))
    assert len(waits) == 2 and sum(w or 1e9 for w in waits) < client.TIMEOUT


def test_a_stopped_tailscale_is_named_in_status(tmp_path: Path, tailnet: FakeRunner) -> None:
    publish(tailnet, 1338)
    stopped = json.dumps(node_status(state="Stopped"))
    tailnet.on("tailscale", "status", "--json", stdout=stopped)  # exit 0, as the real one
    dash = _dash(tmp_path, tailnet)
    assert dash["url"] is None and "Tailscale is stopped" in str(dash["problem"])


def test_daemon_command_reports_a_folder_it_cannot_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "run").write_text("a file where the run folder should go")
    monkeypatch.setenv(config.ROOT_ENV, str(tmp_path))
    assert cli.main(["daemon"]) == 1
    err = capsys.readouterr().err
    assert "the daemon cannot start" in err and "Traceback" not in err


def test_a_trial_daemon_gets_no_firewall_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[object] = []
    monkeypatch.setattr(daemon, "run", lambda cfg, paths, runner, fw: seen.append(fw) or 0)
    monkeypatch.setenv(config.ROOT_ENV, str(tmp_path))
    assert cli.main(["daemon"]) == 0
    monkeypatch.delenv(config.ROOT_ENV)
    assert cli.main(["daemon"]) == 0
    assert seen[0] is None and type(seen[1]).__name__ == "SudoFirewall"


# --- a stream, from the command line to the gate socket


def _gate(path: Path, target: str, ip: str = "192.0.2.1") -> tuple[int, dict[str, str], bytes]:
    """Ask the gate socket the way Caddy does."""
    conn = client._UnixConnection(path, 5)
    try:
        conn.request("GET", target, headers={"X-Vpstream-Ip": ip, "Connection": "close"})
        answer = conn.getresponse()
        return answer.status, dict(answer.getheaders()), answer.read()
    finally:
        conn.close()


@pytest.fixture
def streaming(
    short_tmp: Path, tailnet: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Running]:
    cfg = config.Config(dashboard_port=_free_port(), stream_host="192.0.2.9")
    run = Running(short_tmp, tailnet, cfg, FakeFirewall(), FakeServer()).start()
    monkeypatch.setenv(config.ROOT_ENV, str(short_tmp))
    yield run
    run.close()


def test_start_devices_alias_and_stop_from_the_command_line(
    streaming: Running, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fw, server = streaming.daemon.firewall, streaming.daemon.server
    for name in ("e1.mkv", "e2.mkv"):
        (tmp_path / name).write_bytes(b"x")
    assert cli.main(["start", "--dir", str(tmp_path), "--alias", "aaa", "--ttl", "30"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"Stream:       /aaa \(direct, policy open, stops in (30m 00|29m 5\d)s\)", out)
    assert "Address:      http://192.0.2.9:1337/aaa  (index for browsers" in out
    assert "  2  http://192.0.2.9:1337/aaa/2  e2.mkv" in out
    assert "Stop it with: vpstream stop" in out
    assert fw.calls == ["close", "open"] and server.calls == ["start"]  # type: ignore[union-attr]

    status, headers, _ = _gate(streaming.paths.gate_socket, "/aaa/2")
    assert status == 200 and headers["X-Vpstream-File"] == "2"
    link = streaming.paths.videos_dir / "2"
    assert link.is_symlink() and link.resolve() == tmp_path / "e2.mkv"
    assert _gate(streaming.paths.gate_socket, "/nope")[0] == 404

    assert cli.main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "Policy: open" in out and "192.0.2.1  allowed" in out

    assert cli.main(["devices", "lock"]) == 0
    assert "Policy: locked" in capsys.readouterr().out
    assert _gate(streaming.paths.gate_socket, "/aaa/1", ip="192.0.2.2")[0] == 403
    assert cli.main(["devices", "enable", "192.0.2.2"]) == 0
    assert _gate(streaming.paths.gate_socket, "/aaa/1", ip="192.0.2.2")[0] == 200
    assert cli.main(["devices", "disable", "192.0.2.1"]) == 0
    out = capsys.readouterr().out
    assert "192.0.2.1  disabled" in out and "192.0.2.2  enabled" in out
    assert cli.main(["devices", "unlock"]) == 0
    assert "Policy: open" in capsys.readouterr().out

    assert cli.main(["alias", "Film"]) == 0
    assert "Stream:       /film (" in capsys.readouterr().out
    assert _gate(streaming.paths.gate_socket, "/aaa/1", ip="192.0.2.2")[0] == 404

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Stream:       /film (" in out and "Devices:      2 (see: vpstream devices)" in out
    assert "Stream port:  1337 (open)" in out

    assert cli.main(["stop"]) == 0
    assert "Stopped. Stream port 1337: closed" in capsys.readouterr().out
    assert not streaming.paths.videos_dir.exists()
    assert fw.calls == ["close", "open", "close"] and server.calls == ["start", "stop"]  # type: ignore[union-attr]
    assert _gate(streaming.paths.gate_socket, "/film")[0] == 404


def test_command_line_refusals(
    streaming: Running, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["stop"]) == 1
    assert "no stream is running" in capsys.readouterr().err
    assert cli.main(["devices"]) == 0
    assert "No stream is running." in capsys.readouterr().out
    assert cli.main(["devices", "enable", "192.0.2.1"]) == 1
    assert "no stream is running" in capsys.readouterr().err
    assert cli.main(["devices", "enable"]) == 2
    assert "give the address" in capsys.readouterr().err
    assert cli.main(["alias", "x"]) == 1
    assert cli.main(["start", "--file", str(tmp_path / "nope.mkv")]) == 1
    assert "not a file" in capsys.readouterr().err
    for bad in (
        ["start"],
        ["start", "--file", "a", "--dir", "b"],
        ["start", "--file", "a", "--mode", "x"],
    ):
        with pytest.raises(SystemExit):
            cli.main(bad)
    capsys.readouterr()


def test_an_empty_path_is_refused_without_a_traceback(
    streaming: Running, capsys: pytest.CaptureFixture[str]
) -> None:
    for flag in ("--file", "--dir"):
        assert cli.main(["start", flag, ""]) == 2
        assert f"{flag} needs a path" in capsys.readouterr().err
    assert streaming.daemon.control.stream is None


def test_a_daemon_that_fails_while_starting_leaves_no_socket_behind(
    short_tmp: Path, tailnet: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Running(short_tmp, tailnet, config.Config(dashboard_port=_free_port()))
    monkeypatch.setattr(
        run.daemon.control, "reconcile", lambda: (_ for _ in ()).throw(OSError("no disk"))
    )
    with pytest.raises(OSError, match="no disk"):
        run.daemon.serve(run.stop)
    assert not run.paths.socket.exists() and not run.paths.gate_socket.exists()


def test_a_stream_that_will_not_stop_does_not_keep_the_sockets(
    short_tmp: Path, tailnet: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Running(short_tmp, tailnet, config.Config(dashboard_port=_free_port())).start()
    monkeypatch.setattr(
        run.daemon.control, "shutdown", lambda: (_ for _ in ()).throw(RuntimeError("stuck"))
    )
    run.close()
    assert not run.paths.socket.exists() and not run.paths.gate_socket.exists()
    assert any("stopping the stream failed" in line for line in run.log)


def test_start_sends_the_full_path_of_a_relative_one(
    streaming: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "film.mkv").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["start", "--file", "film.mkv"]) == 0
    assert streaming.daemon.control.stream.videos == [str(tmp_path / "film.mkv")]  # type: ignore[union-attr]


def test_the_gate_socket_is_for_the_service_only(streaming: Running) -> None:
    mode = streaming.paths.gate_socket.stat().st_mode
    assert stat.S_ISSOCK(mode) and stat.S_IMODE(mode) == 0o660


def test_the_gate_socket_has_no_control_routes(streaming: Running, tmp_path: Path) -> None:
    conn = client._UnixConnection(streaming.paths.gate_socket, 5)
    conn.request("POST", "/api/v1/stream/start", body="{}", headers={"X-Vpstream-Ip": "192.0.2.1"})
    assert conn.getresponse().status == 405
    conn.close()
    assert _gate(streaming.paths.gate_socket, "/api/v1/status")[0] == 404


def test_stopping_the_daemon_stops_the_stream_and_removes_both_sockets(
    short_tmp: Path, tailnet: FakeRunner, tmp_path: Path
) -> None:
    fw, server = FakeFirewall(), FakeServer()
    run = Running(
        short_tmp, tailnet, config.Config(dashboard_port=_free_port()), fw, server
    ).start()
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    client.call(run.paths.socket, "POST", "stream/start", {"file": str(film)})
    run.close()
    assert fw.calls == ["close", "open", "close"] and server.calls == ["start", "stop"]
    assert not run.paths.socket.exists() and not run.paths.gate_socket.exists()
    assert state.load(run.paths.state_dir / state.FILE_NAME)[0].stream is None


def test_the_daemon_ends_a_stream_when_its_time_is_over(
    short_tmp: Path, tailnet: FakeRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "TICK_SECONDS", 0.05)
    fw = FakeFirewall()
    run = Running(short_tmp, tailnet, config.Config(dashboard_port=_free_port()), fw, FakeServer())
    run.start()
    try:
        film = tmp_path / "film.mkv"
        film.write_bytes(b"x")
        client.call(run.paths.socket, "POST", "stream/start", {"file": str(film), "ttl": 0.005})
        for _ in range(100):
            if client.call(run.paths.socket, "GET", "status")["stream"] is None:
                break
            run.stop.wait(0.05)
        assert client.call(run.paths.socket, "GET", "status")["stream"] is None
        assert fw.calls == ["close", "open", "close"]
    finally:
        run.close()


def test_a_second_daemon_leaves_the_first_ones_stream_and_firewall_alone(
    streaming: Running, tmp_path: Path
) -> None:
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    client.call(streaming.paths.socket, "POST", "stream/start", {"file": str(film)})
    fw = FakeFirewall()
    second = daemon.Daemon(
        streaming.daemon.config,
        streaming.paths,
        FakeRunner(()),
        fw,
        FakeServer(),
        log=lambda m: None,
    )
    with pytest.raises(daemon.DaemonError, match="already listening"):
        second.serve(threading.Event())
    assert fw.calls == []
    assert client.call(streaming.paths.socket, "GET", "status")["stream"] is not None
    assert streaming.paths.gate_socket.exists()


def test_format_devices_and_states() -> None:
    stream = {
        "devices": [
            {
                "ip": "192.0.2.1",
                "allowed": None,
                "passes": True,
                "wl": False,
                "requests": 3,
                "blocked": 0,
                "bytes": 1_500_000,
                "last_seen": 90.0,
                "user_agent": "VLC/3.0.20",
            },
            {
                "ip": "2001:db8::1",
                "allowed": None,
                "passes": False,
                "wl": True,
                "requests": 2,
                "blocked": 2,
                "bytes": 0,
                "last_seen": 0,
                "user_agent": "",
            },
            {"ip": "192.0.2.3", "allowed": True, "passes": True, "wl": True},
            {"ip": "192.0.2.4", "allowed": False, "passes": False, "wl": False},
        ]
    }
    lines = cli.format_devices(stream, 100.0).splitlines()
    assert lines[0].split() == [
        "ADDRESS",
        "STATE",
        "REQUESTS",
        "BLOCKED",
        "SENT",
        "LAST",
        "SEEN",
        "PLAYER",
    ]
    assert lines[1].split() == [
        "192.0.2.1",
        "allowed",
        "3",
        "0",
        "1.5",
        "MB",
        "10s",
        "ago",
        "VLC/3.0.20",
    ]
    assert lines[2].split() == ["2001:db8::1", "waiting", "WL", "2", "2", "0", "B", "never", "-"]
    assert "enabled WL" in lines[3] and "disabled" in lines[4]
    assert "No device" in cli.format_devices({"devices": []}, 1.0)


@pytest.mark.parametrize(
    ("count", "text"),
    [(0, "0 B"), (999, "999 B"), (1000, "1.0 kB"), (2.5e9, "2.5 GB"), (5e12, "5000.0 GB")],
)
def test_size(count: float, text: str) -> None:
    assert cli._size(count) == text


def test_format_stream_of_one_file() -> None:
    text = cli.format_stream(
        {
            "alias": "aaa",
            "mode": "direct",
            "policy": "locked",
            "seconds_left": None,
            "source": "/srv/film.mkv",
            "kind": "file",
            "url": "http://192.0.2.9:1337/aaa",
            "videos": [{"number": 1, "name": "film.mkv", "url": "http://192.0.2.9:1337/aaa/1"}],
        }
    )
    assert text.splitlines() == [
        "Stream:       /aaa (direct, policy locked, no time limit)",
        "Source:       /srv/film.mkv",
        "Address:      http://192.0.2.9:1337/aaa",
    ]


def test_format_stream_shows_what_is_being_prepared() -> None:
    url = "http://192.0.2.9:1337/aaa"
    stream = {
        "alias": "aaa",
        "mode": "direct",
        "policy": "open",
        "seconds_left": None,
        "source": "/srv/show",
        "kind": "dir",
        "url": url,
        "videos": [
            {"number": 1, "name": "e1.mkv", "url": f"{url}/1", "preparing": True, "note": None},
            {
                "number": 2,
                "name": "e2.mkv",
                "url": f"{url}/2",
                "preparing": False,
                "note": "its seek index has no audio entries, so VLC resumes it slowly",
            },
        ],
    }
    lines = cli.format_stream(stream).splitlines()
    assert lines[3] == f"  1  {url}/1  e1.mkv  [preparing]"
    assert lines[4] == f"  2  {url}/2  e2.mkv"
    assert lines[5].startswith("Preparing:    1 MKV file gets a full seek index")
    assert (
        lines[6] == "note: video 2: its seek index has no audio entries, so VLC resumes it slowly"
    )
