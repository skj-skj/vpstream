"""Install or update ffmpeg from FFmpeg-Builds, the Linux build that ffmpeg.org links to.

The newest stable release branch is used (nX.Y, never the master snapshot), so a new major
version is picked up with no change here. ffmpeg and ffprobe go to /opt/vpstream-ffmpeg and
are linked from /usr/local/bin, which comes before /usr/bin on PATH. System changes run
through sudo; everything is downloaded, checked and tested first, as the user.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vpstream import __version__
from vpstream.ffargs import parse_ffmpeg_list
from vpstream.tools import Runner, tail

REPO = "FFmpeg-Builds/FFmpeg-Builds"  # formerly BtbN/FFmpeg-Builds
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
PROGRAMS = ("ffmpeg", "ffprobe")
MARKER = "vpstream-build.json"
ARCHES = {"x86_64": "linux64", "amd64": "linux64", "aarch64": "linuxarm64", "arm64": "linuxarm64"}
# The builds link the C library dynamically.
MIN_GLIBC = (2, 28)
# libgcc_s is the other library they link; libass reads /etc/fonts and needs fonts. Burned-in
# subtitles fall back to these when a file does not carry its font: Liberation stands in for
# Arial and Times New Roman, Noto CJK draws Chinese, Japanese and Korean.
APT_PACKAGES = (
    "libgcc-s1",
    "fontconfig",
    "fonts-dejavu-core",
    "fonts-liberation",
    "fonts-noto-cjk",
)
# What bench, caps and prepare use; each tuple needs one of its names.
REQUIRED_ENCODERS = (("libx264",), ("libx265",), ("libsvtav1", "libaom-av1"))
REQUIRED_FILTERS = ("overlay", "scale2ref", "subtitles", "tonemap", "zscale")
INSTALLED_BYTES = 300_000_000  # ffmpeg and ffprobe, unpacked
COMMAND_TIMEOUT = 900  # apt-get on a slow mirror
# No questions: commands run without a terminal on stdin.
APT_GET = ("env", "DEBIAN_FRONTEND=noninteractive", "apt-get")

_ASSET = re.compile(
    r"^ffmpeg-(?P<version>n(?P<major>\d+)\.(?P<minor>\d+)(?:\.\d+)?(?:-\d+-g[0-9a-f]+)?)"
    r"-(?P<arch>linux64|linuxarm64)-gpl-(?P=major)\.(?P=minor)\.tar\.xz$"
)
_VERSION = re.compile(r"\bn(\d+)\.(\d+)(?:\.(\d+))?(?:-(\d+)-g[0-9a-f]+)?")


class SetupError(RuntimeError):
    pass


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Layout:
    install_dir: Path = Path("/opt/vpstream-ffmpeg")
    bin_dir: Path = Path("/usr/local/bin")


def version_key(version: str) -> tuple[int, ...] | None:
    """(major, minor, patch, commits since the tag) from a version such as n9.0.2-48-g1a2b."""
    m = _VERSION.search(version)
    return tuple(int(g or 0) for g in m.groups()) if m else None


@dataclass(frozen=True)
class Build:
    asset: str
    url: str
    size: int
    checksums_url: str
    version: str  # n9.0.2-48-gd4070a14ce

    @property
    def key(self) -> tuple[int, ...]:
        return version_key(self.version) or ()


def latest_build(releases: Sequence[dict[str, Any]], arch: str) -> Build:
    """The newest stable build for `arch`, from the GitHub releases list. Pure, so tested.

    From the newest dated autobuild, the highest nX.Y branch. Master snapshots (N-...),
    lgpl and shared builds never match.
    """
    dated = [r for r in releases if str(r.get("tag_name", "")).startswith("autobuild-")]
    dated.sort(key=lambda r: str(r.get("published_at") or ""), reverse=True)
    for rel in dated:
        if rel.get("draft"):
            continue
        assets = [a for a in rel.get("assets") or [] if isinstance(a, dict)]
        sums = next((a for a in assets if a.get("name") == "checksums.sha256"), None)
        builds = []
        for a in assets:
            m = _ASSET.match(str(a.get("name", "")))
            if m and m["arch"] == arch:
                builds.append(
                    Build(
                        asset=m.group(0),
                        url=str(a.get("browser_download_url", "")),
                        size=int(a.get("size") or 0),
                        checksums_url=str((sums or {}).get("browser_download_url", "")),
                        version=m["version"],
                    )
                )
        if builds and sums:
            return max(builds, key=lambda b: b.key)
    raise SetupError(f"no stable {arch} ffmpeg build found in the {REPO} releases")


def parse_checksums(text: str) -> dict[str, str]:
    """{file name: sha256} from a sha256sum listing."""
    sums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            sums[parts[1].lstrip("*")] = parts[0]
    return sums


def missing_features(encoders: set[str], filters: set[str]) -> list[str]:
    missing = [" or ".join(alts) for alts in REQUIRED_ENCODERS if not set(alts) & encoders]
    return missing + [f for f in REQUIRED_FILTERS if f not in filters]


def glibc_problem(confstr: str | None) -> str | None:
    """Why the C library cannot run the builds, from os.confstr("CS_GNU_LIBC_VERSION")."""
    m = re.fullmatch(r"glibc (\d+)\.(\d+).*", confstr or "")
    if not m:
        return "the builds need glibc, and this system does not use it"
    if (int(m[1]), int(m[2])) < MIN_GLIBC:
        need = ".".join(map(str, MIN_GLIBC))
        return f"the builds need glibc {need} or newer; this system has {m[1]}.{m[2]}"
    return None


# --- what is installed now


@dataclass(frozen=True)
class Existing:
    program: str
    path: str  # as found on PATH
    owner: str  # "vpstream", "apt", "snap" or "other"
    package: str = ""  # apt package or snap name
    version: str = ""

    def describe(self) -> str:
        where = {
            "vpstream": "installed by vpstream",
            "apt": f"apt package {self.package}",
            "snap": f"snap {self.package}",
            "other": "not from apt, snap or vpstream",
        }[self.owner]
        ver = f" {self.version}" if self.version else ""
        return f"{self.program}{ver} at {self.path}, {where}"


def read_marker(layout: Layout) -> dict[str, Any]:
    try:
        data = json.loads((layout.install_dir / MARKER).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _program_version(runner: Runner, path: str) -> str:
    try:
        res = runner.run([path, "-version"], timeout=30)
    except OSError:
        return ""
    m = re.match(r"\S+ version (\S+)", res.stdout)
    return m[1] if m else ""


def find_existing(runner: Runner, layout: Layout) -> list[Existing]:
    """The ffmpeg and ffprobe that PATH finds now, and who installed them."""
    found = []
    for prog in PROGRAMS:
        path = runner.which(prog)
        if not path:
            continue
        real = os.path.realpath(path)
        if Path(real).is_relative_to(layout.install_dir):
            version = str(read_marker(layout).get("version") or "")
            found.append(Existing(prog, path, "vpstream", version=version))
            continue
        version = _program_version(runner, path)
        if path.startswith("/snap/"):
            name = Path(path).name.split(".")[0]
            found.append(Existing(prog, path, "snap", name, version))
            continue
        package = ""
        if runner.which("dpkg-query"):
            for candidate in dict.fromkeys((real, path)):
                res = runner.run(["dpkg-query", "-S", candidate], timeout=30)
                if res.returncode == 0 and ": " in res.stdout:
                    package = res.stdout.split(": ", 1)[0].split(",")[0].strip()
                    break
        owner = "apt" if package else "other"
        found.append(Existing(prog, path, owner, package, version))
    return found


def apt_removals(runner: Runner, packages: Sequence[str]) -> list[str]:
    """Every package that `apt-get remove` of `packages` would remove (a dry run)."""
    res = runner.run(["apt-get", "-s", "remove", *packages], timeout=120)
    if res.returncode != 0:
        raise SetupError(f"apt-get cannot remove {' '.join(packages)}: {tail(res)}")
    return [line.split()[1] for line in res.stdout.splitlines() if line.startswith("Remv ")]


# --- network


class Http(Protocol):
    def get_json(self, url: str) -> Any: ...

    def get_text(self, url: str) -> str: ...

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        """Save `url` to `dest` and return its sha256."""
        ...


class UrlHttp:
    timeout = 60

    def _open(self, url: str, accept: str = "*/*") -> Any:
        headers = {"User-Agent": f"vpstream/{__version__}", "Accept": accept}
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=self.timeout
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                msg = "GitHub's API limit for this IP is used up; try again in an hour"
                raise SetupError(msg) from exc
            raise SetupError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SetupError(f"cannot download {url}: {reason}") from exc

    def get_json(self, url: str) -> Any:
        with self._open(url, "application/vnd.github+json") as resp:
            return json.load(resp)

    def get_text(self, url: str) -> str:
        with self._open(url) as resp:
            return str(resp.read().decode("utf-8", errors="replace"))

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        digest = hashlib.sha256()
        with self._open(url) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                progress(done, total)
        return digest.hexdigest()


# --- the install itself


@dataclass
class Setup:
    runner: Runner
    http: Http
    confirm: Callable[[str], bool]
    progress: Callable[[str], None]
    layout: Layout = Layout()
    tmp_dir: str | None = None
    is_root: bool | None = None
    system: str | None = None  # sys.platform, platform.machine() and the C library,
    machine: str | None = None  # given by tests
    libc: str | None = None

    def _sudo(self) -> list[str]:
        root = os.geteuid() == 0 if self.is_root is None else self.is_root
        if root:
            return []
        if not self.runner.which("sudo"):
            raise SetupError("installing needs root: run it as root, or install sudo")
        return ["sudo"]

    def _system(self, *args: str | Path, what: str) -> None:
        cmd = [*self._sudo(), *map(str, args)]
        res = self.runner.run(cmd, timeout=COMMAND_TIMEOUT)
        if res.returncode != 0:
            raise SetupError(f"{what} failed ({' '.join(cmd)}): {tail(res, stdout=True)}")

    def _arch(self) -> str:
        if not (self.system or sys.platform).startswith("linux"):
            raise SetupError("vpstream installs ffmpeg on Linux only")
        machine = (self.machine or platform.machine()).lower()
        arch = ARCHES.get(machine)
        if not arch:
            raise SetupError(f"no ffmpeg build for this processor ({machine})")
        libc = self.libc if self.libc is not None else _confstr()
        problem = glibc_problem(libc)
        if problem:
            raise SetupError(problem)
        return arch

    def run(self, *, update: bool) -> str:
        """Install (or update) ffmpeg. Returns what was done; raises Cancelled or SetupError."""
        arch = self._arch()
        existing = find_existing(self.runner, self.layout)
        ours = [e for e in existing if e.owner == "vpstream"]
        if ours and not update:
            self._install_packages()
            version = f"{ours[0].version} " if ours[0].version else ""
            return (
                f"ffmpeg {version}is already installed by vpstream. "
                "For a newer build: vpstream update ffmpeg"
            )
        other = [e for e in existing if e.owner == "other"]
        if other:
            raise SetupError(
                f"{other[0].describe()}. vpstream cannot uninstall it: remove it yourself, "
                "then run this again"
            )
        self._check_links()

        self.progress(f"looking up the newest stable ffmpeg build ({REPO})")
        build = latest_build(self.http.get_json(RELEASES_API), arch)
        current = version_key(ours[0].version) if ours else None
        if current and current >= build.key:
            self._install_packages()
            return f"ffmpeg {ours[0].version} is already the newest stable build"

        removals = self._removals(existing)
        if existing:
            if ours:
                desc = f"{ours[0].version or 'unknown version'} installed by vpstream"
            else:
                desc = "; ".join(e.describe() for e in existing)
            extra = [p for p in removals if p not in {e.package for e in existing}]
            if extra:
                desc += f"; apt would also remove {', '.join(extra)}"
            question = (
                f"This will uninstall the existing ffmpeg ({desc}) and install {build.version}. "
                "Do you want to proceed?"
            )
            if not self.confirm(question):
                raise Cancelled
        self._install_packages()
        with tempfile.TemporaryDirectory(prefix="vpstream-ffmpeg-", dir=self.tmp_dir) as tmp:
            staged = self._fetch(build, Path(tmp))
            self._check_build(staged)
            self._place(staged)
        self._uninstall([e for e in existing if e.owner != "vpstream"], removals)
        return self._report(build)

    def _check_links(self) -> None:
        """Refuse to overwrite a program in bin_dir that is not our link."""
        for prog in PROGRAMS:
            link = self.layout.bin_dir / prog
            if link.exists() or link.is_symlink():
                target = Path(os.path.realpath(link))
                if not target.is_relative_to(self.layout.install_dir):
                    raise SetupError(
                        f"{link} exists and was not installed by vpstream: remove it yourself, "
                        "then run this again"
                    )

    def _removals(self, existing: Sequence[Existing]) -> list[str]:
        packages = list(dict.fromkeys(e.package for e in existing if e.owner == "apt"))
        return apt_removals(self.runner, packages) if packages else []

    def _install_packages(self) -> None:
        if not self.runner.which("apt-get"):
            if not Path("/etc/fonts/fonts.conf").is_file():
                self.progress("note: no /etc/fonts/fonts.conf; install fontconfig and a font")
            return
        missing = []
        for pkg in APT_PACKAGES:
            res = self.runner.run(["dpkg-query", "-W", "-f=${Status}", pkg], timeout=30)
            if "install ok installed" not in res.stdout:
                missing.append(pkg)
        if missing:
            self.progress(f"installing {', '.join(missing)}")
            self._system(
                *APT_GET, "install", "-y", "--no-install-recommends", *missing,
                what="installing the libraries and fonts ffmpeg needs",
            )  # fmt: skip

    def _check_space(self, build: Build, tmp: Path) -> None:
        free = shutil.disk_usage(tmp).free
        if free < build.size + INSTALLED_BYTES:
            need = (build.size + INSTALLED_BYTES) // 1_000_000
            raise SetupError(
                f"{tmp} has {free // 1_000_000} MB free, the download needs {need} MB; "
                "pass --tmp with a folder on a bigger disk"
            )
        parent = self.layout.install_dir.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        free = shutil.disk_usage(parent).free
        if free < INSTALLED_BYTES:
            raise SetupError(
                f"{parent} has {free // 1_000_000} MB free; ffmpeg needs "
                f"{INSTALLED_BYTES // 1_000_000} MB"
            )

    def _fetch(self, build: Build, tmp: Path) -> Path:
        self._check_space(build, tmp)
        sums = parse_checksums(self.http.get_text(build.checksums_url))
        if build.asset not in sums:
            raise SetupError(f"{build.asset} is not in the release's checksums")
        archive = tmp / build.asset
        step = [0]

        def report(done: int, total: int) -> None:
            if total and done * 10 // total > step[0]:
                step[0] = done * 10 // total
                self.progress(f"downloaded {done // 1_000_000} of {total // 1_000_000} MB")

        self.progress(f"downloading {build.asset} ({build.size // 1_000_000} MB)")
        digest = self.http.download(build.url, archive, report)
        if digest != sums[build.asset]:
            raise SetupError(f"{build.asset} does not match its sha256; nothing was changed")
        self.progress("unpacking")
        staged = tmp / "staged"
        wanted = {f"bin/{p}" for p in PROGRAMS} | {"LICENSE.txt"}
        got = set()
        # One pass: seeking in an xz stream decompresses it again from the start.
        with tarfile.open(archive, "r|xz") as tar:
            for member in tar:
                name = member.name.split("/", 1)[-1]  # without the top folder
                if member.isfile() and name in wanted:
                    member.name = name
                    tar.extract(member, staged, filter="data")
                    got.add(name)
        archive.unlink()
        if not {f"bin/{p}" for p in PROGRAMS} <= got:
            raise SetupError(f"{build.asset} does not contain ffmpeg and ffprobe")
        marker = {
            "version": build.version,
            "asset": build.asset,
            "sha256": digest,
            "url": build.url,
            "installed": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (staged / MARKER).write_text(json.dumps(marker, indent=2) + "\n")
        return staged

    def _check_build(self, staged: Path) -> None:
        """Run the new ffmpeg before anything on the system changes."""
        ffmpeg = str(staged / "bin" / "ffmpeg")
        lists = {}
        for what in ("encoders", "filters"):
            try:
                res = self.runner.run([ffmpeg, "-hide_banner", f"-{what}"], timeout=60)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError) and Path(ffmpeg).is_file():
                    # The file is there; its program loader is not.
                    need = ".".join(map(str, MIN_GLIBC))
                    why = f"its program loader is missing (it needs glibc {need} or newer)"
                else:
                    why = str(exc)
                why += self._ldd(ffmpeg)
                raise SetupError(f"the new ffmpeg does not run here: {why}") from exc
            if res.returncode != 0:
                raise SetupError(f"the new ffmpeg does not run here: {tail(res)}")
            lists[what] = parse_ffmpeg_list(res.stdout)
        missing = missing_features(lists["encoders"], lists["filters"])
        if missing:
            raise SetupError(f"the new ffmpeg lacks {', '.join(missing)}; nothing was changed")
        try:
            res = self.runner.run([str(staged / "bin" / "ffprobe"), "-version"], timeout=60)
        except OSError as exc:
            raise SetupError(f"the new ffprobe does not run here: {exc}") from exc
        if res.returncode != 0:
            raise SetupError(f"the new ffprobe does not run here: {tail(res)}")

    def _ldd(self, path: str) -> str:
        if not self.runner.which("ldd"):
            return ""
        res = self.runner.run(["ldd", path], timeout=30)
        lost = [line.strip() for line in res.stdout.splitlines() if "not found" in line]
        return f" (missing: {'; '.join(lost)})" if lost else ""

    def _place(self, staged: Path) -> None:
        target = self.layout.install_dir
        new = target.with_name(target.name + ".new")
        self.progress(f"installing to {target}")
        self._system("mkdir", "-p", target.parent, what="creating the install folder")
        self._system("rm", "-rf", new, what="clearing an old partial install")
        self._system("cp", "-R", staged, new, what="copying ffmpeg")
        self._system("chmod", "-R", "u+rwX,go+rX,go-w", new, what="setting permissions")
        self._system("rm", "-rf", target, what="removing the previous vpstream ffmpeg")
        self._system("mv", new, target, what="moving ffmpeg into place")
        self._system("mkdir", "-p", self.layout.bin_dir, what="creating the bin folder")
        for prog in PROGRAMS:
            self._system(
                "ln", "-sfn", target / "bin" / prog, self.layout.bin_dir / prog,
                what=f"linking {prog}",
            )  # fmt: skip

    def _uninstall(self, old: Sequence[Existing], removals: Sequence[str]) -> None:
        if removals:
            self.progress(f"uninstalling apt packages: {', '.join(removals)}")
            self._system(*APT_GET, "remove", "-y", *removals, what="removing the old ffmpeg")
        for name in dict.fromkeys(e.package for e in old if e.owner == "snap"):
            self.progress(f"uninstalling snap {name}")
            self._system("snap", "remove", name, what="removing the old ffmpeg")

    def _report(self, build: Build) -> str:
        lines = [f"Installed ffmpeg {build.version} to {self.layout.install_dir}"]
        for prog in PROGRAMS:
            found = self.runner.which(prog)
            want = self.layout.install_dir / "bin" / prog
            if not found or Path(os.path.realpath(found)) != want:
                lines.append(
                    f"note: `{prog}` on PATH is {found or 'missing'}; put {self.layout.bin_dir} "
                    f"first on PATH, or run {self.layout.bin_dir / prog}"
                )
        lines.append("Open a new shell (or run `hash -r`) so it finds the new ffmpeg.")
        return "\n".join(lines)


def _confstr() -> str | None:
    try:
        return os.confstr("CS_GNU_LIBC_VERSION")
    except (AttributeError, ValueError, OSError):  # no confstr on some platforms
        return None
