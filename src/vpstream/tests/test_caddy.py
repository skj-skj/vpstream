from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from vpstream import caddy, config
from vpstream.certificate import Certificate
from vpstream.tests.fakes import FakeRunner
from vpstream.tools import ToolMissing

# A stand-in for the caddy program: it listens where the configuration says, as Caddy does.
FAKE_CADDY = """\
#!{python}
import json, signal, socket, sys, time
from pathlib import Path
assert sys.argv[1:3] == ["run", "--config"], sys.argv
cfg = json.load(open(sys.argv[3]))
mode = open(sys.argv[0] + ".mode").read().strip()
if mode == "broken":
    sys.exit("Error: loading config: something is wrong with it")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if mode == "deaf":
    time.sleep(60)
if "http" not in cfg["apps"]:  # the Caddy that requests a certificate: it never ends itself
    import os
    open(sys.argv[0] + ".pid", "w").write(str(os.getpid()))
    host = cfg["apps"]["tls"]["certificates"]["automate"][0]
    if mode == "issue":
        from vpstream.tests.fakes import store_certificate
        ca = cfg["apps"]["tls"]["automation"]["policies"][0]["issuers"][0]["ca"]
        now = time.time()
        store_certificate(Path(os.environ["XDG_DATA_HOME"]) / "caddy", ca, host, now, now + 576000)
    elif mode == "refuse":
        entry = dict(level="error", logger="tls.obtain", msg="will retry",
                     error="HTTP 429 rateLimited")
        print(json.dumps(entry), file=sys.stderr, flush=True)
    while True:
        time.sleep(1)
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
    assert proxy["headers"]["request"]["set"] == {
        "X-Vpstream-Ip": ["{http.request.remote.host}"],
        "X-Vpstream-Scheme": ["{http.request.scheme}"],
    }


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


def _dashboard_config(tmp_path: Path, hosts: list[str], sock: Path | None = None) -> dict[str, Any]:
    return caddy.dashboard_config(
        1338, hosts, sock or tmp_path / "d.sock", tmp_path / "ca.crt", tmp_path / "ca.key"
    )


def _authority(paths: config.Paths, hosts: list[str]) -> None:
    """An authority as init leaves it, made for `hosts`."""
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.dashboard_ca_cert.write_text("CERT")
    paths.dashboard_ca_key.write_text("KEY")
    paths.dashboard_ca_hosts.write_text(json.dumps({"hosts": hosts}))


def test_the_dashboard_caddy_does_not_start_without_its_own_authority(tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    dash = caddy.DashboardCaddy(FakeRunner(), 1338, ["203.0.113.5"], paths)
    with pytest.raises(caddy.CaddyError, match="sudo vpstream init"):
        dash.start()


def test_the_dashboard_caddy_serves_only_the_addresses_its_authority_permits(
    tmp_path: Path,
) -> None:
    paths = config.Paths.under(tmp_path)
    _authority(paths, ["203.0.113.5"])
    dash = caddy.DashboardCaddy(FakeRunner(), 1338, ["198.51.100.7"], paths)
    with pytest.raises(caddy.CaddyError, match="install the new authority"):
        dash.start()  # the host has a new address: a phone would refuse it
    dash.hosts = ["203.0.113.5", "2001:db8::5"]  # IPv6 came up after init
    with pytest.raises(ToolMissing):  # FakeRunner has no caddy: it got that far
        dash.start()
    assert dash.hosts == ["203.0.113.5"]


def test_the_dashboard_config_is_https_for_the_address_and_never_touches_trust_stores(
    tmp_path: Path,
) -> None:
    cfg = _dashboard_config(tmp_path, ["203.0.113.5"])
    assert cfg["admin"]["disabled"] is True
    [(name, ca)] = cfg["apps"]["pki"]["certificate_authorities"].items()
    assert name == caddy.DASHBOARD_CA != "local" and ca["install_trust"] is False
    # Signed under the authority init made for this host, never Caddy's own.
    assert ca["root"] == {
        "certificate": str(tmp_path / "ca.crt"),
        "private_key": str(tmp_path / "ca.key"),
        "format": "pem_file",
    }
    tls = cfg["apps"]["tls"]
    assert tls["certificates"]["automate"] == ["203.0.113.5"]
    assert tls["automation"]["policies"] == [
        {"subjects": ["203.0.113.5"], "issuers": [{"module": "internal", "ca": name}]}
    ]
    [server] = cfg["apps"]["http"]["servers"].values()
    assert server["listen"] == ["203.0.113.5:1338"]
    assert server["tls_connection_policies"] == [{"default_sni": "203.0.113.5"}]
    assert server["automatic_https"] == {"disable_redirects": True}
    [proxy] = server["routes"][0]["handle"]
    assert proxy["upstreams"] == [{"dial": f"unix/{tmp_path / 'd.sock'}"}]
    assert proxy["headers"]["request"]["set"] == {
        caddy.HEADER_IP: ["{http.request.remote.host}"],
        caddy.HEADER_SCHEME: ["{http.request.scheme}"],
    }


def test_both_caddies_hand_on_the_address_the_same_way(tmp_path: Path) -> None:
    stream = caddy.config(1337, tmp_path / "g.sock", tmp_path / "a.log", tmp_path / "v")
    gate = stream["apps"]["http"]["servers"]["vpstream"]["routes"][1]["handle"][0]
    dash = _dashboard_config(tmp_path, ["h"], tmp_path / "g.sock")
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
    assert dash.root_cert == paths.dashboard_ca_cert
    assert dash.config() == caddy.dashboard_config(
        1338,
        ["203.0.113.5"],
        paths.dashboard_socket,
        paths.dashboard_ca_cert,
        paths.dashboard_ca_key,
    )
    assert dash.probe_host == "203.0.113.5" and stream.probe_host == "127.0.0.1"


def test_each_address_gets_a_listener_whose_certificate_is_its_own(tmp_path: Path) -> None:
    """No SNI is sent for an address, so a listener can pick only one certificate."""
    hosts = ["vps.example.org", "203.0.113.5", "2001:db8::5"]
    cfg = _dashboard_config(tmp_path, hosts)
    servers = cfg["apps"]["http"]["servers"]
    assert [(s["listen"], s["tls_connection_policies"]) for s in servers.values()] == [
        (["203.0.113.5:1338"], [{"default_sni": "203.0.113.5"}]),
        (["[2001:db8::5]:1338"], [{"default_sni": "2001:db8::5"}]),
    ]
    assert cfg["apps"]["tls"]["certificates"]["automate"] == hosts, "the name by its SNI"
    only_a_name = _dashboard_config(tmp_path, ["vps.example.org"])
    [server] = only_a_name["apps"]["http"]["servers"].values()
    assert server["listen"] == [":1338"]
    assert server["tls_connection_policies"] == [{"default_sni": "vps.example.org"}]


# --- HTTPS for the stream


def _cert(tmp_path: Path) -> Certificate:
    return Certificate("203.0.113.5", tmp_path / "c.crt", tmp_path / "c.key", 0.0, 1.0)


def test_the_stream_is_plain_http_without_a_certificate(tmp_path: Path) -> None:
    cfg = caddy.config(1337, tmp_path / "g.sock", tmp_path / "a.log", tmp_path / "v")
    assert "tls" not in cfg["apps"]
    server = cfg["apps"]["http"]["servers"]["vpstream"]
    assert "tls_connection_policies" not in server
    assert server["automatic_https"] == {"disable": True}


def test_the_stream_serves_its_certificate_from_files_and_never_asks(tmp_path: Path) -> None:
    cert = _cert(tmp_path)
    cfg = caddy.config(1337, tmp_path / "g.sock", tmp_path / "a.log", tmp_path / "v", cert)
    assert cfg["apps"]["tls"] == {
        "certificates": {
            "load_files": [{"certificate": str(cert.cert_file), "key": str(cert.key_file)}]
        }
    }
    server = cfg["apps"]["http"]["servers"]["vpstream"]
    assert server["listen"] == [":1337"]
    # A viewer sends no server name for an address.
    assert server["tls_connection_policies"] == [{"default_sni": "203.0.113.5"}]
    assert server["protocols"] == ["h1", "h2"]  # no HTTP/3 on UDP, which ufw does not open
    # Nothing in this Caddy ever asks an authority, or listens on port 80.
    assert server["automatic_https"] == {"disable": True}
    assert "automation" not in cfg["apps"]["tls"]
    assert cfg["admin"]["disabled"] is True


def test_the_requesting_caddy_asks_for_a_short_lived_certificate_over_http(
    tmp_path: Path,
) -> None:
    cfg = caddy.certificate_config("203.0.113.5", config.Config(stream_https=True))
    tls = cfg["apps"]["tls"]
    assert tls["certificates"] == {"automate": ["203.0.113.5"]}
    [policy] = tls["automation"]["policies"]
    assert policy["subjects"] == ["203.0.113.5"]
    assert policy["issuers"] == [
        {
            "module": "acme",
            "ca": config.LETS_ENCRYPT,
            "profile": "shortlived",
            "challenges": {"http": {"alternate_port": 80}, "tls-alpn": {"disabled": True}},
        }
    ]
    assert cfg["apps"]["pki"] == {"certificate_authorities": {"local": {"install_trust": False}}}
    assert "http" not in cfg["apps"], "it serves nothing; Caddy answers the check by itself"
    assert cfg["admin"]["disabled"] is True


def test_a_test_authority_comes_with_its_root(tmp_path: Path) -> None:
    settings = config.Config(
        acme_ca="https://127.0.0.1:14000/dir", acme_ca_root="/r.pem", acme_port=5002
    )
    [issuer] = caddy.certificate_config("127.0.0.1", settings)["apps"]["tls"]["automation"][
        "policies"
    ][0]["issuers"]
    assert issuer["ca"] == "https://127.0.0.1:14000/dir"
    assert issuer["trusted_roots_pem_files"] == ["/r.pem"]
    assert issuer["challenges"]["http"] == {"alternate_port": 5002}


def _log_entry(**entry: object) -> str:
    return json.dumps(entry)


def test_a_failed_check_is_told_with_the_authority_s_own_words() -> None:
    # What Caddy 2.11.9 wrote when Pebble could not reach the port.
    detail = 'Get "http://127.0.0.1:18080/.well-known/acme-challenge/x": connection refused'
    log = "\n".join(
        [
            _log_entry(level="info", logger="tls.obtain", msg="acquiring lock"),
            _log_entry(
                level="error",
                logger="tls.issuance.acme.acme_client",
                msg="challenge failed",
                problem={"type": "urn:ietf:params:acme:error:connection", "detail": detail},
            ),
            _log_entry(
                level="error",
                logger="tls.obtain",
                msg="could not get certificate from issuer",
                error="HTTP 400 ...",
            ),
            _log_entry(level="error", logger="tls.obtain", msg="will retry", retrying_in=60),
        ]
    )
    assert caddy.obtain_failure(log) == detail


def test_a_refusal_without_a_check_says_what_caddy_said() -> None:
    error = "HTTP 429 urn:ietf:params:acme:error:rateLimited - too many certificates"
    log = _log_entry(
        level="error", logger="tls.obtain", msg="could not get certificate from issuer", error=error
    )
    assert caddy.obtain_failure(log) == error


@pytest.mark.parametrize(
    "log",
    [
        "",
        "not json\n{",
        _log_entry(level="info", logger="tls.obtain", msg="obtaining certificate"),
        _log_entry(level="error", logger="pki.ca.local", msg="failed to install root certificate"),
        # A failed check alone: Caddy may still be trying another way; it says when it is not.
        _log_entry(level="error", msg="challenge failed", problem={"detail": "x"}),
    ],
)
def test_nothing_else_counts_as_a_failure(log: str) -> None:
    assert caddy.obtain_failure(log) is None


@pytest.mark.parametrize(
    ("output", "version", "refused"),
    [
        ("2.6.2\n", (2, 6, 2), True),  # Ubuntu's package
        ("v2.9.1 h1:abc=\n", (2, 9, 1), True),
        ("v2.10.0 h1:abc=\n", (2, 10, 0), False),
        ("v2.11.9 h1:wKoF=\n", (2, 11, 9), False),
        ("v3.0.0\n", (3, 0, 0), False),
        ("unknown\n", None, False),  # a build that does not say: it is tried
    ],
)
def test_only_a_caddy_with_acme_profiles_may_request(
    output: str, version: tuple[int, ...] | None, refused: bool
) -> None:
    runner = FakeRunner(("caddy",))
    runner.on("/usr/bin/caddy", "version", stdout=output)
    assert caddy.caddy_version(runner) == version
    problem = caddy.profiles_problem(runner)
    assert (problem is not None) is refused
    if refused:
        assert "2.10 or newer" in problem and "caddyserver.com/docs/install" in problem


def test_without_caddy_there_is_no_version() -> None:
    assert caddy.caddy_version(FakeRunner(())) is None


# --- the Caddy that requests the stream's certificate, run for real (a stand-in program)


def _requester(program: Path, tmp_path: Path) -> tuple[caddy.CertificateCaddy, config.Paths]:
    paths = config.Paths.under(tmp_path / "root")
    settings = config.Config(
        stream_https=True, acme_ca="https://ca.test/dir", acme_port=_free_port()
    )
    requester = caddy.CertificateCaddy(Which(program), settings, paths)
    requester.timeout = 3
    return requester, paths


def _gone(program: Path) -> bool:
    import os

    pid = int(Path(f"{program}.pid").read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("issue", None),
        ("refuse", "refused: HTTP 429 rateLimited"),
        ("quiet", "no certificate after 3"),
    ],
)
def test_the_requesting_caddy_is_stopped_however_it_ends(
    program: Path, tmp_path: Path, mode: str, error: str | None
) -> None:
    from vpstream.certificate import CertificateError, stored

    Path(f"{program}.mode").write_text(mode)
    requester, paths = _requester(program, tmp_path)
    store = paths.certificate_dir / "data" / "caddy"
    done = lambda: stored(store, "https://ca.test/dir", "203.0.113.5") is not None  # noqa: E731
    if error is None:
        requester.obtain("203.0.113.5", done)
    else:
        with pytest.raises(CertificateError, match=error):
            requester.obtain("203.0.113.5", done)
    assert not requester.running()
    assert _gone(program), "Caddy keeps trying when left running; each try may count"


def test_a_requesting_caddy_that_cannot_start_says_so(program: Path, tmp_path: Path) -> None:
    from vpstream.certificate import CertificateError

    Path(f"{program}.mode").write_text("broken")
    requester, _ = _requester(program, tmp_path)
    said = r"(could not request|stopped before it had the certificate).*something is wrong"
    with pytest.raises(CertificateError, match=said):
        requester.obtain("203.0.113.5", lambda: False)


def test_caddy_going_on_past_a_busy_port_is_a_failure() -> None:
    """Caddy only warns, in a plain line, and asks all the same: the other program would
    answer Let's Encrypt's check and spend a failed one."""
    line = (
        "2026/11/15 10:00:00 [WARNING] listen tcp :80: bind: address already in use - "
        "assuming conflicting listener is correctly configured and continuing"
    )
    assert caddy.obtain_failure(_log_entry(level="info", msg="obtaining") + "\n" + line) == line


def test_a_busy_check_port_is_seen_before_port_80_is_opened(tmp_path: Path) -> None:
    """problem() runs before the firewall opens the port, so a busy one opens nothing."""
    port = _free_port()
    settings = config.Config(stream_https=True, acme_ca="https://ca.test/dir", acme_port=port)
    requester = caddy.CertificateCaddy(Which(None), settings, config.Paths.under(tmp_path))
    assert requester.problem() is None
    other = socket.socket()
    other.bind(("0.0.0.0", port))
    other.listen()
    try:
        assert "already used by another program" in str(requester.problem())
    finally:
        other.close()
