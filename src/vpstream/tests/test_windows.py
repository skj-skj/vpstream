"""Windows: the person's own paths, the configuration, the command line, doctor, and the
platform-neutral changes Windows needed (paths that are not POSIX, a filter path that is
not plain). Run here as if on Windows; what only a real Windows has is tested there."""

from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from vpstream import cli, config, doctor, platform, prepare, prepared, stream, subtitles, tools
from vpstream.platform import windows
from vpstream.platform.windows import commands
from vpstream.platform.windows import doctor as windoctor
from vpstream.tests.fakes import FakeRunner

ENV = {"APPDATA": r"C:\Users\ana\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\ana\AppData\Local"}


def test_paths_are_the_persons_own() -> None:
    cfg, state, cache, run = windows.paths(ENV)
    assert str(cfg) == str(Path(ENV["APPDATA"]) / "vpstream" / "config.toml")
    assert [str(p) for p in (state, cache, run)] == [
        str(Path(ENV["LOCALAPPDATA"]) / "vpstream" / name) for name in ("state", "cache", "run")
    ]
    assert windows.tools_dir(ENV) == Path(ENV["LOCALAPPDATA"]) / "vpstream" / "tools"


def test_paths_without_the_variables_are_under_the_profile() -> None:
    cfg, state, *_ = windows.paths({"USERPROFILE": "/home/ana"})
    assert cfg == Path("/home/ana/AppData/Roaming/vpstream/config.toml")
    assert state == Path("/home/ana/AppData/Local/vpstream/state")


def test_default_paths_on_windows(on_windows: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ROOT_ENV, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    assert config.default_paths() == config.Paths(*windows.paths(ENV))


def test_programs_are_found_in_vpstreams_folders_first() -> None:
    env = {**ENV, "PATH": "/usr/bin;/x"}
    folders = windows.program_dirs(env)
    assert folders[:2] == [
        windows.tools_dir(env) / "ffmpeg" / "bin",
        windows.tools_dir(env) / "caddy",
    ]
    assert [str(f) for f in folders[2:]] == list(windows.MKVTOOLNIX_DIRS)
    a, b = str(Path("/a")), str(Path("/b"))
    joined = windows.search_path(windows.os.pathsep.join([b, a, b]), [Path(a)])
    assert joined.split(windows.os.pathsep) == [a, b, b]  # the rest as it was


def test_prepare_process_keeps_the_path_it_was_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows, "ORIGINAL_PATH", {})
    env = {**ENV, "PATH": "/usr/bin"}
    windows.prepare_process(env)
    assert env["PATH"].startswith(str(windows.program_dirs(ENV)[0]) + windows.os.pathsep)
    assert env["PATH"].endswith("/usr/bin")
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    windows.prepare_process(env)  # a second time: what it started with stays
    assert windows.ORIGINAL_PATH == {"PATH": "/usr/bin"}


# --- the configuration


def test_the_windows_configuration_is_local_without_termuxs_limits(on_windows: None) -> None:
    cfg = config.parse(windows.WINDOWS_TEXT)
    assert cfg.local and cfg.stream_port == 1337 and cfg.local_address == ""
    assert cfg.gate_connections == config.Config().gate_connections
    assert (cfg.caddy_cpus, cfg.caddy_memory_mb, cfg.prepare_threads) == (0, 0, 0)
    assert cfg.media_folder == config.Config().media_folder
    found = config.parse('[network]\naddress = "192.168.1.20"\n[stream]\nport = 2345\n')
    assert (found.local_address, found.stream_port) == ("192.168.1.20", 2345)


def test_what_windows_cannot_do_says_windows(on_windows: None) -> None:
    with pytest.raises(config.ConfigError, match="plain HTTP on the local network on Windows"):
        config.parse("[stream]\nhttps = true\n")
    with pytest.raises(config.ConfigError, match=r"Windows serves the local network only"):
        config.parse('[network]\nmode = "public"\n')


def test_termux_keeps_its_words_and_limits(on_termux: None) -> None:
    with pytest.raises(config.ConfigError, match="on the local network on Termux"):
        config.parse("[stream]\nhttps = true\n")
    assert config.parse("").caddy_memory_mb == config.TERMUX_LIMITS["caddy_memory_mb"]


# --- what messages tell the person to do


def test_hints_name_the_windows_install_command(on_windows: None) -> None:
    hints = platform.hints()
    assert hints is windows.HINTS
    assert hints.install("ffprobe") == "vpstream windows-install ffmpeg"
    assert hints.install("mkvmerge") == "vpstream windows-install mkvtoolnix"
    assert "vpstream windows-install caddy" in str(tools.ToolMissing("caddy", "serving streams"))


def test_linux_hints_are_as_they_were() -> None:
    assert platform.hints().install("ffmpeg") == "vpstream install ffmpeg"
    assert platform.hints().install("mkvmerge") == "sudo apt install mkvtoolnix"


def test_per_user_platforms() -> None:
    found = {}
    for name in platform.PLATFORMS:
        platform.override(name)
        found[name] = (platform.per_user(), platform.is_windows())
    assert found == {"linux": (False, False), "termux": (True, False), "windows": (True, True)}


def test_an_unreadable_file_on_windows(on_windows: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "ana")
    exc = stream.Unreadable(r"Z:\films\a.mkv")
    assert exc.user == "ana"
    assert r"\\server\share" in str(exc) and "administrator" in str(exc)
    assert exc.details["code"] == stream.Unreadable.CODE


def test_full_paths_are_this_systems(tmp_path: Path) -> None:
    full = str(tmp_path / "b.mkv")
    assert stream._full_paths([full]) and not stream._full_paths(["a/b.mkv", full])
    with pytest.raises(stream.StreamError, match="full path") as exc:
        stream.videos_of("file", Path("film.mkv"))
    assert ("starting with /" in str(exc.value)) == (os.sep == "/")


# --- the command line


@pytest.fixture
def quiet_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() on Windows changes this process's PATH; not in a test."""
    monkeypatch.setattr(windows, "prepare_process", lambda: None)


def test_only_some_commands_work_on_windows(
    on_windows: None, quiet_process: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["start", "--file", "C:/a.mkv"]) == 2
    said = capsys.readouterr().err
    assert "`vpstream start` is for Linux and Termux" in said and windows.WINDOW in said
    assert commands.refusal("selftest") is None and commands.refusal("doctor") is None
    assert commands.refusal(None) is None


def test_vpstream_alone_opens_the_window_on_windows(
    on_windows: None, quiet_process: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(commands, "open_window", lambda: opened.append(True) or 0)
    assert cli.main([]) == 0 and opened == [True]


def test_vpstream_alone_elsewhere_prints_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage: vpstream" in capsys.readouterr().out


def test_doctor_on_windows_asks_no_daemon(
    on_windows: None,
    quiet_process: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(config.ROOT_ENV, str(tmp_path))
    monkeypatch.setattr(tools, "SystemRunner", lambda: FakeRunner(("ffmpeg", "ffprobe", "caddy")))
    found = cli.main(["doctor"])
    said = capsys.readouterr().out
    assert "ok       caddy" in said and "daemon" not in said
    assert found == (1 if "PROBLEM  tkinter" in said else 0)


# --- doctor


def test_doctor_checks_tkinter_and_the_programs(on_windows: None, tmp_path: Path) -> None:
    paths = config.Paths.under(tmp_path)
    checks = doctor.run_checks(
        FakeRunner(("ffmpeg", "caddy")), paths=paths, original_path="", find=lambda name: None
    )
    by_name = {c.name: c for c in checks}
    assert by_name["tkinter"].status == "problem" and "tcl/tk" in by_name["tkinter"].fix
    assert by_name["ffprobe"].status == "problem"
    assert by_name["ffprobe"].fix == "vpstream windows-install ffmpeg"
    assert by_name["mkvmerge"].status == "warning"
    assert by_name["mkvmerge"].fix == "vpstream windows-install mkvtoolnix"
    assert by_name["config"].detail.endswith("the window writes it")
    assert [c.name for c in checks][:2] == ["python", "tkinter"]
    assert by_name["log"].detail == str(tmp_path / "var" / "lib" / "vpstream.log")


def test_mkvtoolnix_in_its_own_folder_gets_a_path_hint() -> None:
    found = r"C:\Program Files\MKVToolNix\mkvmerge.exe"
    assert "add C:\\Program Files\\MKVToolNix to PATH" in windoctor.path_hint(found, "")
    assert windoctor.path_hint(found, r"C:\Windows;c:\program files\mkvtoolnix\\") == ""
    assert windoctor.path_hint(r"C:\tools\mkvmerge.exe", "") == ""
    assert windoctor.path_hint(None, "") == ""


def test_the_config_check_shows_the_address(on_windows: None, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[network]\naddress = "10.0.0.5"\n', encoding="utf-8")
    assert "address 10.0.0.5" in windoctor.check_config(path).detail
    path.write_text("[stream]\nport = 1\n", encoding="utf-8")
    assert windoctor.check_config(path).status == "problem"


# --- a filter path that is not plain (C:\Users\Ana María\...)


@pytest.mark.skipif(sys.platform == "win32", reason="a path with a drive letter is never plain")
def test_a_plain_filter_path_is_used_as_it_is(tmp_path: Path) -> None:
    link = tmp_path / "source.mkv"
    assert subtitles.filter_path(link) == (link, None)


def test_a_filter_path_that_is_not_plain_is_named_from_its_folder(tmp_path: Path) -> None:
    link = tmp_path / "Ana María" / "work-2" / "source.mkv"
    assert subtitles.filter_path(link) == (Path("source.mkv"), link.parent)
    windows_like = Path(r"C:\Users\ana\work-2\source.mkv")
    name, folder = subtitles.filter_path(windows_like)
    assert folder is not None and str(name).endswith("source.mkv")


def test_run_cancellable_runs_in_the_folder_it_is_given(tmp_path: Path) -> None:
    code, out = tools.run_cancellable(
        [sys.executable, "-c", "import os; print(os.getcwd())"], threading.Event(), cwd=tmp_path
    )
    assert code == 0 and Path(out.strip()).resolve() == tmp_path.resolve()


@pytest.mark.integration
def test_a_burn_in_a_cache_whose_path_is_not_plain(sample: Path, tmp_path: Path) -> None:
    """The real ffmpeg, a cache under a folder with a space and an é: the subtitles filter
    gets the plain name, and ffmpeg runs in the work folder."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed on this host")
    cache = tmp_path / "Ana María" / "cache"
    runner = tools.SystemRunner()
    log: list[str] = []
    preparer = prepare.Preparer(cache, runner, log.append)
    entry = prepared.entry_dir(cache, str(sample))
    plan = prepare.plan_video(runner, cache, str(sample), "2", set(), preparer.speeds)
    assert [t.kind for t in plan.tasks] == [prepare.BASE, prepare.BURN]
    assert plan.info is not None
    preparer.submit(plan.source, plan.entry, plan.info, plan.tasks)
    assert preparer.wait(300), log
    jobs: list[dict[str, Any]] = preparer.status()
    assert {j["state"] for j in jobs} == {"done"}, (jobs, log)
    assert prepared.versions(entry).burn == [2]
    preparer.shutdown()


def test_every_test_skipped_on_windows_is_there() -> None:
    """conftest's WINDOWS_SKIPS names tests by file and name: none may be stale."""
    import ast

    from vpstream.tests import conftest

    here = Path(conftest.__file__).parent
    for key in conftest.WINDOWS_SKIPS:
        file, name = key.split("::")
        tree = ast.parse((here / file).read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert name.split("[")[0] in names, key


def test_what_a_program_writes_is_read_as_utf8() -> None:
    """ffmpeg writes UTF-8 (track titles, file names); Windows would read its code page."""
    write = "import sys; sys.stdout.buffer.write('Épisode 日本\\n'.encode())"
    assert tools.SystemRunner().run([sys.executable, "-c", write]).stdout == "Épisode 日本\n"
    code, out = tools.run_cancellable([sys.executable, "-c", write], threading.Event())
    assert (code, out.strip()) == (0, "Épisode 日本")


def test_under_takes_windows_paths_as_windows_does(monkeypatch: pytest.MonkeyPatch) -> None:
    import ntpath

    from vpstream import files

    monkeypatch.setattr(files.os, "path", ntpath)
    monkeypatch.setattr(files.os, "sep", "\\")
    assert files.under("C:/Films/Show/e1.mkv", "C:\\films\\show")  # / and either case
    assert files.under("C:\\Films\\a.mkv", "C:\\") and files.under("C:\\Films\\a.mkv", "C:")
    assert not files.under("C:\\Films2\\a.mkv", "C:\\Films")
    assert not files.under("D:\\Films\\a.mkv", "C:\\Films")


@pytest.mark.skipif(sys.platform != "win32", reason="a path with \\ in it")
def test_ffmpeg_is_given_hls_paths_with_slashes(tmp_path: Path) -> None:
    """ffmpeg puts the init segment next to the playlist by cutting the playlist's path at
    its last /: with only \\ in it, the init segment went to the folder ffmpeg ran in."""
    from vpstream import ffargs

    args = ffargs.hls_output_args(tmp_path / "base.part")
    assert args[-1] == f"{tmp_path.as_posix()}/base.part/index.m3u8" and "\\" not in args[-1]
    assert "\\" not in args[-2]
