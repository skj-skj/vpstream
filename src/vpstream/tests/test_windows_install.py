"""`vpstream windows-install` and `windows-uninstall`, with downloads and programs faked: what
is kept from a zip, the checks before anything changes, the swap, what uninstall removes,
the desktop icon's PowerShell. The real downloads and Windows itself are tried on Windows."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import io
import json
import os
import types
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from vpstream import cli
from vpstream.ffargs import REQUIRED_FILTERS
from vpstream.platform import windows
from vpstream.platform.common import SetupError
from vpstream.platform.windows import commands, install, shortcut
from vpstream.tests.fakes import FakeRunner
from vpstream.tools import Result

ENCODERS = " V....D libx264  x\n V....D libx265  x\n V....D libaom-av1  x\n"
FILTERS = "".join(f" T.. {name}  V->V  x\n" for name in REQUIRED_FILTERS)


def a_zip(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return out.getvalue()


FFMPEG_ZIP = a_zip(
    {
        "ffmpeg-9/bin/ffmpeg.exe": b"ffmpeg",
        "ffmpeg-9/bin/ffprobe.exe": b"ffprobe",
        "ffmpeg-9/bin/ffplay.exe": b"not kept",
        "ffmpeg-9/LICENSE": b"GPL",
        "ffmpeg-9/doc/ffmpeg.html": b"not kept",
    }
)


class Http:
    def __init__(self, data: bytes, pages: dict[str, object] | None = None) -> None:
        self.data = data
        self.pages = pages or {}
        self.urls: list[str] = []

    def get_json(self, url: str) -> object:
        self.urls.append(url)
        return self.pages[url]

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        return str(self.pages[url])

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        self.urls.append(url)
        dest.write_bytes(self.data)
        progress(len(self.data), len(self.data))
        return hashlib.sha256(self.data).hexdigest()


FFMPEG_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
)


def pinned(
    data: bytes, tool: install.Tool = install.FFMPEG, version: str = "9.0.2"
) -> install.Build:
    """A release of `tool` whose zip is `data`."""
    url = FFMPEG_URL if tool is install.FFMPEG else f"https://example.com/{tool.name}-{version}.zip"
    return install.Build(tool, version, url, hashlib.sha256(data).hexdigest(), len(data))


class Programs(FakeRunner):
    """A host whose ffmpeg lists `encoders` and every filter vpstream needs."""

    def __init__(self, encoders: str = ENCODERS) -> None:
        super().__init__(())
        self.encoders = encoders

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        lists = {"-encoders": self.encoders, "-filters": FILTERS}
        if len(args) == 3 and args[1] == "-hide_banner" and args[2] in lists:
            self.calls.append(list(args))
            return Result(0, lists[args[2]], "")
        return super().run(args, timeout=timeout)


def said() -> tuple[list[tuple[str, int, int]], install.Progress]:
    lines: list[tuple[str, int, int]] = []
    return lines, lambda what, done, total: lines.append((what, done, total))


# --- one pinned build


def test_only_the_named_files_are_kept_wherever_the_zip_puts_them(tmp_path: Path) -> None:
    archive = tmp_path / "f.zip"
    files = {"../../evil/ffmpeg.exe": b"one", "x/ffmpeg.exe": b"two", "x/LICENSE": b"L"}
    archive.write_bytes(a_zip({**files, "y\\ffprobe.exe": b"p", "x/ffplay.exe": b"-"}))
    install.unpack(archive, install.FFMPEG, tmp_path / "staged")
    out = tmp_path / "staged" / "ffmpeg" / "bin"
    assert sorted(p.name for p in out.iterdir()) == ["LICENSE", "ffmpeg.exe", "ffprobe.exe"]
    assert (out / "ffmpeg.exe").read_bytes() == b"one"  # the first of a name
    assert not (tmp_path / "evil").exists()
    archive.write_bytes(a_zip(files))
    with pytest.raises(SetupError, match=r"does not have ffprobe\.exe"):
        install.unpack(archive, install.FFMPEG, tmp_path / "again")


def test_a_build_is_downloaded_checked_and_put_in_place(tmp_path: Path) -> None:
    tools, http, runner = tmp_path / "tools", Http(FFMPEG_ZIP), Programs()
    lines, progress = said()
    done = install.install_build(pinned(FFMPEG_ZIP), tools, runner, http, progress)
    assert done == f"ffmpeg 9.0.2 is installed in {tools / 'ffmpeg'}"
    folder = tools / "ffmpeg" / "bin"
    assert sorted(p.name for p in folder.iterdir()) == ["LICENSE", "ffmpeg.exe", "ffprobe.exe"]
    marker = json.loads((tools / "ffmpeg" / install.MARKER).read_text())
    assert marker["version"] == "9.0.2" and marker["url"] == FFMPEG_URL
    assert install.installed_version(tools, install.FFMPEG) == "9.0.2"
    assert [p.name for p in tools.iterdir()] == ["ffmpeg"]  # no download, no staging left
    assert [c[0] for c in runner.calls if c[0].endswith(".exe")] == [
        str(tools / ".install-ffmpeg" / "staged" / "ffmpeg" / "bin" / name)
        for name in ("ffmpeg.exe", "ffmpeg.exe", "ffprobe.exe")
    ]  # run where it was unpacked, before it is put in place
    assert lines[0] == ("downloading ffmpeg 9.0.2", 0, len(FFMPEG_ZIP))
    assert ("checking the new ffmpeg", 0, 0) in lines


def test_a_new_build_replaces_the_old(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    (tools / "ffmpeg" / "bin").mkdir(parents=True)
    (tools / "ffmpeg" / "bin" / "old.dll").write_bytes(b"old")
    install.install_build(pinned(FFMPEG_ZIP), tools, Programs(), Http(FFMPEG_ZIP), said()[1])
    assert not (tools / "ffmpeg" / "bin" / "old.dll").exists()
    assert sorted(p.name for p in tools.iterdir()) == ["ffmpeg"]


@pytest.mark.parametrize(
    ("why", "setup"),
    [
        ("does not match its SHA-256", lambda b: dataclasses.replace(b, sha256="0" * 64)),
        ("lacks libx265", lambda b: b),
    ],
)
def test_a_build_that_fails_a_check_changes_nothing(
    tmp_path: Path, why: str, setup: Callable[[install.Build], install.Build]
) -> None:
    tools = tmp_path / "tools"
    (tools / "ffmpeg").mkdir(parents=True)
    (tools / "ffmpeg" / "kept").write_text("what was there")
    runner = Programs(" V....D libx264  x\n V....D libaom-av1  x\n")
    build = setup(pinned(FFMPEG_ZIP))
    with pytest.raises(SetupError, match=why):
        install.install_build(build, tools, runner, Http(FFMPEG_ZIP), said()[1])
    assert [p.name for p in tools.iterdir()] == ["ffmpeg"]
    assert (tools / "ffmpeg" / "kept").read_text() == "what was there"


def test_a_build_that_does_not_run_says_so(tmp_path: Path) -> None:
    runner = FakeRunner(())
    staged = tmp_path / "tools" / ".install-caddy" / "staged" / "caddy" / "caddy.exe"
    runner.on(str(staged), returncode=1, stderr="This app can't run on your PC")
    data = a_zip({"caddy.exe": b"caddy", "LICENSE": b"Apache"})
    with pytest.raises(SetupError, match=r"the new caddy\.exe does not run here: This app"):
        install.install_build(
            pinned(data, install.CADDY, "2.11.9"), tmp_path / "tools", runner, Http(data), said()[1]
        )


def running(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """`name` runs: Windows does not open it for writing."""
    real = Path.open

    def open_(path: Path, mode: str = "r", *args: object, **kw: object) -> object:
        if path.name == name and mode == "r+b":
            raise PermissionError(32, "The process cannot access the file")
        return real(path, mode, *args, **kw)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", open_)
    monkeypatch.setattr(install, "RUNNING_SECONDS", 0.0)


def test_a_running_program_is_found_before_anything_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools"
    (tools / "caddy").mkdir(parents=True)
    (tools / "caddy" / "caddy.exe").write_bytes(b"old")
    (tools / "caddy" / install.MARKER).write_text("{}")
    assert install.in_use(tools / "caddy") is None and install.in_use(tmp_path / "no") is None
    running(monkeypatch, "caddy.exe")
    assert install.in_use(tools / "caddy") == "caddy.exe"
    data = a_zip({"caddy.exe": b"new", "LICENSE": b"Apache"})
    with pytest.raises(SetupError, match=r"caddy\.exe is running: stop the stream"):
        install.install_build(
            pinned(data, install.CADDY, "2.11.9"), tools, Programs(), Http(data), said()[1]
        )
    with pytest.raises(SetupError, match=r"\(Caddy was left as it was\)"):
        install.uninstall(tools)
    assert (tools / "caddy" / "caddy.exe").read_bytes() == b"old"
    assert [p.name for p in tools.iterdir()] == ["caddy"]


def test_too_little_room_is_seen_before_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(FFMPEG_ZIP)
    monkeypatch.setattr(
        install.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=5_000_000)
    )
    big = dataclasses.replace(pinned(FFMPEG_ZIP), size=114_768_076)
    with pytest.raises(SetupError, match="has 5 MB free; it needs 314 MB"):
        install.install_build(big, tmp_path / "tools", FakeRunner(()), http, said()[1])
    assert http.urls == []


def test_a_download_of_unknown_size_counts_the_room_it_may_need(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    http = Http(MKV_ZIP)
    monkeypatch.setattr(
        install.shutil, "disk_usage", lambda p: types.SimpleNamespace(free=130_000_000)
    )
    build = dataclasses.replace(pinned(MKV_ZIP, install.MKVTOOLNIX, "103.0"), size=0)
    with pytest.raises(SetupError, match="has 130 MB free; it needs 240 MB"):
        install.install_build(build, tmp_path / "tools", FakeRunner(()), http, said()[1])
    assert http.urls == []


def test_what_an_install_left_is_swept(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    for name in (".install-ffmpeg", ".old-caddy", "caddy"):
        (tools / name).mkdir(parents=True)
    install.sweep(tools)
    assert [p.name for p in tools.iterdir()] == ["caddy"]
    install.sweep(tmp_path / "none")


# --- MKVToolNix: its portable zip, like the others

MKV_ZIP = a_zip(
    {
        "mkvtoolnix/mkvmerge.exe": b"merge",
        "mkvtoolnix/mkvextract.exe": b"extract",
        "mkvtoolnix/mkvtoolnix-gui.exe": b"not kept",
        "mkvtoolnix/doc/COPYING.txt": b"GPL",
    }
)


def test_mkvtoolnix_is_vpstreams_own_copy_from_its_portable_zip(tmp_path: Path) -> None:
    tools, runner = tmp_path / "tools", FakeRunner(())
    build = pinned(MKV_ZIP, install.MKVTOOLNIX, "103.0")
    done = install.install_build(build, tools, runner, Http(MKV_ZIP), said()[1])
    assert done == f"MKVToolNix 103.0 is installed in {tools / 'mkvtoolnix'}"
    kept = sorted(p.name for p in (tools / "mkvtoolnix").iterdir())
    assert kept == ["COPYING.txt", "mkvextract.exe", "mkvmerge.exe", install.MARKER]
    staged = tools / ".install-mkvtoolnix" / "staged" / "mkvtoolnix"
    assert runner.calls == [
        [str(staged / "mkvmerge.exe"), "--version"],
        [str(staged / "mkvextract.exe"), "--version"],
    ]
    assert install.missing(FakeRunner(("ffmpeg", "ffprobe", "caddy", "mkvmerge"))) == [
        "mkvtoolnix"  # mkvextract too
    ]


# --- the latest release, from each one's own release page


def github(version: str, asset: str, digest: str = "sha256:" + "a" * 64) -> dict[str, object]:
    return {
        "tag_name": version,
        "assets": [
            {"name": "other.zip", "size": 1, "browser_download_url": "x", "digest": digest},
            {"name": asset, "size": 18_384_117, "digest": digest,
             "browser_download_url": f"https://github.com/dl/{asset}"},
        ],
    }  # fmt: skip


CADDY_API = "https://api.github.com/repos/caddyserver/caddy/releases/latest"
FFMPEG_API = "https://api.github.com/repos/GyanD/codexffmpeg/releases/latest"
MKV_SUM = "https://mkvtoolnix.download/windows/releases/103.0/mkvtoolnix-64-bit-103.0.zip.sha256"
FEED = (
    '<?xml version="1.0"?><mkvtoolnix-releases><latest-source><version>103.0</version>'
    "</latest-source><latest-windows-pre><version>104.0.0.12</version></latest-windows-pre>"
    "</mkvtoolnix-releases>"
)


def test_the_latest_release_comes_with_its_sha256() -> None:
    http = Http(b"", {
        CADDY_API: github("v2.12.0", "caddy_2.12.0_windows_amd64.zip"),
        FFMPEG_API: github("9.1", "ffmpeg-9.1-essentials_build.zip", "sha256:" + "b" * 64),
        install.MKVTOOLNIX_LATEST: FEED,
        MKV_SUM: "C860A10C707CB1EE587D1464F6171842F2ADA3BB6F031DFFF55FEC75542F0417  m.zip\n",
    })  # fmt: skip
    caddy = install.latest(install.CADDY, http)
    assert (caddy.version, caddy.sha256, caddy.size) == ("2.12.0", "a" * 64, 18_384_117)
    assert caddy.url == "https://github.com/dl/caddy_2.12.0_windows_amd64.zip"
    assert install.latest(install.FFMPEG, http).version == "9.1"
    mkv = install.latest(install.MKVTOOLNIX, http)  # the release, not the pre-release
    assert (mkv.version, mkv.size) == ("103.0", 0)
    assert mkv.url == MKV_SUM.removesuffix(".sha256")
    assert mkv.sha256 == "c860a10c707cb1ee587d1464f6171842f2ada3bb6f031dfff55fec75542f0417"


@pytest.mark.parametrize(
    ("page", "why"),
    [
        (github("v2.12.0", "caddy_2.12.0_linux_amd64.zip"), "has no caddy_2.12.0_windows_amd64"),
        (github("v2.12.0", "caddy_2.12.0_windows_amd64.zip", ""), "with a SHA-256"),
        (github("v2.12.0", "caddy_2.12.0_windows_amd64.zip", "md5:abc"), "with a SHA-256"),
        (github("../../x", "caddy_../../x_windows_amd64.zip"), "does not name a version"),
        (github("", "caddy__windows_amd64.zip"), "does not name a version"),
    ],
)
def test_a_release_that_is_not_as_expected_installs_nothing(page: object, why: str) -> None:
    with pytest.raises(SetupError, match=why):
        install.latest(install.CADDY, Http(b"", {CADDY_API: page}))


class Portal(Http):
    """A network that answers every page with its own (a hotel's sign-in page)."""

    def get_json(self, url: str) -> object:
        raise json.JSONDecodeError("Expecting value", "<html>", 0)


def test_a_page_that_is_not_a_release_says_so() -> None:
    with pytest.raises(SetupError, match="Caddy's latest release cannot be read"):
        install.latest(install.CADDY, Portal(b""))
    no_size = github("v2.12.0", "caddy_2.12.0_windows_amd64.zip")
    del no_size["assets"][1]["size"]  # type: ignore[index]
    with pytest.raises(SetupError, match="cannot be read"):
        install.latest(install.CADDY, Http(b"", {CADDY_API: no_size}))
    plain = github("v2.12.0", "caddy_2.12.0_windows_amd64.zip")
    plain["assets"][1]["browser_download_url"] = "http://example.com/c.zip"  # type: ignore[index]
    with pytest.raises(SetupError, match="not offered over https"):
        install.latest(install.CADDY, Http(b"", {CADDY_API: plain}))


def test_mkvtoolnix_takes_the_release_never_the_pre_release() -> None:
    feed = (
        "<latest-source><url>x</url></latest-source>"
        "<latest-windows-pre><version>104.0.0.12</version></latest-windows-pre>"
    )
    with pytest.raises(SetupError, match="does not name a version"):
        install.latest(install.MKVTOOLNIX, Http(b"", {install.MKVTOOLNIX_LATEST: feed}))


def test_mkvtoolnix_without_a_sha256_installs_nothing() -> None:
    http = Http(b"", {install.MKVTOOLNIX_LATEST: FEED, MKV_SUM: "<html>not found</html>"})
    with pytest.raises(SetupError, match="has no SHA-256"):
        install.latest(install.MKVTOOLNIX, http)
    http = Http(b"", {install.MKVTOOLNIX_LATEST: "<html>maintenance</html>"})
    with pytest.raises(SetupError, match="does not name a version"):
        install.latest(install.MKVTOOLNIX, http)


# --- where each one is found, and its version


def test_what_each_program_says_its_version_is() -> None:
    runner = FakeRunner(())
    runner.on("C:\\t\\ffmpeg.exe", stdout="ffmpeg version 9.0.2-essentials_build-www.gyan.dev\n")
    runner.on("C:\\t\\caddy.exe", stdout="v2.11.9 h1:abc=\n")
    runner.on("C:\\t\\mkvmerge.exe", stdout="mkvmerge v103.0 ('The Darkness') 64-bit\n")
    runner.on("C:\\t\\git.exe", stdout="ffmpeg version N-118000-g1234 Copyright\n")
    runner.on("C:\\t\\bad.exe", returncode=1, stdout="1.2.3")
    runner.on("C:\\t\\tag.exe", stdout="ffmpeg version n7.1 Copyright (c) 2000-2024\n")
    said = [install.program_version(runner, f"C:\\t\\{n}.exe") for n in
            ("ffmpeg", "caddy", "mkvmerge", "git", "bad", "tag")]  # fmt: skip
    assert said == ["9.0.2", "2.11.9", "103.0", "", "", "7.1"]
    assert [c[1:] for c in runner.calls[:3]] == [["-version"], ["version"], ["--version"]]


def test_vpstreams_copy_is_told_from_the_systems(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    (tools / "caddy").mkdir(parents=True)
    (tools / "caddy" / install.MARKER).write_text('{"version": "2.11.9"}')

    class Found(FakeRunner):
        def which(self, name: str) -> str | None:
            where = {"caddy": str(tools / "caddy" / "caddy.exe"), "ffmpeg": "C:\\ff\\ffmpeg.exe"}
            return where.get(name)

    runner = Found(())
    runner.on(str(tools / "caddy" / "caddy.exe"), stdout="v2.11.9 h1:x\n")
    runner.on("C:\\ff\\ffmpeg.exe", stdout="ffmpeg version 7.1 Copyright\n")
    ffmpeg, caddy, mkv = install.found(runner, tools)
    assert (ffmpeg.own, ffmpeg.version, ffmpeg.own_version) == (False, "7.1", None)
    assert (caddy.own, caddy.version, caddy.own_version) == (True, "2.11.9", "2.11.9")
    assert (mkv.path, mkv.own, mkv.version) == (None, False, "")


def test_removing_vpstreams_copy_leaves_the_rest(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    for top in ("caddy", "mkvtoolnix"):
        (tools / top).mkdir(parents=True)
        (tools / top / install.MARKER).write_text("{}")
    assert install.remove_own(tools, install.MKVTOOLNIX)
    assert not install.remove_own(tools, install.MKVTOOLNIX)  # none left
    assert [p.name for p in tools.iterdir()] == ["caddy"]


# --- what is missing, uninstall


def test_what_is_missing_is_installed_when_nothing_is_named() -> None:
    assert (
        install.missing(FakeRunner(("ffmpeg", "ffprobe", "caddy", "mkvmerge", "mkvextract"))) == []
    )
    assert install.missing(FakeRunner(("ffmpeg", "mkvmerge"))) == ["ffmpeg", "caddy", "mkvtoolnix"]


def test_uninstall_removes_only_what_vpstream_installed(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    for top in ("ffmpeg", "caddy"):
        (tools / top).mkdir(parents=True)
    (tools / "ffmpeg" / install.MARKER).write_text("{}")
    (tools / ".old-ffmpeg").mkdir()
    assert install.uninstall(tools) == [f"ffmpeg ({tools / 'ffmpeg'})"]
    assert [p.name for p in tools.iterdir()] == ["caddy"]  # not marked: not vpstream's
    (tools / "caddy").rmdir()
    assert install.uninstall(tools) == [] and not tools.exists()


def test_the_plan_names_what_is_downloaded(tmp_path: Path) -> None:
    builds = [
        dataclasses.replace(pinned(FFMPEG_ZIP), size=114_768_076),
        pinned(MKV_ZIP, install.MKVTOOLNIX, "103.0"),
    ]
    text = install.plan_text([*builds[:1], dataclasses.replace(builds[1], size=0)], tmp_path)
    assert f"ffmpeg 9.0.2 (114 MB) into {tmp_path / 'ffmpeg'}" in text
    assert f"MKVToolNix 103.0 into {tmp_path / 'mkvtoolnix'}" in text


# --- the desktop icon


def test_powershell_strings_are_taken_as_they_are() -> None:
    assert shortcut.quote("C:\\Users\\O'Brien\\$x") == "'C:\\Users\\O''Brien\\$x'"


def test_vpstream_exe_runs_vpstream_with_pythonw(tmp_path: Path) -> None:
    """So that Windows names vpstream when it asks for administrator rights."""
    pytest.importorskip("pip._vendor.distlib", reason="no pip here")
    exe = shortcut.launcher(tmp_path, Path("C:/Py/pythonw.exe"))
    assert exe == tmp_path / "vpstream.exe"
    data = exe.read_bytes()
    assert (
        data.startswith(b"MZ") and b'#!"' + str(Path("C:/Py/pythonw.exe")).encode() + b'"\n' in data
    )
    with zipfile.ZipFile(exe) as zf:
        assert zf.read("__main__.py").decode() == shortcut.MAIN
    later = (2031, 5, 6, 7, 8, 9, 0, 0, 0)
    with pytest.MonkeyPatch.context() as clock:  # built again at another time: the same
        clock.setattr(zipfile.time, "localtime", lambda *a: later)
        assert shortcut.launcher_bytes(Path("C:/Py/pythonw.exe")) == data
    first = exe.stat().st_mtime_ns
    assert shortcut.launcher(tmp_path, Path("C:/Py/pythonw.exe")) == exe
    assert exe.stat().st_mtime_ns == first  # the same: not written again
    shortcut.launcher(tmp_path, Path("D:/Other/pythonw.exe"))
    assert b"D:" in exe.read_bytes() and not (tmp_path / "vpstream.new").exists()
    assert shortcut.launch(tmp_path) == (exe, "")


def test_without_pip_the_icon_runs_pythonw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shortcut, "launcher_bytes", lambda python: None)
    assert shortcut.launcher(tmp_path) is None
    assert shortcut.launch(tmp_path) == (shortcut.pythonw(), "-P -m vpstream")
    (tmp_path / "vpstream.exe").write_bytes(b"MZ")  # made before: still used
    assert shortcut.launch(tmp_path) == (tmp_path / "vpstream.exe", "")


def test_the_icon_runs_the_window_without_a_console(tmp_path: Path) -> None:
    assert shortcut.pythonw("C:\\Py\\python.exe").name == "pythonw.exe"
    runner = FakeRunner(())
    runner.on("powershell", stdout="C:\\Users\\ana\\Desktop\\vpstream.lnk\r\n")
    assert (
        shortcut.create(runner, Path("C:/Py/pythonw.exe"))
        == "C:\\Users\\ana\\Desktop\\vpstream.lnk"
    )
    script = runner.calls[0][4]
    assert "GetFolderPath('Desktop')" in script and "'vpstream.lnk'" in script
    assert f"$s.TargetPath = {shortcut.quote(Path('C:/Py/pythonw.exe'))}" in script
    assert "$s.Arguments = '-P -m vpstream'" in script
    assert f"{shortcut.ICON},0" in script and shortcut.ICON.is_file()


def test_an_icon_is_kept_up_to_date_only_where_it_is_vpstreams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcut, "launch", lambda: (tmp_path / "vpstream.exe", ""))
    runner = FakeRunner(())
    assert shortcut.refresh(runner) == ""
    script = runner.calls[0][4]
    keep = f"if (-not ((Test-Path -LiteralPath $path) -and {shortcut.IS_OURS})) {{ exit 0 }}"
    assert keep in script and script.index("exit 0") < script.index("$s.Save()")
    assert f"$s.TargetPath = {shortcut.quote(tmp_path / 'vpstream.exe')}" in script
    assert "exit 0" not in shortcut.create_script(Path("p"))


def test_only_vpstreams_icon_is_removed() -> None:
    script = shortcut.REMOVE_SCRIPT
    assert "if ($s.Arguments -eq '-P -m vpstream' -or $s.TargetPath -like " in script
    assert "'*\\vpstream\\vpstream.exe') { Remove-Item" in script
    runner = FakeRunner(())
    assert shortcut.remove(runner) == ""
    runner.on("powershell", returncode=1, stderr="Access denied")
    with pytest.raises(SetupError, match="could not be removed: Access denied"):
        shortcut.remove(runner)


# --- the commands


def test_the_windows_commands_are_only_on_windows(
    on_windows: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(windows, "prepare_process", lambda: None)
    with pytest.raises(SystemExit):
        cli.main(["windows-install", "--help"])
    assert "every one that is missing" in capsys.readouterr().out
    assert "windows-install" not in cli.build_parser().format_help()


def test_the_progress_on_a_terminal_is_a_line_a_tenth(capsys: pytest.CaptureFixture[str]) -> None:
    show = commands.Printed()
    show("unpacking", 0, 0)
    for done in range(0, 101, 5):
        show("downloading caddy", done * 1_000_000, 100_000_000)
    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == [
        "... unpacking",
        "... downloading caddy",
        "... downloading caddy: 10 of 100 MB",
    ]
    assert len(lines) == 12


@pytest.fixture
def faked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    done: list[str] = []
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("vpstream.tools.SystemRunner", lambda: FakeRunner(("mkvmerge",)))
    monkeypatch.setattr(install, "install_build", lambda b, *a: done.append(b.name) or "ok")
    versions = {"ffmpeg": "9.0.2", "caddy": "2.11.9", "mkvtoolnix": "103.0"}
    monkeypatch.setattr(
        install, "latest", lambda tool, http: pinned(b"z", tool, versions[tool.name])
    )
    monkeypatch.setattr(shortcut, "create", lambda runner: done.append("icon") or "D:\\i.lnk")
    return done


def test_install_with_no_program_installs_what_is_missing(
    faked: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[str] = []

    def yes(question: str, given: bool) -> bool:
        asked.append(question)
        return True

    args = argparse.Namespace(programs=[], yes=False)
    assert commands.cmd_install(args, yes) == 0
    assert faked == ["ffmpeg", "caddy", "mkvtoolnix", "icon"]  # mkvextract is not there
    assert "ffmpeg 9.0.2" in asked[0] and "Caddy 2.11.9" in asked[0]
    assert "The desktop icon is at D:\\i.lnk" in capsys.readouterr().out


def test_a_named_program_gets_vpstreams_own_copy_even_when_the_system_has_one(
    faked: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(programs=["mkvtoolnix"], yes=True)
    assert commands.cmd_install(args, lambda q, y: y) == 0
    assert faked == ["mkvtoolnix", "icon"]  # the system's mkvmerge is there


def test_install_refuses_what_it_does_not_know(
    faked: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(programs=["vlc"], yes=True)
    assert commands.cmd_install(args, lambda q, y: y) == 2
    assert "cannot install vlc: choose from ffmpeg, caddy, mkvtoolnix" in capsys.readouterr().err


def test_a_no_installs_nothing(faked: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert commands.cmd_install(argparse.Namespace(programs=[], yes=False), lambda q, y: False) == 1
    assert faked == [] and "nothing was changed" in capsys.readouterr().out


def test_one_failure_does_not_stop_the_rest(
    faked: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(build: install.Build, *a: object) -> str:
        raise SetupError("cannot download it")

    monkeypatch.setattr(install, "install_build", fail)
    args = argparse.Namespace(programs=["ffmpeg", "mkvtoolnix"], yes=True)
    assert commands.cmd_install(args, lambda q, y: y) == 1
    assert "error: ffmpeg: cannot download it" in capsys.readouterr().err
    assert faked == ["icon"]


def test_uninstall_says_what_it_removed_and_what_it_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr("vpstream.tools.SystemRunner", lambda: FakeRunner(()))
    monkeypatch.setattr(shortcut, "remove", lambda runner: "D:\\i.lnk")
    (tmp_path / "vpstream" / "tools" / "caddy").mkdir(parents=True)
    (tmp_path / "vpstream" / "tools" / "caddy" / install.MARKER).write_text("{}")
    assert commands.cmd_uninstall(argparse.Namespace(yes=True), lambda q, y: y) == 0
    out = capsys.readouterr().out
    assert "removed caddy" in out and "removed the desktop icon (D:\\i.lnk)" in out
    assert "programs installed for the whole system" in out
    assert commands.cmd_uninstall(argparse.Namespace(yes=False), lambda q, y: False) == 1


# --- doctor


def test_doctor_says_which_programs_vpstream_installed(tmp_path: Path) -> None:
    from vpstream.platform.windows import doctor

    tools = tmp_path / "tools"
    (tools / "caddy").mkdir(parents=True)
    (tools / "caddy" / install.MARKER).write_text('{"version": "2.10.0"}')
    (tools / "ffmpeg" / "bin").mkdir(parents=True)
    (tools / "ffmpeg" / install.MARKER).write_text('{"version": "9.0.2"}')

    class Found(FakeRunner):
        def which(self, name: str) -> str | None:
            folder = {"caddy": tools / "caddy", "mkvmerge": Path("C:/elsewhere")}
            return str(folder.get(name, tools / "ffmpeg" / "bin") / f"{name}.exe")

    checks = {c.name: c for c in doctor.check_tools(Found(()), "", tools)}
    assert checks["ffmpeg"].status == "ok" and checks["ffmpeg"].detail.endswith(
        "(vpstream's 9.0.2)"
    )
    assert checks["caddy"].status == "ok"  # doctor asks nothing of the network
    assert checks["caddy"].detail.endswith("(vpstream's 2.10.0)")
    assert checks["mkvmerge"].status == "ok" and "vpstream's" not in checks["mkvmerge"].detail


def test_a_program_the_antivirus_holds_for_a_moment_is_moved_once_it_lets_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows Defender reads a new program after it is written and run; meanwhile Windows
    refuses to move its folder ("access is denied"). The move is tried again."""
    tools = tmp_path / "tools"
    staged = tools / ".install-caddy" / "staged"
    (staged / "caddy").mkdir(parents=True)
    (staged / "caddy" / "caddy.exe").write_bytes(b"new")
    real, refusals = os.replace, [2]
    waits: list[float] = []

    def replace(src: Path, dst: Path) -> None:
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(install.os, "replace", replace)
    monkeypatch.setattr(install, "_sleep", waits.append)
    install.swap(tools, staged, "caddy")
    assert (tools / "caddy" / "caddy.exe").read_bytes() == b"new" and len(waits) == 2
    refusals[0] = 10**6  # it never lets go: the error, after a while
    monkeypatch.setattr(install, "SCAN_SECONDS", 0.0)
    (staged / "caddy").mkdir()
    with pytest.raises(PermissionError):
        install.swap(tools, staged, "caddy")


def test_a_program_locked_for_a_moment_is_not_taken_as_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "caddy.exe").write_bytes(b"x")
    looks = iter([True, True, False])
    monkeypatch.setattr(install, "_locked", lambda program: next(looks))
    monkeypatch.setattr(install, "_sleep", lambda s: None)
    assert install.in_use(tmp_path) is None
