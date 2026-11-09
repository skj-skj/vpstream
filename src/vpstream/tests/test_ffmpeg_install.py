from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from vpstream import cli
from vpstream import ffmpeg_install as fi
from vpstream.tools import Result, SystemRunner

V9 = "n9.0.2-48-gd4070a14ce"
ENCODERS = ("libx264", "libx265", "libsvtav1")
FILTERS = fi.REQUIRED_FILTERS
FAKED = ("apt-get", "dpkg-query", "snap", "ldd")


# --- pure parts


def _asset(name: str, size: int = 1000) -> dict[str, Any]:
    return {"name": name, "size": size, "browser_download_url": f"https://dl.invalid/{name}"}


def _release(tag: str, published: str, *names: str, sums: bool = True) -> dict[str, Any]:
    assets = [_asset(n) for n in names] + ([_asset("checksums.sha256")] if sums else [])
    return {"tag_name": tag, "published_at": published, "draft": False, "assets": assets}


def _gpl(version: str, arch: str = "linux64") -> str:
    branch = ".".join(version[1:].split("-")[0].split(".")[:2])
    return f"ffmpeg-{version}-{arch}-gpl-{branch}.tar.xz"


def test_latest_build_takes_the_highest_stable_branch() -> None:
    names = [
        "ffmpeg-N-128718-gb2b543c079-linux64-gpl.tar.xz",  # master snapshot
        _gpl("n8.1.3-34-ga35157abb9"),
        _gpl(V9),
        _gpl(V9).replace("-gpl-", "-gpl-shared-"),
        _gpl(V9).replace("-gpl-", "-lgpl-"),
        _gpl(V9, "linuxarm64"),
    ]
    releases = [
        _release("latest", "2026-11-09T13:00:00Z", "ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz"),
        _release("autobuild-2026-11-08", "2026-11-08T12:00:00Z", _gpl("n9.0.1")),
        _release("autobuild-2026-11-09", "2026-11-09T12:00:00Z", *names),
    ]
    build = fi.latest_build(releases, "linux64")
    assert (build.version, build.asset) == (V9, _gpl(V9))
    assert build.url.endswith(_gpl(V9)) and build.checksums_url.endswith("checksums.sha256")
    assert fi.latest_build(releases, "linuxarm64").asset == _gpl(V9, "linuxarm64")


def test_a_new_major_version_is_found_without_code_changes() -> None:
    names = [_gpl(V9), _gpl("n10.0-3-gabcdef1234"), _gpl("n10.1")]
    releases = [_release("autobuild-2026-12-01", "2026-12-01T12:00:00Z", *names)]
    assert fi.latest_build(releases, "linux64").version == "n10.1"


def test_latest_build_needs_checksums_and_a_match() -> None:
    no_sums = [_release("autobuild-1", "2026-11-09T12:00:00Z", _gpl(V9), sums=False)]
    with pytest.raises(fi.SetupError, match="no stable linux64"):
        fi.latest_build(no_sums, "linux64")
    older = _release("autobuild-0", "2026-11-08T12:00:00Z", _gpl("n9.0.1"))
    assert fi.latest_build([*no_sums, older], "linux64").version == "n9.0.1"


def test_version_order() -> None:
    order = ["n8.1.3-34-ga35157abb9", "n9.0", "n9.0.2", V9, "n9.0.3", "n10.0"]
    keys = [fi.version_key(v) for v in order]
    assert keys == sorted(keys, key=lambda k: k or ()) and len(set(keys)) == len(keys)
    assert fi.version_key("ffmpeg version n9.0.2-48-gd4070a14ce-20261109") == (9, 0, 2, 48)
    assert fi.version_key("7:6.1.1-3ubuntu5") is None


def test_parse_checksums() -> None:
    text = f"{'a' * 64}  one.tar.xz\n{'b' * 64} *two.tar.xz\nnot a line\n"
    assert fi.parse_checksums(text) == {"one.tar.xz": "a" * 64, "two.tar.xz": "b" * 64}


@pytest.mark.parametrize(
    ("libc", "problem"),
    [("glibc 2.39", None), ("glibc 2.28", None), ("glibc 2.27", "2.28 or newer"), (None, "glibc")],
)
def test_glibc_problem(libc: str | None, problem: str | None) -> None:
    got = fi.glibc_problem(libc)
    assert got is None if problem is None else problem in (got or "")


def test_missing_features() -> None:
    assert fi.missing_features(set(ENCODERS), set(FILTERS)) == []
    assert fi.missing_features({"libx264", "libaom-av1"}, set(FILTERS) - {"zscale"}) == [
        "libx265",
        "zscale",
    ]
    assert "libsvtav1 or libaom-av1" in fi.missing_features({"libx264", "libx265"}, set())


# --- the whole install, on a fake system in tmp_path


def _script(body: str) -> bytes:
    return f"#!{shutil.which('sh')}\n{body}\n".encode()


def _ffmpeg_script(version: str, encoders: Sequence[str]) -> bytes:
    enc = "".join(f" V....D {e}  encoder\\n" for e in encoders)
    flt = "".join(f" ... {f}  V->V  filter\\n" for f in FILTERS)
    return _script(
        'case "$*" in\n'
        f"  *-encoders*) printf '{enc}' ;;\n"
        f"  *-filters*) printf '{flt}' ;;\n"
        f'  *) echo "ffmpeg version {version}-20261109 Copyright" ;;\n'
        "esac"
    )


def _build_archive(
    version: str, encoders: Sequence[str] = ENCODERS, loader: str | None = None
) -> bytes:
    top = _gpl(version).removesuffix(".tar.xz")
    ffmpeg = _ffmpeg_script(version, encoders)
    if loader:  # a program loader this system lacks, like glibc's on a musl system
        ffmpeg = ffmpeg.replace(f"#!{shutil.which('sh')}".encode(), f"#!{loader}".encode())
    files = {
        f"{top}/bin/ffmpeg": ffmpeg,
        f"{top}/bin/ffprobe": _script(f'echo "ffprobe version {version} Copyright"'),
        f"{top}/bin/ffplay": _script("exit 0"),
        f"{top}/LICENSE.txt": b"GPL\n",
        f"{top}/doc/ffmpeg.html": b"<html></html>\n",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeHttp:
    """Serves one release with one build; `bad_sum` breaks its checksum."""

    def __init__(self, version: str = V9, *, bad_sum: bool = False, **kw: Any) -> None:
        self.calls: list[str] = []
        self.set_build(version, bad_sum=bad_sum, **kw)

    def set_build(self, version: str, *, bad_sum: bool = False, **kw: Any) -> None:
        self.archive = _build_archive(version, **kw)
        name = _gpl(version)
        digest = "0" * 64 if bad_sum else hashlib.sha256(self.archive).hexdigest()
        self.files = {
            f"https://dl.invalid/{name}": self.archive,
            "https://dl.invalid/checksums.sha256": f"{digest}  {name}\n".encode(),
        }
        self.releases = [_release("autobuild-x", "2026-11-09T12:00:00Z", name)]

    def get_json(self, url: str) -> Any:
        self.calls.append(url)
        return json.loads(json.dumps(self.releases))

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        return self.files[url].decode()

    def download(self, url: str, dest: Path, progress: Callable[[int, int], None]) -> str:
        self.calls.append(url)
        dest.write_bytes(self.files[url])
        progress(len(self.files[url]), len(self.files[url]))
        return hashlib.sha256(self.files[url]).hexdigest()


class HostRunner(SystemRunner):
    """Runs real programs, but PATH is `path` and apt, dpkg, snap and ldd are faked."""

    def __init__(self, path: Sequence[Path], tools: Sequence[str] = ()) -> None:
        self.path = list(path)
        self.tools = set(tools)
        self.calls: list[list[str]] = []
        self.answers: dict[tuple[str, ...], Result] = {}

    def on(self, *prefix: str, returncode: int = 0, stdout: str = "") -> None:
        self.answers[prefix] = Result(returncode, stdout, "")

    def which(self, name: str) -> str | None:
        if name in FAKED or name == "sudo":
            return f"/usr/bin/{name}" if name in self.tools else None
        if name in fi.PROGRAMS:
            found = [d / name for d in self.path if (d / name).exists()]
            return str(found[0]) if found else None
        return shutil.which(name)

    def run(self, args: Sequence[str], *, timeout: float | None = None) -> Result:
        cmd = [str(a) for a in args]
        self.calls.append(cmd)
        core = cmd[1:] if cmd[0] == "sudo" else cmd
        if core[:2] == ["env", "DEBIAN_FRONTEND=noninteractive"]:
            core = core[2:]
        if core[0] in FAKED:
            for prefix in sorted(self.answers, key=len, reverse=True):
                if tuple(core[: len(prefix)]) == prefix:
                    return self.answers[prefix]
            return Result(0, "", "")
        return super().run(core, timeout=timeout)


class Env:
    def __init__(self, tmp: Path, tools: Sequence[str] = ()) -> None:
        self.layout = fi.Layout(install_dir=tmp / "opt" / "vpstream-ffmpeg", bin_dir=tmp / "bin")
        self.usr_bin = tmp / "usr-bin"
        self.usr_bin.mkdir()
        self.runner = HostRunner([self.layout.bin_dir, self.usr_bin], tools)
        self.http = FakeHttp()
        self.questions: list[str] = []
        self.answer = True
        self.tmp = tmp / "tmp"
        self.tmp.mkdir()

    def setup(self, **kw: Any) -> fi.Setup:
        def confirm(question: str) -> bool:
            self.questions.append(question)
            return self.answer

        defaults: dict[str, Any] = dict(
            runner=self.runner,
            http=self.http,
            confirm=confirm,
            progress=lambda m: None,
            layout=self.layout,
            tmp_dir=str(self.tmp),
            is_root=True,
            system="linux",
            machine="x86_64",
            libc="glibc 2.39",
        )
        return fi.Setup(**{**defaults, **kw})

    def run(self, update: bool = False, **kw: Any) -> str:
        return self.setup(**kw).run(update=update)

    def installed_version(self) -> str:
        link = self.layout.bin_dir / "ffmpeg"
        return SystemRunner().run([str(link), "-version"]).stdout.split()[2]

    def system_calls(self, program: str) -> list[list[str]]:
        return [c for c in self.runner.calls if program in c]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_fresh_install(env: Env) -> None:
    msg = env.run()
    assert f"Installed ffmpeg {V9}" in msg and "note:" not in msg
    assert env.questions == []  # nothing to uninstall, nothing to ask
    assert env.installed_version() == f"{V9}-20261109"
    inst = env.layout.install_dir
    assert sorted(p.name for p in (inst / "bin").iterdir()) == ["ffmpeg", "ffprobe"]  # no ffplay
    assert (inst / "LICENSE.txt").is_file() and not (inst / "doc").exists()
    assert json.loads((inst / fi.MARKER).read_text())["version"] == V9
    assert (env.layout.bin_dir / "ffprobe").resolve() == inst / "bin" / "ffprobe"
    assert list(env.tmp.iterdir()) == []  # the download is cleaned up


def test_install_twice_points_to_update(env: Env) -> None:
    env.run()
    env.http.calls.clear()
    assert "already installed by vpstream. For a newer build: vpstream update" in env.run()
    assert env.http.calls == []


def test_missing_libraries_are_installed_with_apt(tmp_path: Path) -> None:
    env = Env(tmp_path, tools=("apt-get", "dpkg-query", "sudo"))
    env.runner.on("dpkg-query", "-W", "-f=${Status}", "libgcc-s1", stdout="install ok installed")
    env.run(is_root=False)
    apt = env.system_calls("apt-get")
    assert apt == [
        [
            "sudo",
            *fi.APT_GET,
            "install",
            "-y",
            "--no-install-recommends",
            "fontconfig",
            "fonts-dejavu-core",
        ]
    ]
    # Every system change goes through sudo.
    changes = [c for c in env.runner.calls if c[0] in ("sudo", "cp", "mv", "rm", "ln", "chmod")]
    assert changes and all(c[0] == "sudo" for c in changes)


def test_without_sudo_a_normal_user_is_refused(env: Env) -> None:
    with pytest.raises(fi.SetupError, match="needs root"):
        env.run(is_root=False)
    assert not env.layout.install_dir.exists()


def _apt_ffmpeg(env: Env) -> None:
    for prog in fi.PROGRAMS:
        path = env.usr_bin / prog
        path.write_bytes(_script(f'echo "{prog} version 6.1.1-3ubuntu5 Copyright"'))
        path.chmod(0o755)
        env.runner.on("dpkg-query", "-S", str(path), stdout=f"ffmpeg: {path}\n")
    env.runner.tools |= {"apt-get", "dpkg-query"}
    env.runner.on("apt-get", "-s", "remove", stdout="Remv ffmpeg [7:6.1.1]\nRemv some-app [1.0]\n")
    for pkg in fi.APT_PACKAGES:
        env.runner.on("dpkg-query", "-W", "-f=${Status}", pkg, stdout="install ok installed")


def test_existing_apt_ffmpeg_is_uninstalled_after_a_yes(env: Env) -> None:
    _apt_ffmpeg(env)
    env.run()
    (question,) = env.questions
    assert question.startswith("This will uninstall the existing ffmpeg (")
    assert "Do you want to proceed?" in question
    assert "apt package ffmpeg" in question and "6.1.1-3ubuntu5" in question
    assert "apt would also remove some-app" in question
    assert env.system_calls("remove")[-1] == [*fi.APT_GET, "remove", "-y", "ffmpeg", "some-app"]
    assert env.installed_version() == f"{V9}-20261109"


def test_no_cancels_and_changes_nothing(env: Env) -> None:
    _apt_ffmpeg(env)
    env.answer = False
    with pytest.raises(fi.Cancelled):
        env.run()
    assert not env.layout.install_dir.exists() and not env.layout.bin_dir.exists()
    assert not [c for c in env.system_calls("apt-get") if "-s" not in c]
    assert not [u for u in env.http.calls if u.endswith(".tar.xz")]  # nothing downloaded


def test_ffmpeg_from_elsewhere_is_never_removed(env: Env) -> None:
    own = env.usr_bin / "ffmpeg"
    own.write_bytes(_script("echo ffmpeg version 5.1 Copyright"))
    own.chmod(0o755)
    with pytest.raises(fi.SetupError, match=r"not from apt, snap or vpstream.*remove it yourself"):
        env.run()
    assert env.questions == [] and own.is_file()


def test_a_foreign_file_in_the_bin_folder_is_not_overwritten(env: Env) -> None:
    env.runner.path = [env.usr_bin]  # the bin folder is not first on PATH, so not found there
    env.layout.bin_dir.mkdir()
    (env.layout.bin_dir / "ffprobe").write_text("mine")
    with pytest.raises(fi.SetupError, match="was not installed by vpstream"):
        env.run()
    assert (env.layout.bin_dir / "ffprobe").read_text() == "mine"


def test_snap_ffmpeg_is_removed(env: Env) -> None:
    runner = env.runner
    snap_bin = Path("/snap/bin/ffmpeg")
    runner.tools.add("snap")
    real_which = runner.which
    runner.which = lambda name: str(snap_bin) if name == "ffmpeg" else real_which(name)  # type: ignore[method-assign]
    found = fi.find_existing(runner, env.layout)
    assert [(e.owner, e.package) for e in found] == [("snap", "ffmpeg")]


def test_update_does_nothing_when_current(env: Env) -> None:
    env.run()
    env.http.calls.clear()
    assert env.run(update=True) == f"ffmpeg {V9} is already the newest stable build"
    assert not [u for u in env.http.calls if u.endswith(".tar.xz")] and env.questions == []


def test_update_to_a_new_major_version(env: Env) -> None:
    env.run()
    env.http.set_build("n10.0.1-5-g0123456789")
    env.run(update=True)
    (question,) = env.questions
    assert f"({V9} installed by vpstream) and install n10.0.1-5-g0123456789" in question
    assert env.installed_version() == "n10.0.1-5-g0123456789-20261109"
    marker = json.loads((env.layout.install_dir / fi.MARKER).read_text())
    assert marker["version"] == "n10.0.1-5-g0123456789"
    assert not env.layout.install_dir.with_name("vpstream-ffmpeg.new").exists()


def test_update_without_an_install_installs(env: Env) -> None:
    assert f"Installed ffmpeg {V9}" in env.run(update=True)


def test_a_bad_checksum_changes_nothing(env: Env) -> None:
    env.http = FakeHttp(bad_sum=True)
    with pytest.raises(fi.SetupError, match="does not match its sha256"):
        env.run()
    assert not env.layout.install_dir.exists()


def test_a_build_without_the_needed_encoders_changes_nothing(env: Env) -> None:
    _apt_ffmpeg(env)
    env.http = FakeHttp(encoders=("libx264",))
    with pytest.raises(fi.SetupError, match="lacks libx265, libsvtav1 or libaom-av1"):
        env.run()
    assert not env.layout.install_dir.exists()
    assert not [c for c in env.system_calls("apt-get") if "remove" in c and "-s" not in c]


def test_a_build_this_system_cannot_run_changes_nothing(env: Env) -> None:
    env.http = FakeHttp(loader="/no/such/ld-linux.so")
    with pytest.raises(fi.SetupError, match=r"does not run here: its program loader is missing"):
        env.run()
    assert not env.layout.install_dir.exists() and list(env.tmp.iterdir()) == []


def test_a_failed_system_step_is_reported(env: Env) -> None:
    env.runner.tools |= {"apt-get", "dpkg-query"}
    env.runner.on("apt-get", "install", returncode=100, stdout="E: Unable to locate package")
    with pytest.raises(fi.SetupError, match=r"libraries ffmpeg needs.*Unable to locate"):
        env.run()


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"machine": "riscv64"}, "no ffmpeg build for this processor"),
        ({"libc": ""}, "need glibc"),
        ({"system": "darwin"}, "Linux only"),
    ],
)
def test_unsupported_hosts_are_refused(env: Env, kw: dict[str, Any], match: str) -> None:
    with pytest.raises(fi.SetupError, match=match):
        env.run(**kw)
    assert env.http.calls == []


def test_arm_hosts_get_the_arm_build(env: Env) -> None:
    env.http.releases[0]["assets"][0]["name"] = _gpl(V9, "linuxarm64")
    env.http.files[f"https://dl.invalid/{_gpl(V9, 'linuxarm64')}"] = env.http.archive
    sums = env.http.files["https://dl.invalid/checksums.sha256"].decode()
    env.http.files["https://dl.invalid/checksums.sha256"] = sums.replace(
        _gpl(V9), _gpl(V9, "linuxarm64")
    ).encode()
    env.http.releases[0]["assets"][0]["browser_download_url"] = (
        f"https://dl.invalid/{_gpl(V9, 'linuxarm64')}"
    )
    assert f"Installed ffmpeg {V9}" in env.run(machine="aarch64")


# --- command line


def test_cli_rejects_other_programs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["install", "caddy"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raised", "code", "text"),
    [(fi.Cancelled(), 1, "Cancelled; nothing was changed."), (fi.SetupError("boom"), 1, "boom")],
)
def test_cli_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raised: Exception,
    code: int,
    text: str,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(self: fi.Setup, *, update: bool) -> str:
        seen["update"] = update
        raise raised

    monkeypatch.setattr(fi.Setup, "run", fake_run)
    assert cli.main(["update", "ffmpeg", "--yes"]) == code
    out = capsys.readouterr()
    assert text in out.out + out.err and seen["update"] is True


def test_cli_without_a_terminal_needs_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(self: fi.Setup, *, update: bool) -> str:
        if not self.confirm("This will uninstall the existing ffmpeg (x). Proceed?"):
            raise fi.Cancelled
        return "done"

    monkeypatch.setattr(fi.Setup, "run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["install", "ffmpeg"]) == 1
    assert "pass --yes" in capsys.readouterr().err
    assert cli.main(["install", "ffmpeg", "--yes"]) == 0
