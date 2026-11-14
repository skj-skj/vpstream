from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

from vpstream import caddy, config
from vpstream.tests.fakes import FakeRunner

# A stand-in for the caddy program: it listens where the configuration says, as Caddy does.
FAKE_CADDY = """\
#!{python}
import json, signal, socket, sys, time
assert sys.argv[1:3] == ["run", "--config"], sys.argv
cfg = json.load(open(sys.argv[3]))
mode = open(sys.argv[0] + ".mode").read().strip()
if mode == "broken":
    sys.exit("Error: loading config: something is wrong with it")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if mode == "deaf":
    time.sleep(60)
port = int(cfg["apps"]["http"]["servers"]["vpstream"]["listen"][0].lstrip(":"))
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen()
while True:
    s.accept()[0].close()
"""


class Which(FakeRunner):
    def __init__(self, program: Path | None) -> None:
        super().__init__(())
        self.program = program

    def which(self, name: str) -> str | None:
        return str(self.program) if self.program and name == "caddy" else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def program(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "caddy"
    path.parent.mkdir()
    path.write_text(FAKE_CADDY.format(python=sys.executable))
    path.chmod(0o755)
    Path(f"{path}.mode").write_text("ok")
    return path


def _server(tmp_path: Path, program: Path | None) -> caddy.CaddyServer:
    return caddy.CaddyServer(Which(program), _free_port(), config.Paths.under(tmp_path))


# --- the configuration


def test_config_is_json_and_never_opens_an_admin_port() -> None:
    cfg = caddy.config(
        1337, Path("/run/vpstream/gate.sock"), Path("/var/lib/x/access.log"), Path("/v")
    )
    assert json.loads(json.dumps(cfg)) == cfg
    assert cfg["admin"] == {"disabled": True, "config": {"persist": False}}
    server = cfg["apps"]["http"]["servers"]["vpstream"]
    assert server["listen"] == [":1337"]
    assert server["automatic_https"] == {"disable": True}


def test_every_request_goes_to_the_gate_with_the_real_address() -> None:
    cfg = caddy.config(1337, Path("/run/vpstream/gate.sock"), Path("/x/access.log"), Path("/v"))
    refuse, ask = cfg["apps"]["http"]["servers"]["vpstream"]["routes"]
    assert refuse["match"] == [{"not": [{"method": ["GET", "HEAD"]}]}] and refuse["terminal"]
    assert refuse["handle"][0]["status_code"] == 405
    assert "match" not in ask  # no path is served without asking
    (proxy,) = ask["handle"]
    assert proxy["upstreams"] == [{"dial": "unix//run/vpstream/gate.sock"}]
    # `set` replaces whatever the viewer sent under that name.
    assert proxy["headers"]["request"]["set"] == {"X-Vpstream-Ip": ["{http.request.remote.host}"]}


def test_a_file_is_sent_only_when_the_gate_names_one() -> None:
    cfg = caddy.config(1337, Path("/g.sock"), Path("/x/access.log"), Path("/v"))
    proxy = cfg["apps"]["http"]["servers"]["vpstream"]["routes"][1]["handle"][0]
    (on_file,) = proxy["handle_response"]
    assert on_file["match"] == {"headers": {"X-Vpstream-File": ["*"]}}
    handlers = on_file["routes"][0]["handle"]
    assert [h["handler"] for h in handlers] == ["headers", "rewrite", "file_server"]
    # The gate names a number, and only the folder of numbered links is served.
    assert handlers[1]["uri"] == "/{http.reverse_proxy.header.X-Vpstream-File}"
    assert handlers[2]["root"] == "/v"


def test_the_access_log_goes_to_its_own_file() -> None:
    cfg = caddy.config(1337, Path("/g.sock"), Path("/x/access.log"), Path("/v"))
    logs = cfg["logging"]["logs"]
    assert logs["access"]["writer"]["filename"] == "/x/access.log"
    assert logs["access"]["include"] == logs["default"]["exclude"] == ["http.log.access.vpstream"]
    assert cfg["apps"]["http"]["servers"]["vpstream"]["logs"] == {"default_logger_name": "vpstream"}


def test_videos_are_linked_by_number(tmp_path: Path) -> None:
    import os

    odd = os.fsdecode(b"caf\xe9 100%? {x}.mkv")
    videos = [str(tmp_path / "a.mkv"), str(tmp_path / odd)]
    folder = tmp_path / "caddy" / "videos"
    caddy.link_videos(folder, videos)
    assert sorted(p.name for p in folder.iterdir()) == ["1", "2"]
    assert [os.readlink(folder / n) for n in ("1", "2")] == videos
    caddy.link_videos(folder, videos[:1])  # the stream before is cleared away
    assert [p.name for p in folder.iterdir()] == ["1"]
    caddy.unlink_videos(folder)
    assert not folder.exists()
    caddy.unlink_videos(folder)  # nothing to remove is fine


# --- the access log


def test_log_entry() -> None:
    new = {"request": {"remote_ip": "192.0.2.1", "remote_port": "5"}, "size": 1024, "status": 206}
    old = {"request": {"remote_addr": "[2001:db8::1]:4711"}, "size": 7}
    mapped = {"request": {"remote_ip": "::ffff:192.0.2.1", "remote_port": "8"}, "size": 2}
    assert caddy.log_entry(json.dumps(new)) == ("192.0.2.1", "5", 1024)
    assert caddy.log_entry(json.dumps(old)) == ("2001:db8::1", "4711", 7)
    assert caddy.log_entry(json.dumps(mapped)) == ("192.0.2.1", "8", 2)
    plain = {"request": {"remote_addr": "192.0.2.1:9"}, "size": 1}
    assert caddy.log_entry(json.dumps(plain)) == ("192.0.2.1", "9", 1)
    for bad in ("", "{", "[]", "5", '{"size": 5}', '{"request": {"remote_ip": "x"}}'):
        assert caddy.log_entry(bad) is None, bad
    assert caddy.log_entry('{"request": {"remote_ip": "192.0.2.1"}, "size": "big"}') is None
    assert caddy.log_entry('{"request": {"remote_ip": 5}, "size": 1}') is None
    assert caddy.log_entry('{"request": {"remote_ip": "nope"}, "size": 1}') is None
    assert caddy.log_entry('{"request": "x", "size": 1}') is None


def _line(ip: str, size: int) -> str:
    return json.dumps({"request": {"remote_ip": ip, "remote_port": "7"}, "size": size}) + "\n"


def test_log_tail_reads_only_what_is_new(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    tail = caddy.LogTail(log)
    assert list(tail.read()) == []  # no file yet
    log.write_text(_line("192.0.2.1", 1))
    assert list(tail.read()) == [("192.0.2.1", "7", 1)]
    assert list(tail.read()) == []
    with log.open("a") as fh:
        fh.write(_line("192.0.2.2", 2) + "not json\n" + _line("192.0.2.3", 3))
    assert list(tail.read()) == [("192.0.2.2", "7", 2), ("192.0.2.3", "7", 3)]


def test_log_tail_waits_for_the_rest_of_a_half_written_line(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    tail = caddy.LogTail(log)
    whole = _line("192.0.2.1", 5)
    log.write_text(whole[:20])
    assert list(tail.read()) == []
    with log.open("a") as fh:
        fh.write(whole[20:])
    assert list(tail.read()) == [("192.0.2.1", "7", 5)]


def test_log_tail_follows_a_rotated_or_emptied_log(tmp_path: Path) -> None:
    log = tmp_path / "access.log"
    tail = caddy.LogTail(log)
    log.write_text(_line("192.0.2.1", 1) * 3)
    assert len(list(tail.read())) == 3
    log.rename(tmp_path / "access-old.log")  # Caddy rolls the file and starts a new one
    log.write_text(_line("192.0.2.2", 2))
    assert list(tail.read()) == [("192.0.2.2", "7", 2)]
    log.write_text("")  # emptied in place
    assert list(tail.read()) == []
    log.write_text(_line("192.0.2.3", 3))
    assert list(tail.read()) == [("192.0.2.3", "7", 3)]


# --- running it


def test_start_writes_the_configuration_and_waits_until_caddy_listens(
    tmp_path: Path, program: Path
) -> None:
    server = _server(tmp_path, program)
    paths = config.Paths.under(tmp_path)
    paths.caddy_dir.mkdir(parents=True)
    paths.access_log.write_text("left over from the stream before")
    try:
        server.start()
        assert server.running()
        with socket.create_connection(("127.0.0.1", server.port), timeout=2):
            pass
        written = json.loads((paths.caddy_dir / "caddy.json").read_text())
        assert written == caddy.config(
            server.port, paths.gate_socket, paths.access_log, paths.videos_dir
        )
        assert not paths.access_log.exists()
        server.start()  # already running: nothing more happens
    finally:
        server.stop()
    assert not server.running()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", server.port), timeout=1)
    server.stop()  # stopping twice is harmless


def test_start_reports_why_caddy_stopped(tmp_path: Path, program: Path) -> None:
    Path(f"{program}.mode").write_text("broken")
    server = _server(tmp_path, program)
    with pytest.raises(caddy.CaddyError, match=r"stopped at once.*something is wrong with it"):
        server.start()
    assert not server.running()


def test_start_gives_up_on_a_caddy_that_never_listens(
    tmp_path: Path, program: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path(f"{program}.mode").write_text("deaf")
    monkeypatch.setattr(caddy, "START_SECONDS", 0.5)
    server = _server(tmp_path, program)
    with pytest.raises(caddy.CaddyError, match="did not listen"):
        server.start()
    assert not server.running()


def test_start_refuses_a_port_that_is_taken(tmp_path: Path, program: Path) -> None:
    server = _server(tmp_path, program)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", server.port))
        busy.listen()
        with pytest.raises(caddy.CaddyError, match="already used"):
            server.start()


def test_start_says_when_caddy_is_not_installed(tmp_path: Path) -> None:
    from vpstream.tools import ToolMissing

    with pytest.raises(ToolMissing, match="sudo apt install caddy"):
        _server(tmp_path, None).start()


def test_running_notices_a_caddy_that_died(tmp_path: Path, program: Path) -> None:
    server = _server(tmp_path, program)
    server.start()
    assert server._proc is not None
    server._proc.kill()
    server._proc.wait()
    assert not server.running()
    server.stop()


# --- the public dashboard's Caddy


def test_the_dashboard_config_is_https_for_the_address_and_never_touches_trust_stores(
    tmp_path: Path,
) -> None:
    cfg = caddy.dashboard_config(1338, ["203.0.113.5"], tmp_path / "d.sock")
    assert cfg["admin"]["disabled"] is True
    assert cfg["apps"]["pki"]["certificate_authorities"]["local"]["install_trust"] is False
    tls = cfg["apps"]["tls"]
    assert tls["certificates"]["automate"] == ["203.0.113.5"]
    assert tls["automation"]["policies"] == [
        {"subjects": ["203.0.113.5"], "issuers": [{"module": "internal"}]}
    ]
    [server] = cfg["apps"]["http"]["servers"].values()
    assert server["listen"] == ["203.0.113.5:1338"]
    assert server["tls_connection_policies"] == [{"default_sni": "203.0.113.5"}]
    assert server["automatic_https"] == {"disable_redirects": True}
    [proxy] = server["routes"][0]["handle"]
    assert proxy["upstreams"] == [{"dial": f"unix/{tmp_path / 'd.sock'}"}]
    assert proxy["headers"]["request"]["set"] == {caddy.HEADER_IP: ["{http.request.remote.host}"]}


def test_both_caddies_hand_on_the_address_the_same_way(tmp_path: Path) -> None:
    stream = caddy.config(1337, tmp_path / "g.sock", tmp_path / "a.log", tmp_path / "v")
    gate = stream["apps"]["http"]["servers"]["vpstream"]["routes"][1]["handle"][0]
    dash = caddy.dashboard_config(1338, ["h"], tmp_path / "g.sock")
    proxy = dash["apps"]["http"]["servers"][caddy.DASHBOARD_SERVER]["routes"][0]["handle"][0]
    assert proxy["headers"] == gate["headers"] and proxy["upstreams"] == gate["upstreams"]


def test_the_fingerprint_is_the_one_openssl_shows() -> None:
    pem = (Path(__file__).parent / "data" / "caddy-root.crt").read_text()
    # openssl x509 -in caddy-root.crt -noout -fingerprint -sha256
    expect = (
        "AD:4D:AC:19:E9:B2:B3:B7:B7:41:5B:6D:AC:68:69:53:"
        "E1:10:AF:FA:3F:7D:B3:3B:4F:CF:A2:E6:63:FC:D6:61"
    )
    assert caddy.certificate_fingerprint(pem) == expect
    assert caddy.certificate_fingerprint("junk before\n" + pem + "\njunk after") == expect


@pytest.mark.parametrize(
    "pem",
    ["", "no certificate", "-----BEGIN CERTIFICATE-----\n@@@\n-----END CERTIFICATE-----\n"],
)
def test_a_file_without_a_certificate_has_no_fingerprint(pem: str) -> None:
    with pytest.raises(ValueError):
        caddy.certificate_fingerprint(pem)


def test_the_dashboard_caddy_keeps_its_files_apart_from_the_streams(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    dash = caddy.DashboardCaddy(FakeRunner(), 1338, ["203.0.113.5"], paths)
    stream = caddy.CaddyServer(FakeRunner(), 1337, paths)
    assert dash.work_dir != stream.work_dir
    assert dash.root_cert == caddy.dashboard_root_cert(paths)
    assert dash.root_cert.is_relative_to(dash.data_dir)
    assert dash.config() == caddy.dashboard_config(1338, ["203.0.113.5"], paths.dashboard_socket)
    assert dash.probe_host == "203.0.113.5" and stream.probe_host == "127.0.0.1"


def test_each_address_gets_a_listener_whose_certificate_is_its_own(tmp_path: Path) -> None:
    """No SNI is sent for an address, so a listener can pick only one certificate."""
    hosts = ["vps.example.org", "203.0.113.5", "2001:db8::5"]
    cfg = caddy.dashboard_config(1338, hosts, tmp_path / "d.sock")
    servers = cfg["apps"]["http"]["servers"]
    assert [(s["listen"], s["tls_connection_policies"]) for s in servers.values()] == [
        (["203.0.113.5:1338"], [{"default_sni": "203.0.113.5"}]),
        (["[2001:db8::5]:1338"], [{"default_sni": "2001:db8::5"}]),
    ]
    assert cfg["apps"]["tls"]["certificates"]["automate"] == hosts, "the name by its SNI"
    only_a_name = caddy.dashboard_config(1338, ["vps.example.org"], tmp_path / "d.sock")
    [server] = only_a_name["apps"]["http"]["servers"].values()
    assert server["listen"] == [":1338"]
    assert server["tls_connection_policies"] == [{"default_sni": "vps.example.org"}]
