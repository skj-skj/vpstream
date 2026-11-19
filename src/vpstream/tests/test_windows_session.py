"""The daemon inside the Windows window (`platform.windows.session`), and what it asks of
Windows (network, firewall, processes), with Windows faked: the port it picks, the address
and port it keeps, the crash counter, the gate behind its secret, Caddy's configuration,
the firewall rule. What only a real Windows does is tried there."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from vpstream import caddy, config, control, local, state
from vpstream.platform.common import FirewallError
from vpstream.platform.windows import WINDOWS_TEXT, firewall, network, processes, session
from vpstream.tests.fakes import FakeFirewall, FakeRunner, FakeServer

LAN = "192.168.1.73"
pytestmark = pytest.mark.usefixtures("on_windows")


@pytest.fixture
def lan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "find", lambda configured="": configured or LAN)
    monkeypatch.setattr(local, "is_mine", lambda address: address in (LAN, "10.0.0.9"))


@pytest.fixture
def film(tmp_path: Path) -> Path:
    path = tmp_path / "media" / "Film été.mkv"
    path.parent.mkdir()
    path.write_bytes(b"x" * 100)
    return path


def a_session(tmp_path: Path, taken: set[int] = frozenset(), **more: Any) -> session.Session:  # type: ignore[assignment]
    lines: list[str] = []
    return session.Session(
        config.Paths.under(tmp_path / "vp"),
        runner=FakeRunner(()),
        log=more.pop("log", lines.append),
        free=lambda host, port: port not in taken,
        boot="windows-7",
        firewall=more.pop("firewall", FakeFirewall()),
        server=more.pop("server", FakeServer()),
        has_address=more.pop("has_address", lambda address: local.is_mine(address)),
        **more,
    )


# --- the configuration file


def test_a_setting_replaces_its_line_or_takes_up_a_commented_one() -> None:
    text = session.set_setting(WINDOWS_TEXT, "network", "address", '"10.0.0.9"')
    assert config.parse(text).local_address == "10.0.0.9"
    assert text.count("address = ") == 1  # the commented example was taken up
    again = session.set_setting(text, "network", "address", '""')
    assert config.parse(again).local_address == "" and again.count("address = ") == 1
    port = session.set_setting(again, "stream", "port", "2345")
    assert config.parse(port).stream_port == 2345 and port.count("port = ") == 1
    assert "# " in port  # the explanations are kept


def test_a_setting_of_a_missing_section_is_added() -> None:
    text = session.set_setting("[prepare]\ncopy_speed = 2.0\n", "stream", "port", "1234")
    assert text == "[prepare]\ncopy_speed = 2.0\n\n[stream]\nport = 1234\n"
    assert session.set_setting("", "stream", "port", "1") == "[stream]\nport = 1\n"


def test_a_setting_that_would_not_be_read_back_is_not_written(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with pytest.raises(config.ConfigError):
        session.save_setting(path, "stream", "port", "0")
    assert not path.exists()
    session.save_setting(path, "network", "address", session.toml_string("10.0.0.9"))
    assert config.load(path).local_address == "10.0.0.9"
    assert session.toml_string('a"b\\') == '"a\\"b\\\\"'


def test_the_first_window_writes_windows_defaults(tmp_path: Path) -> None:
    cfg = session.load_config(tmp_path / "c" / "config.toml")
    assert (tmp_path / "c" / "config.toml").read_text() == WINDOWS_TEXT
    assert cfg.local and cfg.stream_port == 1337


# --- the port


def test_the_port_is_the_configured_one_or_an_easy_one() -> None:
    def free(*taken: int) -> Any:
        return lambda host, port: port not in taken

    assert session.pick_port(LAN, 1337, free()) == 1337
    assert session.pick_port(LAN, 1337, free(1337)) == 1234
    assert session.pick_port(LAN, 2345, free(2345, 1337, 1234)) == 3456
    assert session.pick_port(LAN, 1337, free(*session.PORTS)) is None
    assert session.PORTS[:4] == (1337, 1234, 2345, 3456) and len(set(session.PORTS)) == 14


def test_port_free_binds_and_lets_go() -> None:
    import socket

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert not session.port_free("127.0.0.1", taken)
    assert session.port_free("127.0.0.1", taken)


def test_a_start_on_a_busy_port_takes_the_next_and_keeps_it(
    tmp_path: Path, lan: None, film: Path
) -> None:
    s = a_session(tmp_path, taken={1337})
    s.open()
    try:
        info = s.start({"file": str(film), "alias": "film"})
        assert info["stream_port"] == 1234 and s.server.port == 1234
        assert s.firewall.calls == ["close", "open"]  # type: ignore[union-attr]
        assert info["stream"]["url"] == f"http://{LAN}:1234/film"
        assert config.load(s.paths.config_file).stream_port == 1234
    finally:
        s.close()


def test_no_free_port_is_said(tmp_path: Path, lan: None, film: Path) -> None:
    s = a_session(tmp_path, taken=set(session.PORTS))
    s.open()
    with pytest.raises(session.StreamPortError, match="no port to stream on is free"):
        s.start({"file": str(film)})
    s.close()


def test_the_chosen_address_is_kept(tmp_path: Path, lan: None) -> None:
    s = a_session(tmp_path)
    s.choose_address("10.0.0.9")
    assert s.control.host() == "10.0.0.9"
    assert config.load(s.paths.config_file).local_address == "10.0.0.9"
    s.choose_address("")
    assert s.control.host() == LAN


# --- a crash, and three


def recorded_stream(tmp_path: Path, film: Path) -> None:
    """What a window that crashed while it streamed leaves: the stream, recorded."""
    s = a_session(tmp_path)
    s.open()
    s.start({"file": str(film), "alias": "film"})
    s._stop.set()  # dies: no stop, no close


def test_a_stream_a_crash_left_is_taken_up_and_counted(
    tmp_path: Path, lan: None, film: Path
) -> None:
    recorded_stream(tmp_path, film)
    s = a_session(tmp_path)
    s.open()
    assert s.control.stream is not None and s.control.stream.alias == "film"
    assert s._crashes() == 1 and s.notice == ""
    s.close()
    assert s._crashes() == 0  # closed as it should be


def test_three_crashes_in_a_row_leave_the_stream_stopped(
    tmp_path: Path, lan: None, film: Path
) -> None:
    recorded_stream(tmp_path, film)
    for _ in range(3):
        s = a_session(tmp_path)
        s.open()
        s._stop.set()  # crashes again
    assert s._crashes() == 3 and s.control.stream is None
    assert "3 times in a row" in s.notice
    recorded, _ = state.load(s.paths.state_dir / state.FILE_NAME)
    assert recorded.stream is None
    s.start({"file": str(film)})  # the person starts one: it counts as fine again
    assert s._crashes() == 0
    s.close()


def test_a_resumed_stream_that_runs_ten_minutes_counts_as_fine(
    tmp_path: Path, lan: None, film: Path
) -> None:
    recorded_stream(tmp_path, film)
    now = [0.0]
    s = a_session(tmp_path, clock=lambda: now[0])
    s.open()
    assert s._crashes() == 1
    now[0] = session.CALM_SECONDS + 1
    threading.Event().wait(session.TICK_SECONDS * 2.5)
    assert s._crashes() == 0
    s.close()


# --- Wi-Fi turned off


def test_a_stream_stops_when_windows_no_longer_uses_its_address(
    tmp_path: Path, lan: None, film: Path
) -> None:
    """Windows keeps a turned-off Wi-Fi's address (Deprecated), and a socket can still be
    bound to it: Windows is asked whether it is in use."""
    wifi = [True]
    lines: list[str] = []
    s = a_session(tmp_path, has_address=lambda address: wifi[0], log=lines.append)
    s.open()
    s.start({"file": str(film)})
    wifi[0] = False
    for _ in range(50):
        if s.control.stream is None:
            break
        threading.Event().wait(0.1)
    assert s.control.stream is None
    assert any("is no longer this device's address" in line for line in lines)
    with pytest.raises(control.ControlError, match=r"192\.168\.1\.73 is not in use now"):
        s.start({"file": str(film)})
    s.close()


def test_in_use_is_what_windows_says_of_the_address() -> None:
    states = {"192.168.1.73": network.PREFERRED, "172.16.0.184": 1, "10.0.0.2": 3}
    assert network.in_use("192.168.1.73", lambda: states)
    assert not network.in_use("10.0.0.2", lambda: states)  # Deprecated: the Wi-Fi is off
    assert not network.in_use("172.16.0.184", lambda: states)  # Tentative: no cable
    assert not network.in_use("10.9.9.9", lambda: states)  # not there at all


def test_where_windows_cannot_be_asked_a_socket_is_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "is_mine", lambda address: address == "10.0.0.5")
    assert network.in_use("10.0.0.5", dict) and not network.in_use("10.0.0.6", dict)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own table")
def test_windows_table_of_addresses_has_its_loopback() -> None:
    states = network.address_states()
    assert states.get("127.0.0.1") == network.PREFERRED, states


# --- the gate and Caddy


def test_the_gate_answers_only_with_the_secret() -> None:
    def app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("200 OK", [])
        return [b"gate"]

    server = session.gate_server(session.guarded(app, "s3cret"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/x"
    try:
        with pytest.raises(urllib.error.HTTPError, match="403"):
            urllib.request.urlopen(url, timeout=5)
        wrong = urllib.request.Request(url, headers={session.GATE_HEADER: "guess"})
        with pytest.raises(urllib.error.HTTPError, match="403"):
            urllib.request.urlopen(wrong, timeout=5)
        right = urllib.request.Request(url, headers={session.GATE_HEADER: "s3cret"})
        assert urllib.request.urlopen(right, timeout=5).read() == b"gate"
    finally:
        server.shutdown()


def test_caddy_asks_the_gate_on_loopback_with_the_secret(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    server = session.WindowsCaddy(FakeRunner(()), 1337, paths, 50123, "s3cret")
    server.host = LAN
    cfg = server.config()
    linux = caddy.config(1337, paths.gate_socket, paths.access_log, paths.videos_dir, host=LAN)
    assert cfg == session.to_gate(linux, 50123, "s3cret")
    text = json.dumps(cfg)
    assert "unix/" not in text and '"dial": "127.0.0.1:50123"' in text
    proxy = cfg["apps"]["http"]["servers"][caddy.SERVER]["routes"][1]["handle"][0]
    assert proxy["headers"]["request"]["set"][session.GATE_HEADER] == ["s3cret"]
    assert proxy["headers"]["request"]["set"][caddy.HEADER_IP] == ["{http.request.remote.host}"]
    writer = cfg["logging"]["logs"]["access"]["writer"]
    assert writer["roll"] is False and "roll_size_mb" not in writer
    assert cfg["apps"]["http"]["servers"][caddy.SERVER]["listen"] == [f"{LAN}:1337"]


class Rule(FakeFirewall):
    def __init__(self, events: list[str], fail: str = "") -> None:
        super().__init__(fail)
        self.events = events
        self.port, self.program = 0, ""

    def open(self) -> None:
        self.events.append(f"rule {self.port} {self.program}")
        super().open()

    def close(self) -> None:
        self.events.append("no rule")


def test_a_session_whose_open_failed_part_way_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = config.Paths.under(tmp_path / "vp")
    server = session.WindowsCaddy(FakeRunner(("caddy",)), 1337, paths, 0, "s3cret")
    opened = a_session(tmp_path, server=server)

    def unreadable(path: Path) -> Any:
        raise OSError("the state file cannot be read")

    monkeypatch.setattr(state, "load", unreadable)
    with pytest.raises(OSError):
        opened.open()  # after the gate was made, before it served
    closing = threading.Thread(target=opened.close, daemon=True)
    closing.start()
    closing.join(5)
    assert not closing.is_alive()  # before, shutdown() waited for a gate that never served
    assert opened._gate.socket.fileno() == -1  # and its port is let go


def test_an_open_that_ends_after_the_close_starts_nothing(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path / "vp")
    server = session.WindowsCaddy(FakeRunner(("caddy",)), 1337, paths, 0, "s3cret")
    late = a_session(tmp_path, server=server)
    late.close()  # the window closed while the open was still under way
    late.open()
    assert all(thread.ident is None for thread in late._threads)
    late._gate.server_close()


def test_the_rule_is_there_before_caddy_listens_and_gone_when_it_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    server = session.WindowsCaddy(
        FakeRunner(("caddy",)), 2345, config.Paths.under(tmp_path), 1, "s"
    )
    server.firewall = Rule(events)  # type: ignore[assignment]

    def listen(self: Any) -> None:
        events.append("caddy listens")

    monkeypatch.setattr(caddy.CaddyServer, "start", listen)
    server.start()
    assert events == ["rule 2345 /usr/bin/caddy", "caddy listens"]

    def fail(self: Any) -> None:
        raise caddy.CaddyError("Caddy stopped at once")

    events.clear()
    monkeypatch.setattr(caddy.CaddyServer, "start", fail)
    with pytest.raises(caddy.CaddyError):
        server.start()
    assert events == ["rule 2345 /usr/bin/caddy", "no rule"]


def test_a_caddy_a_crashed_window_left_is_stopped_only_if_it_is_still_that_one(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "caddy.pid.json"
    noted = {"pid": 42, "program": "C:\\t\\caddy.exe", "started": 99}
    ended: list[int] = []
    for found, stopped in (
        ({"program": "C:\\t\\caddy.exe", "started": 99}, True),
        ({"program": "C:\\t\\caddy.exe", "started": 100}, False),  # the number, used again
        ({"program": "C:\\x\\notepad.exe", "started": 99}, False),
        (None, False),  # ended
    ):
        pid_file.write_text(json.dumps(noted))
        assert processes.stop_noted(pid_file, lambda pid, f=found: f, ended.append) is stopped
        assert not pid_file.exists()
    assert ended == [42]
    pid_file.write_text("not json")
    assert processes.stop_noted(pid_file) is False and not pid_file.exists()
    assert processes.stop_noted(pid_file) is False


@pytest.mark.skipif(sys.platform == "win32", reason="Windows answers for real (below)")
def test_away_from_windows_the_process_calls_do_nothing(tmp_path: Path) -> None:
    assert processes.is_admin() is False and processes.boot_id() == ""
    assert processes.describe(1) is None and not processes.relaunch_elevated("ana")
    assert processes.SingleInstance().first
    processes.keep_awake(True)
    processes.lower_priority()
    processes.write_pid(tmp_path / "p", 1)
    assert not (tmp_path / "p").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="what Windows itself answers")
def test_windows_answers_about_its_processes(tmp_path: Path) -> None:
    import os

    me = processes.describe(os.getpid())
    assert me is not None and me["program"].lower().endswith("python.exe") and me["started"]
    assert processes.boot_id().startswith("windows-")
    first = processes.SingleInstance("Local\\vpstream-test")
    assert first.first and not processes.SingleInstance("Local\\vpstream-test").first
    processes.write_pid(tmp_path / "p", os.getpid())
    assert json.loads((tmp_path / "p").read_text())["program"] == me["program"]
    assert processes.describe(2**31 - 1) is None


# --- the firewall rule


def test_the_rule_lets_in_only_caddy_on_the_port_from_the_local_subnet() -> None:
    runner = FakeRunner(())
    rule = firewall.NetshFirewall(runner, 1337, "C:\\Users\\Ana María\\caddy.exe")
    rule.open()
    assert runner.calls[0] == firewall.DELETE
    add = runner.calls[-1]
    assert add[:5] == ["netsh", "advfirewall", "firewall", "add", "rule"]
    for part in (
        "name=vpstream stream", "dir=in", "action=allow", "protocol=TCP", "localport=1337",
        "program=C:\\Users\\Ana María\\caddy.exe", "profile=private", "remoteip=localsubnet",
    ):  # fmt: skip
        assert part in add
    rule.open()  # again for the same: nothing to do
    assert len(runner.calls) == 2
    rule.port = 2345
    rule.open()
    assert runner.calls[-1][9] == "localport=2345" and len(runner.calls) == 4
    rule.close()
    assert runner.calls[-1] == firewall.DELETE and rule.opened is None


def test_closing_no_rule_is_fine_in_any_language() -> None:
    runner = FakeRunner(())
    runner.on("netsh", "advfirewall", "firewall", "delete", returncode=1, stdout="Aucune règle")
    runner.on("netsh", "advfirewall", "firewall", "show", returncode=1)
    firewall.NetshFirewall(runner, 1, "c").close()
    runner.on("netsh", "advfirewall", "firewall", "show", returncode=0)
    with pytest.raises(FirewallError, match="did not remove the stream's rule: Aucune"):
        firewall.NetshFirewall(runner, 1, "c").close()
    runner = FakeRunner(())
    runner.on("netsh", "advfirewall", "firewall", "add", returncode=1, stdout="denied")
    with pytest.raises(FirewallError, match="did not let the stream in: denied"):
        firewall.NetshFirewall(runner, 1, "c").open()


def test_rules_that_block_caddy_are_found() -> None:
    runner = FakeRunner(())
    runner.on("powershell", stdout="caddy.exe\r\n\r\ncaddy.exe\r\n")
    assert firewall.blocking_rules(runner, "C:\\O'Brien\\caddy.exe") == ["caddy.exe", "caddy.exe"]
    assert "-Program 'C:\\O''Brien\\caddy.exe'" in runner.calls[0][-1]
    runner.on("powershell", returncode=1)
    assert firewall.blocking_rules(runner, "c") == []


# --- the networks


ADDRESSES = [
    ("172.29.32.1", "vEthernet (Default Switch)", 18),
    ("169.254.105.194", "Ethernet 3", 11),
    ("100.90.17.123", "Tailscale", 8),
    ("192.168.1.73", "Wi-Fi", 16),
    ("127.0.0.1", "Loopback Pseudo-Interface 1", 1),
]
PS = json.dumps(
    {
        "addresses": [
            {"IPAddress": ip, "InterfaceAlias": alias, "InterfaceIndex": index}
            for ip, alias, index in ADDRESSES
        ],
        "profiles": {"InterfaceIndex": 16, "NetworkCategory": 0, "Name": "Home Wi-Fi"},
    }
)


def test_the_private_addresses_with_their_adapters() -> None:
    found = network.parse(PS)
    assert [a.label for a in found] == [
        "192.168.1.73 (Wi-Fi)",  # on a network Windows knows: first
        "172.29.32.1 (vEthernet (Default Switch))",
    ]
    wifi = found[0]
    assert (wifi.public, wifi.network, wifi.index) == (True, "Home Wi-Fi", 16)
    assert found[1].public is False


def test_an_address_of_an_adapter_that_is_off_is_not_offered() -> None:
    """Measured on Windows 10: an adapter with no cable (or its Wi-Fi off) keeps its
    address, as Tentative (1), and is not Up."""
    rows = [
        {"IPAddress": "192.168.1.73", "InterfaceAlias": "Wi-Fi", "InterfaceIndex": 16,
         "AddressState": 4},
        {"IPAddress": "172.16.0.184", "InterfaceAlias": "Ethernet", "InterfaceIndex": 5,
         "AddressState": 1},
        {"IPAddress": "172.29.32.1", "InterfaceAlias": "vEthernet", "InterfaceIndex": 18,
         "AddressState": 4},
    ]  # fmt: skip
    found = network.parse(json.dumps({"addresses": rows, "profiles": [], "up": [16]}))
    assert [a.ip for a in found] == ["192.168.1.73"]
    one = network.parse(json.dumps({"addresses": rows, "profiles": [], "up": 18}))
    assert [a.ip for a in one] == ["172.29.32.1"]  # one adapter up: not a list


def test_a_list_windows_did_not_give_is_said() -> None:
    for text in ("", "[1]", "nope"):
        with pytest.raises(network.NetworkError):
            network.parse(text)
    assert network.parse('{"addresses": null, "profiles": []}') == []
    runner = FakeRunner(())
    runner.on("powershell", returncode=1, stderr="Get-NetIPAddress: denied")
    with pytest.raises(network.NetworkError, match="denied"):
        network.addresses(runner)


def test_a_network_is_made_private_by_its_adapter() -> None:
    runner = FakeRunner(())
    network.make_private(runner, 16)
    assert runner.calls[0][-1].endswith(
        "Set-NetConnectionProfile -InterfaceIndex 16 -NetworkCategory Private"
    )
