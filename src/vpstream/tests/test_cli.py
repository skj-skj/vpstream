from __future__ import annotations

from pathlib import Path

import pytest

from vpstream import cli, selftest


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "bench" in capsys.readouterr().out


def test_bench_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bench", "/no/such/file.mkv"]) == 2
    assert "not a file" in capsys.readouterr().err


def test_selftest_args() -> None:
    args = selftest.pytest_args(True, ["-k", "probe"])
    assert args[0] == str(selftest.TESTS_DIR)
    assert "--integration" in args
    assert args[-2:] == ["-k", "probe"]
    assert "--integration" not in selftest.pytest_args(False, [])


def test_selftest_without_pytest_names_the_system_wide_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "pytest", None)  # makes `import pytest` fail
    assert selftest.run() == 2
    err = capsys.readouterr().err
    # Not a plain `pipx install`: that makes the per-user install init refuses.
    assert 'sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install --force "vp' in err


@pytest.mark.parametrize("port", ["0", "70000", "x"])
def test_caps_port_is_checked(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["caps", "--port", port])
    assert exc.value.code == 2
    assert "port number" in capsys.readouterr().err


def test_bench_checks_paths_before_running(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "a.mkv"
    f.write_bytes(b"x")
    assert cli.main(["bench", str(f), "--tmp", str(tmp_path / "nope")]) == 2
    assert "--tmp" in capsys.readouterr().err
    assert cli.main(["bench", str(f), "--json", str(tmp_path / "nope" / "r.json")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_caps_refuses_a_user_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mine = tmp_path / "videos"
    mine.mkdir()
    (mine / "film.mkv").write_text("x")
    out = str(tmp_path / "r.json")
    assert cli.main(["caps", "--clips", str(mine), "--out", out]) == 1
    assert "not made by vpstream" in capsys.readouterr().err
    assert (mine / "film.mkv").is_file()


def test_caps_checks_the_results_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["caps", "--out", str(tmp_path / "nope" / "r.json")]) == 2
    assert "does not exist" in capsys.readouterr().err


# --- the public dashboard's password and status


def _args(**over: object) -> object:
    import argparse

    return argparse.Namespace(**{"dash_pass": None, "dash_pass_file": None, **over})


def test_a_password_is_read_from_the_first_line_of_a_file(tmp_path: Path) -> None:
    from vpstream import cli

    path = tmp_path / "pw"
    path.write_text("  spaces kept  \r\nsecond line\n")
    assert cli._password(_args(dash_pass_file=str(path)), ask=False) == "  spaces kept  "
    path.write_text("\n")
    with pytest.raises(ValueError, match="empty"):
        cli._password(_args(dash_pass_file=str(path)), ask=False)
    with pytest.raises(ValueError, match="cannot read"):
        cli._password(_args(dash_pass_file=str(tmp_path / "none")), ask=False)


def test_a_password_from_standard_input(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from vpstream import cli

    monkeypatch.setattr("sys.stdin", io.StringIO("from a pipe\n"))
    assert cli._password(_args(dash_pass_file="-"), ask=False) == "from a pipe"


def test_a_password_on_the_command_line_is_warned_about(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vpstream import cli

    assert cli._password(_args(dash_pass="on the line"), ask=False) == "on the line"
    assert "shell history" in capsys.readouterr().err


def test_a_password_is_asked_twice_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    import getpass

    from vpstream import cli

    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    answers = iter(["typed once", "typed once", "first", "second"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: next(answers))
    assert cli._password(_args(), ask=True) == "typed once"
    with pytest.raises(ValueError, match="not the same"):
        cli._password(_args(), ask=True)
    assert cli._password(_args(), ask=False) is None


def test_init_takes_the_mode_and_the_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from vpstream import cli

    calls: list[tuple[object, object]] = []
    schemes: list[object] = []

    class Svc:
        public = False

        def dashboard_public(self, public: bool | None = None) -> bool:
            return self.public if public is None else public

        def wants_password(self, public: bool | None = None) -> bool:
            return False

        def init(
            self, public: object = None, password: object = None, https: object = None
        ) -> None:
            calls.append((public, password))
            schemes.append(https)

    monkeypatch.setattr(cli, "_service", lambda: Svc())
    path = tmp_path / "pw"
    path.write_text("from the file\n")
    assert cli.main(["init", "--no-tailscale", "--dash-pass-file", str(path)]) == 0
    assert cli.main(["init"]) == 0
    assert cli.main(["init", "--tailscale"]) == 0
    assert calls == [(True, "from the file"), (False, None), (False, None)]
    assert schemes == [None, None, None]
    assert cli.main(["init", "--https"]) == 0
    assert cli.main(["init", "--no-https"]) == 0
    assert schemes[3:] == [True, False]
    assert cli.main(["init", "--tailscale", "--dash-pass", "x" * 12]) == 2
    assert "public dashboard only" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["init", "--tailscale", "--no-tailscale"])
    with pytest.raises(SystemExit):
        cli.main(["init", "--dash-pass", "a", "--dash-pass-file", "b"])


def test_dash_pass_without_a_terminal_needs_a_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    from vpstream import cli

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000)
    assert cli.main(["dash-pass"]) == 1
    assert "needs root" in capsys.readouterr().err, "refused before a password is asked"
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    assert cli.main(["dash-pass"]) == 2
    assert "--dash-pass-file" in capsys.readouterr().err


def test_status_says_where_the_dashboard_is_and_how_it_stands() -> None:
    from vpstream import cli

    public = {
        "mode": "public",
        "url": "https://203.0.113.5:1338/",
        "port_state": "open",
        "certificate": "AB:CD",
        "password": True,
    }
    line = cli._dashboard_line(public)
    assert line.startswith("https://203.0.113.5:1338/ (public, port open)")
    assert "certificate authority SHA-256 AB:CD" in line
    down = {
        **public,
        "url": None,
        "problem": "Caddy failed",
        "password": False,
        "certificate": None,
    }
    line = cli._dashboard_line(down)
    assert "Caddy failed" in line and "NO PASSWORD" in line and "SHA-256" not in line
    tailnet = {"mode": "tailnet", "url": "https://host.ts.net:1338/"}
    assert cli._dashboard_line(tailnet) == "https://host.ts.net:1338/ (Tailscale only)"


def test_status_tells_https_and_its_certificate() -> None:
    import time

    from vpstream import cli

    assert cli._https_line({"enabled": False}) is None
    assert "no certificate yet" in str(cli._https_line({"enabled": True, "certificate": None}))
    soon = {"host": "203.0.113.5", "expires": time.time() + 2 * 3600 + 30}
    line = str(cli._https_line({"enabled": True, "certificate": soon}))
    assert "certificate for 203.0.113.5 until" in line and "2h" in line and "left" in line
    old = {"host": "203.0.113.5", "expires": 1.0}
    assert "expired" in str(cli._https_line({"enabled": True, "certificate": old}))
    info = {"version": "1", "https": {"enabled": True, "certificate": None}, "dashboard": {}}
    assert "HTTPS:        on; no certificate yet" in cli.format_status(info, "1")


def test_dashboard_cert_prints_the_authority_to_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vpstream import cli

    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"
    monkeypatch.setattr(cli, "_daemon", lambda route: {"fingerprint": "AB:CD", "pem": pem})
    assert cli.main(["dashboard-cert"]) == 0
    out = capsys.readouterr()
    assert out.out == pem + "\n" and "SHA-256 AB:CD" in out.err


def test_a_user_agent_cannot_write_control_characters_to_the_terminal() -> None:
    device = {
        "id": "192.0.2.1",
        "ip": "192.0.2.1",
        "kind": "player",
        "allowed": None,
        "user_agent": "VLC\u009b2J\x1b[31m/3.0",
        "requests": 1,
        "blocked": 0,
        "bytes": 0,
        "last_seen": 1.0,
        "passes": True,
    }
    out = cli.format_devices({"devices": [device]}, 2.0)
    assert "\u009b" not in out and "\x1b" not in out
    assert "VLC?2J?[31m/3.0" in out


# --- what status, devices and clients print


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
                "code": "0420",
            },
            {"ip": "192.0.2.3", "allowed": True, "passes": True, "wl": True},
            {"ip": "192.0.2.4", "allowed": False, "passes": False, "wl": False},
            {
                "id": "b-0a1b2c",
                "kind": "browser",
                "ip": "192.0.2.5",
                "name": "Living-room TV",
                "allowed": True,
                "passes": True,
                "user_agent": "JioSphere",
            },
        ]
    }
    lines = [" ".join(line.split()) for line in cli.format_devices(stream, 100.0).splitlines()]
    assert lines[0] == "DEVICE ADDRESS STATE CODE REQUESTS BLOCKED SENT LAST SEEN PLAYER"
    assert lines[1] == "192.0.2.1 192.0.2.1 allowed - 3 0 1.5 MB 10s ago VLC/3.0.20"
    assert lines[2] == "2001:db8::1 2001:db8::1 waiting WL 0420 2 2 0 B never -"
    assert "enabled WL" in lines[3] and "disabled" in lines[4]
    # A browser is listed by its id, and by the name the owner gave it.
    assert lines[5].split()[:3] == ["b-0a1b2c", "192.0.2.5", "enabled"]
    assert lines[5].endswith("Living-room TV")
    assert "No device" in cli.format_devices({"devices": []}, 1.0)


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


def test_format_clients() -> None:
    menu = {
        "audio": [{"value": "1", "label": "English"}, {"value": "2", "label": "Japanese"}],
        "subs": [{"value": "3-burn", "label": "Signs (burned in)"}],
    }
    client = {"video": 1, "position": 610, "duration": 5400, "paused": False, "audio": 2}
    stream = {
        "videos": [{"number": 1, "menu": menu}],
        "devices": [
            {"id": "192.0.2.2", "kind": "player", "passes": True},
            {
                "id": "b-000001",
                "kind": "browser",
                "passes": True,
                "name": "Living-room TV",
                "client": {
                    **client,
                    "sub": "3-burn",
                    "reported": 99,
                    "active": True,
                    "pending": {"seek": 3723},
                    "start_at": [1, 65],
                },
            },
            {
                "id": "b-000002",
                "kind": "browser",
                "passes": True,
                "user_agent": "Chrome",
                "client": {**client, "reported": 10, "paused": True, "sub": "off"},
            },
            {"id": "b-000003", "kind": "browser", "passes": True, "client": None},
            {"id": "b-000004", "kind": "browser", "passes": False, "allowed": None, "wl": True},
        ],
    }
    lines = cli.format_clients(stream, 100.0).splitlines()
    assert lines[0].split()[:3] == ["DEVICE", "STATE", "VIDEO"] and len(lines) == 5
    tv = lines[1]
    assert tv.startswith("b-000001") and "playing" in tv and "10:10 / 1:30:00" in tv
    assert "Japanese" in tv and "Signs (burned in)" in tv and "Living-room TV" in tv
    assert "seek 3723, start 1 at 1:05" in tv
    assert "gone 1m 30s ago" in lines[2] and "off" in lines[2] and lines[2].endswith("Chrome")
    assert "not playing yet" in lines[3]
    assert "waiting WL" in lines[4]
    assert "No browser" in cli.format_clients({"devices": stream["devices"][:1]}, 1.0)


def test_clients_command_refusals(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["clients", "set", "--seek", "10"]) == 2
    assert "--all" in capsys.readouterr().err
    assert cli.main(["clients", "set", "b-000001", "--all", "--seek", "10"]) == 2
    capsys.readouterr()
    assert cli.main(["clients", "set", "b-000001"]) == 2
    assert "say what to change" in capsys.readouterr().err
    assert cli.main(["clients", "list", "b-000001"]) == 2


def test_clients_command_sends_the_change(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[object] = []
    monkeypatch.setattr(cli, "_daemon", lambda route, payload=None: sent.append((route, payload)))
    cli.main(
        ["clients", "set", "b-1", "b-2", "--start-at", "10:10", "--video", "2", "--sub", "off"]
    )
    cli.main(["clients", "set", "--all", "--name", "TV"])
    cli.main(["clients"])
    assert sent == [
        ("clients", {"sub": "off", "start_at": "10:10", "video": 2, "devices": ["b-1", "b-2"]}),
        ("clients", {"name": "TV", "all": True}),
        ("status", None),
    ]
