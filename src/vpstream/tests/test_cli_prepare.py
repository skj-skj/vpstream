"""The command line of prepare, prepared, track-info and browser-mode start, with a
stand-in daemon."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vpstream import api, cli, client, probe
from vpstream.tests.test_probe import SAMPLE

PLAN = {
    "source": "/m/film.mkv",
    "name": "film.mkv",
    "duration": 7200.0,
    "seconds": 9000.0,
    "longer_than_film": True,
    "tasks": [{"task": "subtitle 7 (burned in)", "kind": "burn", "sub": 7, "seconds": 9000.0}],
    "notes": ["subtitle 9: not in this file"],
}


def job(id: int, state: str, progress: float = 0.0, error: str = "", **kw: Any) -> dict[str, Any]:
    return {"id": id, "source": "/m/film.mkv", "name": "film.mkv", "task": "base version (copy)",
            "state": state, "progress": progress, "seconds_left": 60.0 if state != "done" else None,
            "error": error, **kw}  # fmt: skip


class Daemon:
    """Answers each route from a script: a list of answers per route, the last one repeated.
    An answer that is an exception is raised."""

    def __init__(self, **routes: list[Any]) -> None:
        self.routes = {"status": [{"stream": None, "prepare": []}], **routes}
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, socket: Path, method: str, route: str, payload: Any = None) -> Any:
        self.calls.append((route, payload))
        answers = self.routes[route]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer(payload) if callable(answer) else answer

    def posts(self, route: str) -> list[Any]:
        return [p for r, p in self.calls if r == route]


def confirm_first() -> client.DaemonRefused:
    data = {"error": "preparing takes longer than the film", "code": api.CONFIRM, "plans": [PLAN]}
    return client.DaemonRefused("the daemon refused: it takes long", data)


@pytest.fixture(autouse=True)
def quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """No real waiting, no real access record; `_revoke` calls are counted."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    revoked: list[str] = []
    monkeypatch.setattr(cli, "_revoke", lambda: revoked.append("revoke") or 0)
    return revoked


def use(monkeypatch: pytest.MonkeyPatch, daemon: Daemon) -> Daemon:
    monkeypatch.setattr(client, "call", daemon)
    return daemon


def tty(monkeypatch: pytest.MonkeyPatch, answer: str | None) -> None:
    """A terminal that answers `answer`; None for no terminal."""
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: answer is not None)
    monkeypatch.setattr("builtins.input", lambda prompt: answer or "")


# --- prepare


def test_a_long_prepare_is_shown_and_asked_about(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    film = tmp_path / "film.mkv"
    done = {"plans": [{**PLAN, "longer_than_film": False}], "jobs": [4]}
    d = use(monkeypatch, Daemon(
        prepare=[confirm_first(), done],
        status=[{"prepare": []}, {"prepare": [job(4, "running", 0.5)]},
                {"prepare": [job(4, "done", 1.0)]}],
    ))  # fmt: skip
    tty(monkeypatch, "y")
    assert cli.main(["prepare", "--file", str(film), "--sub", "7"]) == 0
    first, second = d.posts("prepare")
    assert first == {"file": str(film), "sub": "7"} and second == {**first, "confirmed": True}
    out = capsys.readouterr().out
    assert "preparing takes longer than the film" in out
    assert "film.mkv (2h 00m): about 2h 30m  (longer than the film)" in out
    assert "  subtitle 7 (burned in): about 2h 30m" in out and "note: subtitle 9" in out
    assert "film.mkv: base version (copy): done" in out


def test_without_a_terminal_a_long_prepare_needs_yes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = use(monkeypatch, Daemon(prepare=[confirm_first()]))
    tty(monkeypatch, None)
    assert cli.main(["prepare", "--file", "/m/film.mkv"]) == 1
    assert len(d.posts("prepare")) == 1
    captured = capsys.readouterr()
    assert "pass --yes" in captured.err and "Cancelled; nothing was changed." in captured.out
    d = use(monkeypatch, Daemon(prepare=[confirm_first(), {"plans": [PLAN], "jobs": []}]))
    assert cli.main(["prepare", "--file", "/m/film.mkv", "--yes"]) == 0
    assert d.posts("prepare")[1]["confirmed"] is True


def test_a_no_is_a_no(monkeypatch: pytest.MonkeyPatch) -> None:
    d = use(monkeypatch, Daemon(prepare=[confirm_first()]))
    tty(monkeypatch, "n")
    assert cli.main(["prepare", "--dir", "/m/show"]) == 1
    assert len(d.posts("prepare")) == 1


def test_the_daemon_is_asked_only_once_more(monkeypatch: pytest.MonkeyPatch) -> None:
    d = use(monkeypatch, Daemon(prepare=[confirm_first()]))  # refuses every time
    assert cli.main(["prepare", "--file", "/m/f.mkv", "--yes"]) == 1
    assert len(d.posts("prepare")) == 2


def test_nothing_to_make(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, Daemon(prepare=[{"plans": [{**PLAN, "tasks": []}], "jobs": []}]))
    assert cli.main(["prepare", "--file", "/m/f.mkv"]) == 0
    out = capsys.readouterr().out
    assert "film.mkv (2h 00m): nothing to make" in out and "Nothing to make" in out


def test_no_wait(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    d = use(monkeypatch, Daemon(prepare=[{"plans": [PLAN], "jobs": [1]}]))
    assert cli.main(["prepare", "--file", "/m/f.mkv", "--no-wait"]) == 0
    assert "Queued" in capsys.readouterr().out
    assert [r for r, _ in d.calls].count("status") == 1  # the look before, none after


def test_a_failed_prepare_says_why_and_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], quiet: list[str]
) -> None:
    use(monkeypatch, Daemon(
        prepare=[{"plans": [PLAN], "jobs": [1, 2]}],
        status=[{"prepare": []}, {"prepare": [job(2, "queued"), job(1, "failed", error="no room")]},
                {"prepare": [job(2, "done"), job(1, "failed", error="no room")]}],
    ))  # fmt: skip
    assert cli.main(["prepare", "--file", "/m/f.mkv"]) == 1
    out = capsys.readouterr().out
    assert out.count("failed: no room") == 1 and "base version (copy): done" in out
    assert quiet == ["revoke", "revoke"]  # before (idle), and after the last job


def test_ctrl_c_stops_the_showing_not_the_preparing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], quiet: list[str]
) -> None:
    def interrupt(payload: Any) -> Any:
        raise KeyboardInterrupt

    use(monkeypatch, Daemon(
        prepare=[{"plans": [PLAN], "jobs": [1]}],
        status=[{"prepare": []}, interrupt],
    ))  # fmt: skip
    assert cli.main(["prepare", "--file", "/m/f.mkv"]) == 0
    assert "Still preparing in the background" in capsys.readouterr().out
    assert quiet == ["revoke"]  # not taken back while it runs


def test_relative_paths_are_made_whole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    d = use(monkeypatch, Daemon(prepare=[{"plans": [], "jobs": []}]))
    cli.main(["prepare", "--dir", "show"])
    assert d.posts("prepare")[0] == {"dir": str(tmp_path / "show")}


# --- file access is kept while prepares read the files


def busy_status() -> dict[str, Any]:
    return {"stream": None, "prepare": [job(1, "running", 0.3)]}


def test_stop_keeps_the_access_while_prepares_run(
    monkeypatch: pytest.MonkeyPatch, quiet: list[str]
) -> None:
    stopped = {**busy_status(), "stream_port": 1337, "stream_port_state": "closed"}
    use(monkeypatch, Daemon(**{"stream/stop": [stopped]}))
    assert cli.main(["stop"]) == 0 and quiet == []
    use(monkeypatch, Daemon(**{"stream/stop": [{**stopped, "prepare": [job(1, "done")]}]}))
    assert cli.main(["stop"]) == 0 and quiet == ["revoke"]


def test_stop_without_a_stream_still_looks_at_the_prepares(
    monkeypatch: pytest.MonkeyPatch, quiet: list[str]
) -> None:
    refused = client.DaemonRefused("the daemon refused: no stream is running", {})
    use(monkeypatch, Daemon(**{"stream/stop": [refused]}, status=[busy_status()]))
    cli.main(["stop"])
    assert quiet == []


def test_revoke_waits_for_the_prepares(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], quiet: list[str]
) -> None:
    use(monkeypatch, Daemon(status=[busy_status()]))
    assert cli.main(["revoke"]) == 1 and quiet == []
    assert "being prepared" in capsys.readouterr().err


def test_start_does_not_take_back_what_a_prepare_reads(
    monkeypatch: pytest.MonkeyPatch, quiet: list[str]
) -> None:
    started = {"stream": {"alias": "aaa", "videos": [], "url": "u"}, "prepare": [], "dashboard": {}}
    use(monkeypatch, Daemon(status=[busy_status()], **{"stream/start": [started]}))
    assert cli.main(["start", "--file", "/m/film.mkv"]) == 0 and quiet == []


# --- browser-mode start


def test_a_browser_start_that_must_transcode_asks_and_shows_the_copy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # As Stream.describe and Control.status give it: no source per video (found in review:
    # the command looked for one, so it never showed the progress).
    video = {"number": 1, "name": "film.mkv", "url": "http://h/aaa", "ready": False,
             "job": job(3, "running", 0.25)}  # fmt: skip
    stream = {"alias": "aaa", "mode": "browser", "policy": "approval", "kind": "file",
              "url": "http://h/aaa", "videos": [video]}  # fmt: skip
    started = {"stream": stream, "prepare": [job(3, "running", 0.25)], "dashboard": {},
               "jobs": [3]}  # fmt: skip
    d = use(monkeypatch, Daemon(
        **{"stream/start": [confirm_first(), started]},
        status=[{"stream": None, "prepare": []}, {"prepare": [job(3, "done", 1.0)]}],
    ))  # fmt: skip
    code = cli.main(["start", "--file", "/m/film.mkv", "--mode", "browser", "--audio", "jpn",
                     "--sub", "eng", "--yes"])  # fmt: skip
    assert code == 0
    first, second = d.posts("stream/start")
    assert first["audio"] == "jpn" and first["sub"] == "eng" and first["mode"] == "browser"
    assert second["confirmed"] is True
    out = capsys.readouterr().out
    assert "Address:      http://h/aaa  [25%]" in out
    assert "Preparing the videos for browsers" in out and "base version (copy): done" in out


# --- following prepares (found in review)


def follow(monkeypatch: pytest.MonkeyPatch, ids: list[int], *statuses: dict[str, Any]) -> int:
    use(monkeypatch, Daemon(
        prepare=[{"plans": [], "jobs": ids, "prepare_run": "r1"}],
        status=[{"prepare": [], "prepare_run": "r1"}, *statuses],
    ))  # fmt: skip
    return cli.main(["prepare", "--file", "/m/f.mkv"])


def test_a_failure_is_counted_when_it_is_seen_not_only_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = follow(
        monkeypatch,
        [1, 2],
        {"prepare_run": "r1", "prepare": [job(1, "failed", error="x"), job(2, "running")]},
        # By the end the daemon no longer lists job 1: too many have finished since.
        {"prepare_run": "r1", "prepare": [job(2, "done")]},
    )
    assert code == 1


def test_a_daemon_restart_while_following_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The new daemon numbers its jobs from 1 again: job 7 of the old one is not its job 7.
    code = follow(monkeypatch, [7], {"prepare_run": "r2", "prepare": [job(7, "done")]})
    assert code == 1 and "the daemon was restarted" in capsys.readouterr().err


def test_jobs_that_ended_unseen_are_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = follow(monkeypatch, [1, 2], {"prepare_run": "r1", "prepare": [job(2, "done")]})
    assert code == 1 and "1 prepares ended unseen" in capsys.readouterr().err


def test_a_cancelled_job_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    code = follow(monkeypatch, [1], {"prepare_run": "r1", "prepare": [job(1, "cancelled")]})
    assert code == 1


def test_when_the_access_is_kept_the_command_says_how_to_take_it_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, Daemon(prepare=[{"plans": [PLAN], "jobs": [1]}]))
    cli.main(["prepare", "--file", "/m/f.mkv", "--no-wait"])
    assert "vpstream revoke" in capsys.readouterr().out
    stopped = {**busy_status(), "stream_port": 1337, "stream_port_state": "closed"}
    use(monkeypatch, Daemon(**{"stream/stop": [stopped]}))
    cli.main(["stop"])
    assert "vpstream revoke" in capsys.readouterr().out


def test_status_shows_the_prepares(monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"version": "x", "prepare": [job(1, "running", 0.5), job(2, "queued"), job(3, "done")]}
    text = cli.format_status(info, "x")
    assert "Preparing:    film.mkv: base version (copy): 50%, about 1m 00s left" in text
    assert "and 1 more queued" in text


# --- prepared


def test_prepared_lists_the_cache_and_the_prepares(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        {"key": "k1", "source": "/m/a.mkv", "stale": False, "base": "copy", "soft": [1, 3],
         "burn": [7], "bytes": 2.5e9, "in_use": True, "busy": False},
        {"key": "k2", "source": "/m/old.mkv", "stale": True, "base": "", "soft": [],
         "burn": [], "bytes": 1e6, "in_use": False, "busy": False},
        {"key": "k3", "source": "/m/locked/c.mkv", "stale": False, "unchecked": True,
         "base": "copy", "soft": [], "burn": [], "bytes": 1e6, "in_use": False, "busy": False},
    ]  # fmt: skip
    jobs = [job(1, "failed", error="x"), job(2, "done")]
    use(monkeypatch, Daemon(prepared=[{"prepared": rows}], status=[{"prepare": jobs}]))
    assert cli.main(["prepared"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["BASE", "SOFT", "BURNED", "IN", "SIZE", "SOURCE"]
    assert lines[1].split() == ["copy", "1,3", "7", "2.5", "GB", "/m/a.mkv", "(in", "use)"]
    assert lines[2].split()[:3] == ["-", "-", "-"] and lines[2].endswith("(stale)")
    assert lines[3].endswith("/m/locked/c.mkv  (cannot check)")
    assert lines[4] == "3 videos, 2.5 GB in all"
    assert "film.mkv: base version (copy): failed: x" in lines[-1]
    assert not any("done" in line for line in lines)


def test_nothing_prepared(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use(monkeypatch, Daemon(prepared=[{"prepared": []}]))
    assert cli.main(["prepared", "list"]) == 0
    assert capsys.readouterr().out.strip() == "Nothing is prepared."


@pytest.mark.parametrize(
    ("argv", "payload"),
    [
        (["rm", "--stale"], {"stale": True, "all": False}),
        (["rm", "--all", "--yes"], {"stale": False, "all": True}),
    ],
)
def test_prepared_rm(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], payload: dict[str, Any]
) -> None:
    d = use(monkeypatch, Daemon(**{"prepared/rm": [{"removed": 2, "kept": ["/m/a.mkv"]}]}))
    assert cli.main(["prepared", *argv]) == 0
    assert d.posts("prepared/rm") == [payload]


def test_prepared_rm_of_a_path_follows_links_like_the_daemon_did(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    d = use(monkeypatch, Daemon(**{"prepared/rm": [{"removed": 1, "kept": ["/m/x.mkv"]}]}))
    assert cli.main(["prepared", "rm", str(tmp_path / "link")]) == 0
    assert d.posts("prepared/rm") == [{"path": str(real)}]
    out = capsys.readouterr().out
    assert "Removed the prepared versions of 1 videos." in out and "Kept" in out


@pytest.mark.parametrize("argv", [["rm"], ["rm", "--stale", "--all"]])
def test_prepared_rm_needs_one_choice(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    d = use(monkeypatch, Daemon())
    assert cli.main(["prepared", *argv]) == 2 and d.calls == []


def test_prepared_rm_all_asks_first(monkeypatch: pytest.MonkeyPatch) -> None:
    d = use(monkeypatch, Daemon())
    tty(monkeypatch, "n")
    assert cli.main(["prepared", "rm", "--all"]) == 1 and d.calls == []


def test_prepared_cancel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = use(monkeypatch, Daemon(**{"prepare/cancel": [{"cancelled": 3}]}))
    assert cli.main(["prepared", "cancel"]) == 0
    assert d.posts("prepare/cancel") == [{"path": None}]
    assert "Cancelled 3 prepares." in capsys.readouterr().out


# --- track-info


def test_track_info(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen: list[Path] = []

    def fake_probe(runner: Any, path: Path) -> Any:
        seen.append(path)
        return probe.parse_probe(SAMPLE, str(path))

    monkeypatch.setattr(probe, "probe", fake_probe)
    assert cli.main(["track-info", "movie.mkv"]) == 0
    assert seen == [Path("movie.mkv").resolve()]
    assert "SUBTITLES" in capsys.readouterr().out


def test_track_info_of_a_file_ffprobe_cannot_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(runner: Any, path: Path) -> Any:
        raise probe.ProbeError("ffprobe failed for x: Invalid data")

    monkeypatch.setattr(probe, "probe", broken)
    assert cli.main(["track-info", "x.mkv"]) == 1
    assert "Invalid data" in capsys.readouterr().err


# --- how jobs and videos are shown


@pytest.mark.parametrize(
    ("item", "shown"),
    [
        (job(1, "running", 0.456), "film.mkv: base version (copy): 46%, about 1m 00s left"),
        (job(1, "queued"), "film.mkv: base version (copy): queued, about 1m 00s"),
        (job(1, "failed", error="no room"), "film.mkv: base version (copy): failed: no room"),
        (job(1, "cancelled", seconds_left=None), "film.mkv: base version (copy): cancelled"),
    ],
)  # fmt: skip
def test_format_job(item: dict[str, Any], shown: str) -> None:
    assert cli.format_job(item) == shown


@pytest.mark.parametrize(
    ("video", "mark"),
    [
        ({"job": {"state": "queued"}}, "  [queued]"),
        ({"job": {"state": "running", "progress": 0.5}}, "  [50%]"),
        ({"preparing": True}, "  [preparing]"),  # direct mode: a seek index
        ({"ready": False}, "  [not ready]"),
        ({"ready": True}, ""),
        ({}, ""),
    ],
)
def test_video_states(video: dict[str, Any], mark: str) -> None:
    assert cli._video_state(video) == mark
