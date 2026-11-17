"""Local mode: a private address only, and the stream stops when the address goes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vpstream import caddy, cli, config, local, show, state
from vpstream.control import Control, ControlError
from vpstream.tests.fakes import Clock, FakeRunner, FakeServer

LAN = "192.168.1.69"


@pytest.mark.parametrize(
    "address",
    ["192.168.1.5", "10.0.0.2", "172.16.0.1", "172.31.255.254", "169.254.3.4", "fd12::1",
     "::ffff:192.168.1.5"],
)  # fmt: skip
def test_private_addresses_are_local(address: str) -> None:
    assert local.private_problem(address) is None


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("100.64.0.1", "carrier's shared address"),
        ("100.127.255.254", "carrier's shared address"),
        ("100.108.41.15", "carrier's shared address"),  # Tailscale on the phone
        ("172.32.0.1", "not a private address"),
        ("8.8.8.8", "not a private address"),
        ("127.0.0.1", "not a private address"),
        ("0.0.0.0", "not a private address"),
        ("2001:db8::1", "not a private address"),
        ("::1", "not a private address"),
        ("not an address", "is not an IP address"),
        ("fe80::1", "IPv6 link-local address, which cannot be served"),
        ("fe80::1%wlan0", "IPv6 link-local address, which cannot be served"),
    ],
)
def test_other_addresses_are_refused(address: str, why: str) -> None:
    assert why in (local.private_problem(address) or "")


def test_network_of() -> None:
    assert local.network_of("192.168.1.69") == "192.168.0.0/16"
    assert local.network_of("10.1.2.3") == "10.0.0.0/8"
    with pytest.raises(local.LocalError):
        local.network_of("100.64.0.1")


def test_is_mine() -> None:
    assert local.is_mine("127.0.0.1")
    assert not local.is_mine("192.0.2.55")  # reserved for documentation: no host has it
    assert not local.is_mine("nonsense")


def test_find(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "is_mine", lambda address: address == LAN)
    monkeypatch.setattr(local, "interface_addresses", lambda: [])
    monkeypatch.setattr(local.net, "outgoing_address", lambda: LAN)
    assert local.find() == LAN
    assert local.find(LAN) == LAN
    with pytest.raises(local.LocalError, match="not an address of this device now"):
        local.find("192.168.1.70")
    monkeypatch.setattr(local.net, "outgoing_address", lambda: "100.71.103.68")  # mobile data
    with pytest.raises(local.LocalError, match="carrier's shared address"):
        local.find()
    monkeypatch.setattr(local.net, "outgoing_address", lambda: "")
    with pytest.raises(local.LocalError, match="on no network"):
        local.find()


def test_the_wifi_is_found_when_a_vpn_or_mobile_data_has_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tailscale's app, or mobile data while the Wi-Fi has no internet, carries the way out:
    the Wi-Fi's address is still this phone's, and the one to serve on."""
    wifi = "192.168.1.20"
    monkeypatch.setattr(local.net, "outgoing_address", lambda: "100.101.102.103")
    monkeypatch.setattr(local, "is_mine", lambda address: address == wifi)
    found = [("lo", "127.0.0.1"), ("tun0", "100.101.102.103"), ("rmnet0", "10.20.30.40"),
             ("wlan0", wifi)]  # fmt: skip
    monkeypatch.setattr(local, "interface_addresses", lambda: found)
    assert local.find() == wifi  # Wi-Fi before a carrier's private 10.x
    monkeypatch.setattr(local, "interface_addresses", lambda: found[:2])
    with pytest.raises(local.LocalError, match=r"carrier's shared .*set its address as"):
        local.find()


@pytest.mark.parametrize(
    ("outgoing", "found"),
    [
        ("100.101.102.103", [("tun0", "100.101.102.103"), ("rmnet_data0", "10.45.3.2")]),
        ("", [("tun0", "10.8.0.6")]),  # no route; an OpenVPN tunnel's 10.x
        ("10.45.3.2", [("rmnet_data0", "10.45.3.2")]),  # the carrier hands out a 10.x itself
        ("10.45.3.2", [("ccmni0", "10.45.3.2"), ("wwan0", "10.1.1.1")]),
    ],
)
def test_a_carrier_or_vpn_private_address_is_no_local_network(
    monkeypatch: pytest.MonkeyPatch, outgoing: str, found: list[tuple[str, str]]
) -> None:
    monkeypatch.setattr(local.net, "outgoing_address", lambda: outgoing)
    monkeypatch.setattr(local, "is_mine", lambda address: True)
    monkeypatch.setattr(local, "interface_addresses", lambda: found)
    with pytest.raises(local.LocalError):
        local.find()


def test_a_private_address_on_wifi_wire_or_bridge_is_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "is_mine", lambda address: True)
    for name in ("wlan0", "wlp3s0", "eth0", "enp2s0", "br0"):
        monkeypatch.setattr(local.net, "outgoing_address", lambda: "10.0.0.7")
        monkeypatch.setattr(local, "interface_addresses", lambda n=name: [(n, "10.0.0.7")])
        assert local.find() == "10.0.0.7", name
    monkeypatch.setattr(local, "interface_addresses", lambda: [])  # where they cannot be told
    assert local.find() == "10.0.0.7"
    assert local.find("10.45.3.2") == "10.45.3.2"  # one set by hand is the owner's choice


def test_interface_addresses_of_this_host() -> None:
    found = dict(local.interface_addresses())
    assert found.get("lo") == "127.0.0.1"  # SIOCGIFCONF works here, without root


def test_caddy_listens_on_one_address_in_local_mode(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    server = caddy.CaddyServer(FakeRunner(), 1337, paths)
    assert server.config()["apps"]["http"]["servers"]["vpstream"]["listen"] == [":1337"]
    assert server.probe_host == "127.0.0.1"
    server.host = LAN
    assert server.config()["apps"]["http"]["servers"]["vpstream"]["listen"] == [f"{LAN}:1337"]
    assert server.probe_host == LAN
    server.host = "fd12::1"
    assert server.config()["apps"]["http"]["servers"]["vpstream"]["listen"] == ["[fd12::1]:1337"]


class Here:
    """Whether this device has the LAN address, as a test says."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.on = True
        monkeypatch.setattr(local, "is_mine", lambda address: self.on and address == LAN)
        monkeypatch.setattr(local.net, "outgoing_address", lambda: LAN if self.on else "")
        monkeypatch.setattr(local, "interface_addresses", lambda: [])


@pytest.fixture
def here(monkeypatch: pytest.MonkeyPatch) -> Here:
    return Here(monkeypatch)


def _control(tmp_path: Path, server: FakeServer, log: list[str], boot: str = "b") -> Control:
    cfg = config.Config(local=True)
    return Control(cfg, config.Paths.under(tmp_path), None, server, log.append, Clock(), boot)


def _start(control: Control, tmp_path: Path) -> dict[str, Any]:
    film = tmp_path / "film.mp4"
    film.write_bytes(b"video")
    return control.start({"file": str(film), "alias": "aaa"})


def test_a_local_stream_listens_on_the_lan_address(tmp_path: Path, here: Here) -> None:
    server, log = FakeServer(), []
    control = _control(tmp_path, server, log)
    assert control.status()["local"] == {"address": LAN}
    info = _start(control, tmp_path)
    assert server.host == LAN
    assert info["stream"]["url"] == f"http://{LAN}:1337/aaa"
    assert info["local"] == {"address": LAN}
    assert info["stream_port_state"] == f"no firewall; listening on {LAN} only while a stream runs"


def test_no_local_stream_without_a_private_address(tmp_path: Path, here: Here) -> None:
    here.on = False
    control = _control(tmp_path, FakeServer(), [])
    assert control.status()["local"] == {"address": None}
    with pytest.raises(ControlError, match=r"no stream on the local network: .*on no network"):
        _start(control, tmp_path)
    assert control.stream is None


def test_the_stream_stops_when_the_address_goes(tmp_path: Path, here: Here) -> None:
    server, log = FakeServer(), []
    control = _control(tmp_path, server, log)
    _start(control, tmp_path)
    control.tick()
    assert control.stream is not None
    here.on = False  # the Wi-Fi is turned off
    control.tick()
    assert control.stream is None and not server.up
    assert f"the stream is stopped ({LAN} is no longer this device's address" in log[-1]
    assert state.load(control.paths.state_dir / state.FILE_NAME)[0].stream is None


def test_a_resume_needs_the_address(tmp_path: Path, here: Here) -> None:
    server, log = FakeServer(), []
    _start(_control(tmp_path, server, log), tmp_path)
    here.on = False
    again = _control(tmp_path, FakeServer(), log)
    again.reconcile()
    assert again.stream is None
    assert any("could not be started again: no stream on the local network" in m for m in log)


def test_linux_streams_listen_everywhere_as_before(tmp_path: Path) -> None:
    server = FakeServer()
    cfg = config.Config(stream_host="192.0.2.9")
    control = Control(cfg, config.Paths.under(tmp_path), None, server, print, Clock(), "b")
    info = _start(control, tmp_path)
    assert server.host == "" and info["local"] is None
    assert info["stream_port_state"] == "unknown"


def test_with_local() -> None:
    cfg = config.Config()
    assert config.with_local(cfg, False) is cfg
    assert config.with_local(cfg, True, LAN) == config.Config(local=True, local_address=LAN)
    with pytest.raises(config.ConfigError, match="--address goes with --local"):
        config.with_local(cfg, False, LAN)
    with pytest.raises(config.ConfigError, match="cannot go with"):
        config.with_local(config.Config(dashboard_public=True), True)
    with pytest.raises(config.ConfigError, match="must be an address"):
        config.with_local(cfg, True, "a;b")
    for wrong in ("phone.lan", "8.8.8.8", "100.64.1.1"):  # found out now, not at a start
        with pytest.raises(config.ConfigError, match="--address: "):
            config.with_local(cfg, True, wrong)


def test_status_reminds_of_local_mode() -> None:
    info: dict[str, Any] = {"version": "1", "local": {"address": LAN}, "dashboard": {}}
    assert f"Local mode:   {LAN} ({local.REMINDER})" in show.format_status(info, "1")
    info["local"] = {"address": None}
    assert "no private address now" in show.format_status(info, "1")
    assert "Local mode" not in show.format_status({"version": "1"}, "1")


def test_start_warns_in_local_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    stream = {"alias": "aaa", "url": f"http://{LAN}:1337/aaa", "videos": [], "mode": "direct"}
    answer = {"stream": stream, "local": {"address": LAN}, "dashboard": {"mode": "local"}}
    monkeypatch.setattr(cli, "_request", lambda *a, **k: answer)
    film = tmp_path / "film.mp4"
    film.write_bytes(b"v")
    assert cli.main(["start", "--file", str(film)]) == 0
    assert local.WARNING in capsys.readouterr().err
    monkeypatch.setattr(cli, "_request", lambda *a, **k: {**answer, "local": None})
    assert cli.main(["start", "--file", str(film)]) == 0
    assert "Local mode" not in capsys.readouterr().err


def test_linux_daemon_local_keeps_the_firewall_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vpstream import daemon
    from vpstream.platform.linux import firewall

    monkeypatch.delenv(config.ROOT_ENV, raising=False)
    monkeypatch.setattr(config, "default_paths", lambda: config.Paths.under(tmp_path))
    got: dict[str, Any] = {}

    def run(cfg: config.Config, paths: config.Paths, runner: Any, *walls: Any) -> int:
        got.update(cfg=cfg, walls=walls)
        return 0

    monkeypatch.setattr(daemon, "run", run)
    assert cli.main(["daemon", "--local", "--address", LAN]) == 0
    assert (got["cfg"].local, got["cfg"].local_address) == (True, LAN)
    assert all(isinstance(w, firewall.SudoFirewall) for w in got["walls"])
    assert cli.main(["daemon"]) == 0
    assert got["cfg"].local is False and len(got["walls"]) == 3
    assert cli.main(["daemon", "--address", LAN]) == 1
