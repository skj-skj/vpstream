from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from vpstream import access, api, cli, client, config, doctor
from vpstream.control import Control
from vpstream.tests.fakes import FakeFirewall, FakeRunner, FakeServer
from vpstream.tools import ToolMissing

ME = os.geteuid()


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A private library, as an upload leaves it: nothing in it is open to other users."""
    root = tmp_path / "media"
    (root / "show").mkdir(parents=True)
    root.chmod(0o700)
    (root / "show").chmod(0o750)
    for name in ("film.mkv", "show/e1.mkv", "show/e2.mkv", "show/notes.txt"):
        (root / name).write_bytes(b"x")
        (root / name).chmod(0o660)
    tmp_path.chmod(0o755)
    return root


@pytest.fixture(autouse=True)
def only_the_tests_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The folders above the test's own are the host's, and differ from host to host (in
    Termux they are private to the app). The tests look at what is below."""
    real = access.Plan.add

    def add(self: access.Plan, path: Path, perms: str, uid: int) -> None:
        if path.is_relative_to(tmp_path) and path != tmp_path:
            real(self, path, perms, uid)

    monkeypatch.setattr(access.Plan, "add", add)


def _mine(tmp_path: Path, plan: access.Plan) -> dict[str, list[str]]:
    return {
        perms: [str(p.relative_to(tmp_path)) for p in paths] for perms, paths in plan.grants.items()
    }


# --- what to change


def test_a_file_needs_read_and_its_folders_enter(tmp_path: Path, library: Path) -> None:
    plan = access.plan("file", library / "film.mkv", ME)
    assert _mine(tmp_path, plan) == {"r": ["media/film.mkv"], "x": ["media"]}


def test_a_folder_needs_read_and_enter_and_its_videos_read(tmp_path: Path, library: Path) -> None:
    plan = access.plan("dir", library / "show", ME)
    assert _mine(tmp_path, plan) == {
        "rx": ["media/show"],
        "r": ["media/show/e1.mkv", "media/show/e2.mkv"],  # not notes.txt
        "x": ["media"],
    }


def test_what_is_open_to_everyone_already_is_left_alone(tmp_path: Path, library: Path) -> None:
    library.chmod(0o755)
    (library / "film.mkv").chmod(0o644)
    assert _mine(tmp_path, access.plan("file", library / "film.mkv", ME)) == {}
    (library / "film.mkv").chmod(0o640)
    assert _mine(tmp_path, access.plan("file", library / "film.mkv", ME)) == {
        "r": ["media/film.mkv"]
    }


def test_a_link_is_opened_where_it_leads(tmp_path: Path, library: Path) -> None:
    public = tmp_path / "public"
    public.mkdir()
    public.chmod(0o755)
    (public / "e1.mkv").symlink_to(library / "film.mkv")
    plan = access.plan("dir", public, ME)
    assert _mine(tmp_path, plan) == {"r": ["media/film.mkv"], "x": ["media"]}


def test_what_belongs_to_someone_else_is_named_not_changed(tmp_path: Path, library: Path) -> None:
    plan = access.plan("file", library / "film.mkv", ME + 1)  # as another user
    assert plan.grants == {}
    assert plan.not_mine == [library / "film.mkv", library]


def test_a_path_that_is_not_there_plans_nothing(tmp_path: Path) -> None:
    assert _mine(tmp_path, access.plan("file", tmp_path / "nope.mkv", ME)) == {}
    assert _mine(tmp_path, access.plan("dir", tmp_path / "nope", ME)) == {}


# --- carrying it out


def test_commands_name_one_user_and_end_the_options(tmp_path: Path) -> None:
    odd = tmp_path / "-rf file.mkv"
    calls = access.commands("vpstream", {"r": [odd], "x": [tmp_path]})
    assert calls == [
        ["setfacl", "-m", "u:vpstream:r", "--", str(odd)],
        ["setfacl", "-m", "u:vpstream:x", "--", str(tmp_path)],
    ]
    many = access.commands("vpstream", {"r": [tmp_path / str(n) for n in range(450)]})
    assert [len(call) - 4 for call in many] == [200, 200, 50]


def test_grant_runs_setfacl_and_counts(tmp_path: Path, library: Path) -> None:
    runner = FakeRunner(("setfacl",))
    assert access.grant(runner, "vpstream", "dir", library / "show") == 4
    assert [call[:4] for call in runner.calls] == [
        ["setfacl", "-m", "u:vpstream:rx", "--"],
        ["setfacl", "-m", "u:vpstream:r", "--"],
        ["setfacl", "-m", "u:vpstream:x", "--"],
    ]
    assert runner.calls[1][4:] == [str(library / "show/e1.mkv"), str(library / "show/e2.mkv")]


def test_grant_does_nothing_when_nothing_is_lacking(tmp_path: Path, library: Path) -> None:
    for path in (library, library / "film.mkv"):
        path.chmod(0o755)
    runner = FakeRunner(())  # setfacl is not even there
    assert access.grant(runner, "vpstream", "file", library / "film.mkv") == 0
    assert runner.calls == []


def test_grant_says_when_the_acl_package_is_missing(library: Path) -> None:
    with pytest.raises(ToolMissing, match="sudo apt install acl"):
        access.grant(FakeRunner(()), "vpstream", "file", library / "film.mkv")


def test_grant_reports_a_setfacl_failure(library: Path) -> None:
    runner = FakeRunner(("setfacl",))
    runner.on("setfacl", returncode=1, stderr="setfacl: film.mkv: Operation not supported")
    with pytest.raises(access.AccessError, match="Operation not supported"):
        access.grant(runner, "vpstream", "file", library / "film.mkv")


def test_grant_names_the_command_for_files_of_another_owner(library: Path) -> None:
    runner = FakeRunner(("setfacl",))
    with pytest.raises(access.AccessError, match="not your file") as caught:
        access.grant(runner, "vpstream", "file", library / "film.mkv", uid=ME + 1)
    assert f"sudo setfacl -m u:vpstream:rX {library / 'film.mkv'}" in str(caught.value)
    assert runner.calls == []  # nothing is changed halfway


@pytest.mark.parametrize("user", ["", "root; rm -rf /", "a b", "-m", "u:x", "A" * 40, "None"])
def test_grant_takes_only_a_plain_user_name(library: Path, user: str) -> None:
    runner = FakeRunner(("setfacl",))
    if user == "None":
        user = "x" * 33
    with pytest.raises(access.AccessError, match="not a user name"):
        access.grant(runner, user, "file", library / "film.mkv")
    assert runner.calls == []


def test_service_user_is_this_user() -> None:
    import getpass

    assert access.service_user() == getpass.getuser()


def test_the_real_setfacl_opens_a_file_to_one_user_only(library: Path) -> None:
    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        pytest.skip("the acl tools are not installed on this host")
    from vpstream.tools import SystemRunner

    film = library / "film.mkv"
    try:
        assert access.grant(SystemRunner(), access.service_user(), "file", film) == 2
    except access.AccessError as exc:
        pytest.skip(f"this file system has no ACLs: {exc}")
    shown = subprocess.run(["getfacl", "-c", str(film)], capture_output=True, text=True).stdout
    assert f"user:{access.service_user()}:r--" in shown and "other::---" in shown


# --- the daemon's answer, and the command line acting on it


def test_the_refusal_says_what_and_for_whom(tmp_path: Path, library: Path) -> None:
    if ME == 0:
        pytest.skip("root reads everything")
    (library / "film.mkv").chmod(0)
    control = Control(
        config.Config(), config.Paths.under(tmp_path / "r"), FakeFirewall(), FakeServer()
    )
    http = api.create_app(control.status, control).test_client()
    answer = http.post("/api/v1/stream/start", json={"file": str(library / "film.mkv")})
    data = answer.get_json()
    assert answer.status_code == 400 and data["code"] == "unreadable"
    assert data["path"] == str(library / "film.mkv") and data["user"] == access.service_user()
    other = http.post("/api/v1/stream/start", json={"file": "/no/such.mkv"}).get_json()
    assert "code" not in other


class Daemon:
    """Stands in for the daemon: refuses as unreadable until access was given."""

    def __init__(self, refusals: int, code: str = api.UNREADABLE) -> None:
        self.refusals, self.code, self.asked = refusals, code, 0

    def __call__(self, socket: Path, method: str, route: str, payload: Any = None) -> Any:
        self.asked += 1
        if self.asked <= self.refusals:
            data = {"error": "cannot read it", "code": self.code, "user": "vpstream"}
            raise client.DaemonRefused("the daemon refused: cannot read it", data)
        return {"stream": {"alias": "aaa", "videos": [], "url": "http://x/aaa"}, "dashboard": {}}


@pytest.fixture
def setfacl(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    from vpstream import tools

    runner = FakeRunner(("setfacl",))
    monkeypatch.setattr(tools, "SystemRunner", lambda: runner)
    return runner


def test_start_gives_access_and_asks_again(
    library: Path,
    setfacl: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = Daemon(refusals=1)
    monkeypatch.setattr(client, "call", daemon)
    assert cli.main(["start", "--file", str(library / "film.mkv")]) == 0
    out = capsys.readouterr()
    assert daemon.asked == 2
    assert ["setfacl", "-m", "u:vpstream:r", "--", str(library / "film.mkv")] in setfacl.calls
    assert "Gave the vpstream service (user vpstream) read access" in out.err
    assert "Stream:       /aaa" in out.out and "chmod" not in out.out + out.err


def test_start_needs_nothing_when_the_service_can_read(
    library: Path, setfacl: FakeRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(refusals=0)
    monkeypatch.setattr(client, "call", daemon)
    assert cli.main(["start", "--file", str(library / "film.mkv")]) == 0
    assert daemon.asked == 1 and setfacl.calls == []


def test_start_asks_only_once_more(
    library: Path,
    setfacl: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = Daemon(refusals=5)
    monkeypatch.setattr(client, "call", daemon)
    assert cli.main(["start", "--file", str(library / "film.mkv")]) == 1
    assert daemon.asked == 2 and "cannot read it" in capsys.readouterr().err


def test_start_changes_nothing_for_other_refusals(
    library: Path,
    setfacl: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemon = Daemon(refusals=1, code="")
    monkeypatch.setattr(client, "call", daemon)
    assert cli.main(["start", "--file", str(library / "film.mkv")]) == 1
    assert daemon.asked == 1 and setfacl.calls == []
    capsys.readouterr()


def test_start_says_how_when_it_cannot_give_access(
    library: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from vpstream import tools

    monkeypatch.setattr(tools, "SystemRunner", lambda: FakeRunner(()))  # no setfacl
    daemon = Daemon(refusals=1)
    monkeypatch.setattr(client, "call", daemon)
    assert cli.main(["start", "--file", str(library / "film.mkv")]) == 1
    err = capsys.readouterr().err
    assert daemon.asked == 1 and "cannot read it" in err and "sudo apt install acl" in err


def test_start_with_nothing_to_change_shows_the_daemons_reason(
    library: Path,
    setfacl: FakeRunner,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for path in (library, library / "film.mkv"):
        path.chmod(0o755)
    daemon = Daemon(refusals=1)
    monkeypatch.setattr(client, "call", daemon)
    assert cli.main(["start", "--file", str(library / "film.mkv")]) == 1
    assert daemon.asked == 1 and "cannot read it" in capsys.readouterr().err


# --- doctor


def test_doctor_checks_for_the_acl_package() -> None:
    (tool,) = [t for t in doctor.TOOLS if t.name == "setfacl"]
    assert not tool.required  # a host where the service reads the videos anyway needs none
    check = doctor.check_tool(FakeRunner(()), tool)
    assert check.status == "warning" and check.fix == "sudo apt install acl"
    assert doctor.check_tool(FakeRunner(("setfacl",)), tool).status == "ok"
