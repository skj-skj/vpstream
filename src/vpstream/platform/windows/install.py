"""`vpstream windows-install` and `windows-uninstall`: the programs vpstream needs on Windows.

ffmpeg and Caddy come from their official release pages, at the version pinned here, and
are checked against the SHA-256 pinned with it. They go to %LOCALAPPDATA%\\vpstream\\tools,
which vpstream puts first on its own PATH (and nobody else's): nothing outside that folder
changes, and uninstalling removes the folder. Each one is downloaded, checked, unpacked and
run before it takes the place of the one there, so a failure leaves what was there.

MKVToolNix has only an installer (and a .7z, which Python cannot unpack): winget installs
it, or its own installer pinned here when there is no winget. It goes to Program Files and
stays there when vpstream is uninstalled; doctor and the window find it there.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import shutil
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from vpstream.ffargs import missing_features, parse_ffmpeg_list
from vpstream.platform import windows
from vpstream.platform.common import Http, SetupError
from vpstream.platform.windows.shortcut import powershell, quote
from vpstream.tools import Runner, tail

MARKER = "vpstream-tool.json"  # in each folder vpstream installed
COMMAND_TIMEOUT = 900  # winget or an installer on a slow line

# progress(what is happening, bytes done, bytes in all); 0 in all: no bar, only the words
Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class Build:
    """One pinned download: a zip from the program's release page."""

    name: str  # what the person types: windows-install ffmpeg
    version: str
    url: str
    sha256: str
    size: int  # bytes of the zip
    unpacked: int  # bytes of what is kept from it
    keep: tuple[str, ...]  # the files kept, by name, wherever they are in the zip
    folder: str  # under tools: where they go, the first folder vpstream installed

    @property
    def top(self) -> str:
        return self.folder.split("/")[0]


FFMPEG = Build(
    name="ffmpeg",
    version="9.0.2",
    # gyan.dev's builds, which ffmpeg.org links to for Windows; its GitHub releases have
    # the same files at a fixed address.
    url="https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip",
    sha256="60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
    size=114_768_076,
    unpacked=200_000_000,
    keep=("ffmpeg.exe", "ffprobe.exe", "LICENSE"),
    folder="ffmpeg/bin",
)
CADDY = Build(
    name="caddy",
    version="2.11.9",
    url="https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_windows_amd64.zip",
    sha256="ad93112d335ee687100c4c9f11bced16ca590377976b7fe7883882b9e797e472",
    size=18_384_117,
    unpacked=50_000_000,
    keep=("caddy.exe", "LICENSE"),
    folder="caddy",
)
BUILDS = {b.name: b for b in (FFMPEG, CADDY)}

MKVTOOLNIX = "mkvtoolnix"
WINGET_ID = "MoritzBunkus.MKVToolNix"
MKVTOOLNIX_SETUP = Build(
    name=MKVTOOLNIX,
    version="102.0",
    url="https://mkvtoolnix.download/windows/releases/102.0/mkvtoolnix-64-bit-102.0-setup.exe",
    sha256="aebd61c359457a05629362e46567d75a04ea0ae2797d1ea5af719e94f8fdfbd5",
    size=0,  # not known before it is asked for
    unpacked=150_000_000,
    keep=(),
    folder="",
)
PROGRAMS = (*BUILDS, MKVTOOLNIX)
NEEDS = {"ffmpeg": ("ffmpeg", "ffprobe"), "caddy": ("caddy",), MKVTOOLNIX: ("mkvmerge",)}


def installed_version(tools: Path, build: Build) -> str | None:
    """The version of `build` vpstream installed, or None."""
    try:
        data = json.loads((tools / build.top / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def missing(runner: Runner) -> list[str]:
    """What `windows-install` with no program installs: what is not found."""
    return [name for name in PROGRAMS if not all(runner.which(p) for p in NEEDS[name])]


def sweep(tools: Path) -> None:
    """What an install that was cut off left: half a download, an unpacked copy."""
    if not tools.is_dir():
        return
    for path in tools.iterdir():
        if path.name.startswith("."):
            shutil.rmtree(path, ignore_errors=True)


def _check_space(folder: Path, need: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free < need:
        raise SetupError(
            f"{folder} has {free // 1_000_000} MB free; it needs {need // 1_000_000} MB"
        )


def _download(http: Http, build: Build, dest: Path, progress: Progress) -> None:
    what = f"downloading {build.name} {build.version}"
    progress(what, 0, build.size)
    digest = http.download(build.url, dest, lambda done, total: progress(what, done, total))
    if digest != build.sha256:
        dest.unlink(missing_ok=True)
        raise SetupError(
            f"the download of {build.name} does not match its SHA-256; nothing was changed. "
            f"Try again; if it happens again, the file at {build.url} is not the one vpstream "
            "expects"
        )


def unpack(archive: Path, build: Build, staged: Path) -> None:
    """The files `build` keeps, by name, into `staged`/its folder. Only names are used, so
    nothing lands outside it whatever the zip says."""
    out = staged / build.folder
    out.mkdir(parents=True)
    got = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not info.is_dir() and name in build.keep and name not in got:
                with zf.open(info) as src, (out / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                got.add(name)
    lost = [name for name in build.keep if name.endswith(".exe") and name not in got]
    if lost:
        raise SetupError(f"the download of {build.name} does not have {', '.join(lost)}")


def check_build(runner: Runner, build: Build, folder: Path) -> None:
    """Run what was unpacked before it replaces anything."""

    def run(*args: str) -> str:
        cmd = [str(folder / args[0]), *args[1:]]
        try:
            res = runner.run(cmd, timeout=60)
        except OSError as exc:
            raise SetupError(f"the new {args[0]} does not run here: {exc}") from exc
        if res.returncode != 0:
            raise SetupError(f"the new {args[0]} does not run here: {tail(res)}")
        return res.stdout

    if build.name == FFMPEG.name:
        lists = [
            parse_ffmpeg_list(run("ffmpeg.exe", "-hide_banner", f"-{w}"))
            for w in ("encoders", "filters")
        ]
        lacking = missing_features(*lists)
        if lacking:
            raise SetupError(f"the new ffmpeg lacks {', '.join(lacking)}; nothing was changed")
        run("ffprobe.exe", "-version")
    else:
        run("caddy.exe", "version")


# Windows Defender reads a program that was just written or run, and while it does nothing
# can move it: what fails with "access is denied" is tried again for this long.
SCAN_SECONDS = 30.0
RUNNING_SECONDS = 5.0  # a program that stays locked this long is running, not being read
_sleep = time.sleep


def _retried(action: Callable[[], object], seconds: float | None = None) -> None:
    deadline = time.monotonic() + (SCAN_SECONDS if seconds is None else seconds)
    while True:
        try:
            action()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            _sleep(0.5)


def _locked(program: Path) -> bool:
    try:
        with program.open("r+b"):
            return False
    except PermissionError:
        return True


def in_use(folder: Path, seconds: float | None = None) -> str | None:
    """A program in `folder` that is running, or None. Windows renames a folder with a
    running program in it, and the program goes on running from the new place; but it
    does not open a running program for writing. Opening it so changes nothing. A program
    the antivirus is reading is locked too, for a moment: it is asked again for `seconds`."""
    wait = RUNNING_SECONDS if seconds is None else seconds
    for program in sorted(folder.rglob("*.exe")) if folder.is_dir() else ():
        deadline = time.monotonic() + wait
        while _locked(program):
            if time.monotonic() >= deadline:
                return program.name
            _sleep(0.5)
    return None


def _not_in_use(folder: Path, name: str) -> None:
    program = in_use(folder)
    if program:
        raise SetupError(
            f"{program} is running: stop the stream and close {windows.WINDOW}, then try again"
            f" ({name} was left as it was)"
        )


def swap(tools: Path, staged: Path, top: str) -> None:
    """Put `staged`/`top` in the place of tools/`top`, unless a program there is running."""
    target, old = tools / top, tools / f".old-{top}"
    _not_in_use(target, top)
    if target.exists():
        _retried(lambda: os.replace(target, old))
    try:
        _retried(lambda: os.replace(staged / top, target))
    except OSError:
        if old.exists():
            os.replace(old, target)  # what was there, back
        raise
    shutil.rmtree(old, ignore_errors=True)


def install_build(build: Build, tools: Path, runner: Runner, http: Http, progress: Progress) -> str:
    """Download, check and install one pinned build. Returns what was done."""
    sweep(tools)
    _check_space(tools, build.size + build.unpacked)
    work = tools / f".install-{build.name}"
    work.mkdir(parents=True)
    try:
        archive = work / build.url.rsplit("/", 1)[1]
        _download(http, build, archive, progress)
        progress(f"unpacking {build.name}", 0, 0)
        staged = work / "staged"
        unpack(archive, build, staged)
        archive.unlink()
        progress(f"checking the new {build.name}", 0, 0)
        check_build(runner, build, staged / build.folder)
        marker = {
            "version": build.version,
            "url": build.url,
            "sha256": build.sha256,
            "installed": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (staged / build.top / MARKER).write_text(json.dumps(marker, indent=2) + "\n", "utf-8")
        swap(tools, staged, build.top)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return f"{build.name} {build.version} is installed in {tools / build.top}"


def install_mkvtoolnix(runner: Runner, http: Http, tools: Path, progress: Progress) -> str:
    """MKVToolNix by winget, or by its own installer (pinned) when there is no winget. Both
    ask Windows for administrator rights unless this already has them."""
    if runner.which("winget"):
        progress("installing MKVToolNix with winget (Windows may ask to allow it)", 0, 0)
        res = runner.run(
            [
                "winget", "install", "--id", WINGET_ID, "--exact", "--silent",
                "--accept-package-agreements", "--accept-source-agreements",
                "--disable-interactivity",
            ],
            timeout=COMMAND_TIMEOUT,
        )  # fmt: skip
        if res.returncode != 0:
            raise SetupError(f"winget could not install MKVToolNix: {tail(res, stdout=True)}")
        return "MKVToolNix is installed (by winget)"
    sweep(tools)
    _check_space(tools, MKVTOOLNIX_SETUP.unpacked)
    work = tools / ".install-mkvtoolnix"
    work.mkdir(parents=True)
    try:
        setup = work / "mkvtoolnix-setup.exe"
        _download(http, MKVTOOLNIX_SETUP, setup, progress)
        progress("installing MKVToolNix (Windows may ask to allow it)", 0, 0)
        # Its installer needs administrator rights: Start-Process asks Windows for them.
        script = (
            f"exit (Start-Process -FilePath {quote(setup)} -ArgumentList '/S' -Verb RunAs "
            "-Wait -PassThru).ExitCode"
        )
        res = runner.run(powershell(script), timeout=COMMAND_TIMEOUT)
        if res.returncode != 0:
            raise SetupError(f"the MKVToolNix installer failed: {tail(res)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return f"MKVToolNix {MKVTOOLNIX_SETUP.version} is installed"


def uninstall(tools: Path) -> list[str]:
    """Remove the programs vpstream installed. Returns what was removed."""
    removed = []
    sweep(tools)
    for build in BUILDS.values():
        folder = tools / build.top
        if (folder / MARKER).exists():
            _not_in_use(folder, build.name)
            _retried(functools.partial(shutil.rmtree, folder))
            removed.append(f"{build.name} ({folder})")
    with contextlib.suppress(OSError):
        tools.rmdir()  # only when nothing else is in it
    return removed


def plan_text(names: Sequence[str], tools: Path) -> str:
    """What an install of `names` downloads, for the question before it."""
    lines = []
    for name in names:
        build = BUILDS.get(name)
        if build:
            lines.append(
                f"  {name} {build.version} ({build.size // 1_000_000} MB) into {tools / build.top}"
            )
        else:
            lines.append("  MKVToolNix, with winget or its installer, into Program Files")
    return "\n".join(lines)
