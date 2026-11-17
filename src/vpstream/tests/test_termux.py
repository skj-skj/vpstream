"""Termux: the person's own paths, the configuration, and the commands that need no root."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vpstream import cli, client, config, doctor, login, platform, stream, tools
from vpstream.platform import termux
from vpstream.platform.termux import commands
from vpstream.tests.fakes import FakeRunner

ENV = {"HOME": "/home/u", "PREFIX": "/usr"}


def test_paths_are_the_persons_own() -> None:
    assert termux.paths(ENV) == (
        Path("/home/u/.config/vpstream/config.toml"),
        Path("/home/u/.local/state/vpstream"),
        Path("/home/u/.cache/vpstream"),
        Path("/usr/var/run/vpstream"),
    )


def test_paths_follow_xdg_but_never_a_relative_one() -> None:
    env = {**ENV, "XDG_CONFIG_HOME": "/cfg", "XDG_CACHE_HOME": "relative"}
    found = termux.paths(env)
    assert found[0] == Path("/cfg/vpstream/config.toml")
    assert found[2] == Path("/home/u/.cache/vpstream")


def test_default_paths_on_termux(on_termux: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ROOT_ENV, raising=False)
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):  # a CI runner sets them
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", "/home/u")
    monkeypatch.setenv("PREFIX", "/usr")
    paths = config.default_paths()
    assert paths.socket == Path("/usr/var/run/vpstream/daemon.sock")
    assert paths.dashboard_key == Path("/home/u/.config/vpstream/dashboard.key")
    assert len(str(paths.dashboard_socket)) < 108  # what a unix socket path may be
    monkeypatch.setenv(config.ROOT_ENV, "/tmp/try")
    assert config.default_paths() == config.Paths.under(Path("/tmp/try"))


def test_termux_config_is_local_always(on_termux: None) -> None:
    assert config.parse("").local is True
    assert config.parse("").media_folder == config.TERMUX_MEDIA
    cfg = config.parse('[network]\nmode = "local"\naddress = "192.168.1.20"\n')
    assert (cfg.local, cfg.local_address) == (True, "192.168.1.20")
    assert config.parse(config.TERMUX_TEXT) == config.parse("")


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('[network]\nmode = "public"\n', 'must be "local"'),
        ("[dashboard]\npublic = true\n", "local network only on Termux"),
        ("[stream]\nhttps = true\n", "plain HTTP on the local network"),
        ('[network]\naddress = "a b"\n', "must be an address"),
        ('[network]\naddress = "phone.lan"\n', "is not an IP address"),
        ('[network]\naddress = "8.8.8.8"\n', "not a private address"),
        ('[network]\naddress = "100.64.1.1"\n', "carrier's shared address"),
    ],
)
def test_termux_config_refuses_public(on_termux: None, text: str, problem: str) -> None:
    with pytest.raises(config.ConfigError, match=problem):
        config.parse(text)


def test_linux_config_has_no_local_mode() -> None:
    assert config.parse("").local is False
    with pytest.raises(config.ConfigError) as refused:
        config.parse('[network]\nmode = "local"\n')
    assert str(refused.value) == "unknown setting 'network.mode'"  # word for word as on main
    # The default text, as init writes it on Linux, is the same as before.
    assert "[network]" not in config.DEFAULT_TEXT


def test_the_firewall_helper_never_reads_local_mode(on_termux: None, tmp_path: Path) -> None:
    from vpstream.platform.linux import firewall

    path = tmp_path / "config.toml"
    path.write_text('[network]\nmode = "local"\n', encoding="utf-8")
    cfg, problem = firewall._trusted_config(path, owner_uid=path.stat().st_uid)
    assert cfg is None and "unknown setting 'network.mode'" in problem


def test_hints_name_what_termux_has(on_termux: None) -> None:
    assert "pkg install ffmpeg" in str(tools.ToolMissing("ffprobe"))
    assert "pkg install mkvtoolnix" in str(tools.ToolMissing("mkvmerge"))
    with pytest.raises(client.DaemonUnavailable, match="Start it with: vpstream up"):
        client.call(Path("/nonexistent/daemon.sock"), "GET", "status")


def test_hints_on_linux_are_as_before() -> None:
    assert "vpstream install ffmpeg" in str(tools.ToolMissing("ffprobe"))
    assert "sudo apt install caddy" in str(tools.ToolMissing("caddy"))
    assert "sudo apt install mkvtoolnix" in str(tools.ToolMissing("mkvmerge"))
    with pytest.raises(client.DaemonUnavailable, match="sudo systemctl start vpstream"):
        client.call(Path("/nonexistent/daemon.sock"), "GET", "status")


def test_unreadable_says_what_termux_needs(on_termux: None) -> None:
    message = str(stream.Unreadable("/storage/emulated/0/a.mkv"))
    assert "termux-setup-storage" in message and "setfacl" not in message


def _paths(tmp_path: Path) -> config.Paths:
    return config.Paths(
        config_file=tmp_path / "config" / "config.toml",
        state_dir=tmp_path / "state",
        cache_dir=tmp_path / "cache",
        run_dir=tmp_path / "run",
    )


def test_init_writes_the_config_and_says_what_is_left(on_termux: None, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    said: list[str] = []
    commands.init(FakeRunner(("ffmpeg", "ffprobe")), paths, said.append, tmp_path / "none")
    assert paths.config_file.read_text(encoding="utf-8") == config.TERMUX_TEXT
    assert paths.state_dir.is_dir() and paths.cache_dir.is_dir()
    text = "\n".join(said)
    assert "caddy, mkvmerge, mkvextract): pkg install ffmpeg caddy mkvtoolnix" in text
    assert "termux-setup-storage" in text
    assert "vpstream dash-pass" in text and "vpstream up" in text
    said.clear()
    paths.config_file.write_text("[stream]\nport = 2000\n", encoding="utf-8")
    commands.init(FakeRunner(commands.NEEDED), paths, said.append, tmp_path)
    assert paths.config_file.read_text(encoding="utf-8") == "[stream]\nport = 2000\n"
    assert said[0].endswith(": kept")
    assert not any("pkg install" in line or "termux-setup-storage" in line for line in said)


def test_init_refuses_a_broken_config(on_termux: None, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_file.parent.mkdir()
    paths.config_file.write_text("[dashboard]\npublic = true\n", encoding="utf-8")
    with pytest.raises(commands.CommandError, match="fix the configuration"):
        commands.init(FakeRunner(), paths, lambda _: None, tmp_path)


def test_set_password_is_private_and_forgets_logins(on_termux: None, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.state_dir.mkdir()
    paths.login_record.write_text("{}", encoding="utf-8")
    said: list[str] = []
    commands.set_password(paths, "a long enough password", said.append)
    assert paths.dashboard_key.stat().st_mode & 0o077 == 0
    verifier = login.load_verifier(paths.dashboard_key)
    assert verifier is not None
    assert not paths.login_record.exists()
    assert "did not take it now" in said[-1]  # no daemon here
    with pytest.raises(commands.CommandError, match="at least"):
        commands.set_password(paths, "short", said.append)


def test_uninstall_keeps_prepared_videos_unless_purged(on_termux: None, tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    for folder in (paths.config_file.parent, paths.state_dir, paths.run_dir, paths.cache_dir):
        folder.mkdir(parents=True)
    (paths.cache_dir / "prepared").mkdir()
    said: list[str] = []
    commands.uninstall(paths, purge=False, say=said.append)
    assert not paths.config_file.parent.exists() and not paths.state_dir.exists()
    assert paths.cache_dir.exists() and any("kept (prepared videos)" in s for s in said)
    commands.uninstall(paths, purge=True, say=said.append)
    assert not paths.cache_dir.exists()


def test_uninstall_refuses_while_the_daemon_runs(
    on_termux: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.run_dir.mkdir()
    paths.socket.touch()
    monkeypatch.setattr(client, "call", lambda *a, **k: {"version": "x"})
    with pytest.raises(commands.CommandError, match="vpstream down"):
        commands.uninstall(paths, purge=False)


def test_cli_on_termux(
    on_termux: None, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["install", "caddy"]) == 1
    assert "pkg install caddy" in capsys.readouterr().err
    assert cli.main(["revoke"]) == 0
    assert "Nothing to take back on Termux" in capsys.readouterr().out
    assert cli.main(["init", "--no-tailscale"]) == 2
    assert "local network only" in capsys.readouterr().err
    assert cli.main(["init", "--address", "192.168.1.20"]) == 2  # [network] address instead
    assert "run init without options" in capsys.readouterr().err


def test_cli_daemon_on_termux_gets_no_firewall(
    on_termux: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vpstream import daemon

    monkeypatch.setenv(config.ROOT_ENV, str(tmp_path))
    got: dict[str, Any] = {}
    monkeypatch.setattr(daemon, "run", lambda cfg, paths, runner, *rest: got.update(rest=rest) or 0)
    cli.main(["daemon"])
    assert got["rest"] == ()


def test_doctor_on_termux_checks_what_termux_needs(on_termux: None, tmp_path: Path) -> None:
    checks = doctor.run_checks(
        FakeRunner(("ffmpeg", "ffprobe")), paths=_paths(tmp_path), storage=tmp_path
    )
    names = {c.name for c in checks}
    assert not names & {"ufw", "systemctl", "tailscale", "sudo", "install", "systemd"}
    by_name = {c.name: c for c in checks}
    assert by_name["caddy"].status == "problem" and by_name["caddy"].fix == "pkg install caddy"
    assert by_name["mkvmerge"].fix == "pkg install mkvtoolnix"
    assert by_name["storage"].status == "ok"
    assert by_name["daemon"].fix == "vpstream up"
    assert by_name["dashboard password"].status == "warning"
    assert platform.current() == platform.TERMUX


def test_doctor_on_termux_without_storage(on_termux: None, tmp_path: Path) -> None:
    checks = doctor.run_checks(
        FakeRunner(commands.NEEDED), paths=_paths(tmp_path), storage=tmp_path / "none"
    )
    storage = next(c for c in checks if c.name == "storage")
    assert storage.status == "problem" and storage.fix == "termux-setup-storage"
    assert doctor.exit_code(checks) == 1
