"""`vpstream windows-install` and `windows-uninstall`, and the window's Tools tab: the
programs vpstream needs on Windows.

ffmpeg, Caddy and MKVToolNix come from each one's own release page, at its latest release
(`latest`): Caddy's and gyan.dev's ffmpeg GitHub releases, which give each file's SHA-256,
and MKVToolNix's portable zip with the SHA-256 published next to it. Each download is
checked against that SHA-256, unpacked, and run before it takes the place of the one there,
so a failure leaves what was there. They go to %LOCALAPPDATA%\\vpstream\\tools, which
vpstream puts first on its own PATH (and nobody else's): nothing outside that folder
changes, and uninstalling removes the folder. A program installed for the whole system (in
Program Files, on PATH) is used when vpstream has no copy of its own.
"""

from __future__ import annotations

import contextlib
import functools
import http.client as http_client
import json
import ntpath
import os
import re
import shutil
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vpstream.ffargs import missing_features, parse_ffmpeg_list
from vpstream.platform import windows
from vpstream.platform.common import Http, SetupError
from vpstream.tools import Runner, tail

MARKER = "vpstream-tool.json"  # in each folder vpstream installed
VERSION = re.compile(r"\d+(?:\.\d+){0,3}")  # what a release may call itself: 9.0.2, 103.0

# progress(what is happening, bytes done, bytes in all); 0 in all: no bar, only the words
Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class Tool:
    """A program vpstream can install: what it is called, what is kept of its zip."""

    name: str  # what the person types: windows-install ffmpeg
    title: str  # what the window shows
    programs: tuple[str, ...]  # what must be found for it to be there; the first is run
    keep: tuple[str, ...]  # the files kept, by name, wherever they are in the zip
    folder: str  # under tools: where they go, the first folder vpstream installed
    unpacked: int  # bytes of what is kept, with room to spare

    @property
    def top(self) -> str:
        return self.folder.split("/")[0]


FFMPEG = Tool(
    "ffmpeg", "ffmpeg", ("ffmpeg", "ffprobe"),
    ("ffmpeg.exe", "ffprobe.exe", "LICENSE"), "ffmpeg/bin", 200_000_000,
)  # fmt: skip
CADDY = Tool("caddy", "Caddy", ("caddy",), ("caddy.exe", "LICENSE"), "caddy", 50_000_000)
MKVTOOLNIX = Tool(
    "mkvtoolnix", "MKVToolNix", ("mkvmerge", "mkvextract"),
    ("mkvmerge.exe", "mkvextract.exe", "COPYING.txt"), "mkvtoolnix", 120_000_000,
)  # fmt: skip
TOOLS = {t.name: t for t in (FFMPEG, CADDY, MKVTOOLNIX)}
PROGRAMS = tuple(TOOLS)

# Where each one says what its latest release is.
GITHUB = {"ffmpeg": "GyanD/codexffmpeg", "caddy": "caddyserver/caddy"}
GITHUB_ASSET = {
    "ffmpeg": "ffmpeg-{v}-essentials_build.zip",  # gyan.dev's, which ffmpeg.org links to
    "caddy": "caddy_{v}_windows_amd64.zip",
}
MKVTOOLNIX_LATEST = "https://mkvtoolnix.download/latest-release.xml"
MKVTOOLNIX_ZIP = "https://mkvtoolnix.download/windows/releases/{v}/mkvtoolnix-64-bit-{v}.zip"


@dataclass(frozen=True)
class Build:
    """One release of a tool: the zip to download and the SHA-256 it must have."""

    tool: Tool
    version: str
    url: str
    sha256: str
    size: int = 0  # bytes of the zip; 0 when not known before the download

    @property
    def name(self) -> str:
        return self.tool.name


def _version(found: object, what: str) -> str:
    text = str(found or "").strip().removeprefix("v")
    if not VERSION.fullmatch(text):
        raise SetupError(f"{what} does not name a version vpstream understands: {found!r}")
    return text


def latest(tool: Tool, http: Http) -> Build:
    """The latest release of `tool`, as its own release page says. Raises SetupError, also
    for a page that is not what it should be (a portal's, a cut-off one)."""
    try:
        build = _latest(tool, http)
    except SetupError:
        raise
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OSError,
        http_client.HTTPException,
    ) as exc:
        raise SetupError(f"{tool.title}'s latest release cannot be read: {exc!r}") from exc
    if not build.url.startswith("https://"):
        raise SetupError(f"{tool.title} {build.version} is not offered over https: {build.url}")
    return build


def _latest(tool: Tool, http: Http) -> Build:
    if tool.name in GITHUB:
        api = f"https://api.github.com/repos/{GITHUB[tool.name]}/releases/latest"
        release = http.get_json(api)
        version = _version(release.get("tag_name"), f"{tool.title}'s latest release")
        wanted = GITHUB_ASSET[tool.name].format(v=version)
        asset = next((a for a in release.get("assets") or [] if a.get("name") == wanted), None)
        digest = str((asset or {}).get("digest") or "")
        if asset is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise SetupError(f"{tool.title} {version} has no {wanted} with a SHA-256 on GitHub")
        return Build(
            tool, version, str(asset["browser_download_url"]), digest[7:], int(asset["size"])
        )
    feed = http.get_text(MKVTOOLNIX_LATEST)
    source = re.search(r"<latest-source>(.*?)</latest-source>", feed, re.S)
    found = re.search(r"<version>([^<]*)</version>", source[1]) if source else None
    version = _version(found[1] if found else "", f"{tool.title}'s latest release")
    url = MKVTOOLNIX_ZIP.format(v=version)
    sums = re.match(r"\s*([0-9a-fA-F]{64})\b", http.get_text(url + ".sha256"))
    if sums is None:
        raise SetupError(f"{tool.title} {version} has no SHA-256 next to {url}")
    return Build(tool, version, url, sums[1].lower())


def installed_version(tools: Path, tool: Tool) -> str | None:
    """The version of `tool` vpstream installed, or None."""
    try:
        data = json.loads((tools / tool.top / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


@dataclass(frozen=True)
class Found:
    """Where a tool is found now, for the Tools tab: vpstream's own copy comes first."""

    tool: Tool
    path: str | None  # its first program, as vpstream finds it; None when missing
    own: bool  # that is vpstream's copy
    version: str  # what that program says it is; "" when it does not say
    own_version: str | None  # vpstream's copy, if it has one


def program_version(runner: Runner, path: str) -> str:
    """The version a program says it is (`ffmpeg -version`, `caddy version`, `mkvmerge
    --version`); "" when it does not say or does not run."""
    name = ntpath.basename(path).lower().removesuffix(".exe")
    args = {"ffmpeg": ["-version"], "caddy": ["version"]}.get(name, ["--version"])
    try:
        res = runner.run([path, *args], timeout=20)
    except OSError:
        return ""
    first = (res.stdout or "").strip().splitlines()[:1]
    # 9.0.2-essentials, v2.11.9, v103.0, n7.1 (a release build named after its git tag)
    found = re.search(r"(?<![\w.])[vn]?(\d+\.\d+(?:\.\d+)?)", first[0]) if first else None
    return found[1] if found and res.returncode == 0 else ""


def found(runner: Runner, tools: Path) -> list[Found]:
    """Each tool as vpstream finds it now. Runs each one found: not on the window's thread."""
    rows = []
    for tool in TOOLS.values():
        path = runner.which(tool.programs[0])
        top = ntpath.normcase(str(tools / tool.top)).rstrip("\\")
        folder = ntpath.normcase(ntpath.dirname(path or ""))
        own = bool(path) and (folder == top or folder.startswith(top + "\\"))
        version = program_version(runner, path) if path else ""
        rows.append(Found(tool, path, own, version, installed_version(tools, tool)))
    return rows


def missing(runner: Runner) -> list[str]:
    """What `windows-install` with no program installs: what is not found."""
    return [t.name for t in TOOLS.values() if not all(runner.which(p) for p in t.programs)]


# One install or removal of a tool at a time, whoever asks (the window's Setup box, its
# Tools tab, which installs several tools at once, the command line): a second is refused.
_BUSY = {name: threading.Lock() for name in TOOLS}


@contextlib.contextmanager
def _only_one(tool: Tool) -> Any:
    if not _BUSY[tool.name].acquire(blocking=False):
        raise SetupError(f"{tool.title} is being installed or removed already")
    try:
        yield
    finally:
        _BUSY[tool.name].release()


def sweep(tools: Path) -> None:
    """What an install that was cut off left: half a download, an unpacked copy; but not
    what an install under way uses now. A copy that a swap cut off had moved aside comes
    back (it is the one that worked)."""
    if not tools.is_dir():
        return
    for tool in TOOLS.values():
        old, top = tools / f".old-{tool.top}", tools / tool.top
        if old.is_dir() and not top.exists() and not _BUSY[tool.name].locked():
            with contextlib.suppress(OSError):
                os.replace(old, top)
    under_way = {f".{what}-{t.name if what == 'install' else t.top}"
                 for t in TOOLS.values() if _BUSY[t.name].locked()
                 for what in ("install", "old")}  # fmt: skip
    for path in tools.iterdir():
        if path.name.startswith(".") and path.name not in under_way:
            shutil.rmtree(path, ignore_errors=True)


def _check_space(folder: Path, need: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free
    if free < need:
        raise SetupError(
            f"{folder} has {free // 1_000_000} MB free; it needs {need // 1_000_000} MB"
        )


def _download(http: Http, build: Build, dest: Path, progress: Progress) -> None:
    what = f"downloading {build.tool.title} {build.version}"
    progress(what, 0, build.size)
    digest = http.download(build.url, dest, lambda done, total: progress(what, done, total))
    if digest != build.sha256:
        dest.unlink(missing_ok=True)
        raise SetupError(
            f"the download of {build.tool.title} does not match its SHA-256; nothing was changed."
            f" Try again; if it happens again, the file at {build.url} is not the one its "
            "release page names"
        )


def unpack(archive: Path, tool: Tool, staged: Path) -> None:
    """The files `tool` keeps, by name, into `staged`/its folder. Only names are used, so
    nothing lands outside it whatever the zip says."""
    out = staged / tool.folder
    out.mkdir(parents=True)
    got = set()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if not info.is_dir() and name in tool.keep and name not in got:
                with zf.open(info) as src, (out / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                got.add(name)
    lost = [name for name in tool.keep if name.endswith(".exe") and name not in got]
    if lost:
        raise SetupError(f"the download of {tool.title} does not have {', '.join(lost)}")


def check_build(runner: Runner, tool: Tool, folder: Path) -> None:
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

    if tool is FFMPEG:
        lists = [
            parse_ffmpeg_list(run("ffmpeg.exe", "-hide_banner", f"-{w}"))
            for w in ("encoders", "filters")
        ]
        lacking = missing_features(*lists)
        if lacking:
            raise SetupError(f"the new ffmpeg lacks {', '.join(lacking)}; nothing was changed")
        run("ffprobe.exe", "-version")
    elif tool is CADDY:
        run("caddy.exe", "version")
    else:
        run("mkvmerge.exe", "--version")
        run("mkvextract.exe", "--version")


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
    """Download, check and install one release. Returns what was done."""
    with _only_one(build.tool):
        return _install(build, tools, runner, http, progress)


def _install(build: Build, tools: Path, runner: Runner, http: Http, progress: Progress) -> str:
    tool = build.tool
    sweep(tools)
    _check_space(tools, (build.size or tool.unpacked) + tool.unpacked)  # size 0: not known
    work = tools / f".install-{tool.name}"
    try:
        work.mkdir(parents=True)
    except FileExistsError:  # in use: another vpstream (the window, a terminal) installs it
        raise SetupError(
            f"{tool.title} is being installed by another vpstream already ({work} is in use)"
        ) from None
    try:
        archive = work / "download.zip"  # not the URL's name: nothing it says lands elsewhere
        _download(http, build, archive, progress)
        progress(f"unpacking {tool.title}", 0, 0)
        staged = work / "staged"
        unpack(archive, tool, staged)
        archive.unlink()
        progress(f"checking the new {tool.title}", 0, 0)
        check_build(runner, tool, staged / tool.folder)
        marker = {
            "version": build.version,
            "url": build.url,
            "sha256": build.sha256,
            "installed": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (staged / tool.top / MARKER).write_text(json.dumps(marker, indent=2) + "\n", "utf-8")
        swap(tools, staged, tool.top)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return f"{tool.title} {build.version} is installed in {tools / tool.top}"


def remove_own(tools: Path, tool: Tool) -> bool:
    """Remove vpstream's copy of `tool` (one installed for the system is used after);
    False when there is none."""
    with _only_one(tool):
        sweep(tools)
        folder = tools / tool.top
        if not (folder / MARKER).exists():
            return False
        _not_in_use(folder, tool.title)
        _retried(functools.partial(shutil.rmtree, folder))
        return True


def uninstall(tools: Path) -> list[str]:
    """Remove the programs vpstream installed. Returns what was removed."""
    removed = []
    sweep(tools)
    for tool in TOOLS.values():
        if remove_own(tools, tool):
            removed.append(f"{tool.name} ({tools / tool.top})")
    with contextlib.suppress(OSError):
        tools.rmdir()  # only when nothing else is in it
    return removed


def plan_text(builds: Sequence[Build], tools: Path) -> str:
    """What an install of `builds` downloads, for the question before it."""
    lines = []
    for build in builds:
        size = f" ({build.size // 1_000_000} MB)" if build.size else ""
        lines.append(f"  {build.tool.title} {build.version}{size} into {tools / build.tool.top}")
    return "\n".join(lines)
