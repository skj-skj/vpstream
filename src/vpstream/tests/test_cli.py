from __future__ import annotations

from pathlib import Path

import pytest

from vpstream import cli, net, selftest


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


@pytest.mark.parametrize(
    ("host", "shown"),
    [("0.0.0.0", "<server-ip>"), ("::", "<server-ip>"), ("::1", "[::1]"), ("10.0.0.2", "10.0.0.2")],
)
def test_url_host(host: str, shown: str) -> None:
    assert net.url_host(host) == shown


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

    class Svc:
        public = False

        def dashboard_public(self, public: bool | None = None) -> bool:
            return self.public if public is None else public

        def wants_password(self, public: bool | None = None) -> bool:
            return False

        def init(self, public: object = None, password: object = None) -> None:
            calls.append((public, password))

    monkeypatch.setattr(cli, "_service", lambda args: Svc())
    path = tmp_path / "pw"
    path.write_text("from the file\n")
    assert cli.main(["init", "--no-tailscale", "--dash-pass-file", str(path)]) == 0
    assert cli.main(["init"]) == 0
    assert cli.main(["init", "--tailscale"]) == 0
    assert calls == [(True, "from the file"), (False, None), (False, None)]
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


def test_dashboard_cert_prints_the_authority_to_install(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vpstream import cli

    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"
    monkeypatch.setattr(cli, "_daemon", lambda route: {"fingerprint": "AB:CD", "pem": pem})
    assert cli.main(["dashboard-cert"]) == 0
    out = capsys.readouterr()
    assert out.out == pem + "\n" and "SHA-256 AB:CD" in out.err
