"""The daemon's control of browser mode and of prepares, with a stand-in ffmpeg."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from vpstream import api, config, prepare, prepared, public
from vpstream.control import Control, ControlError
from vpstream.stream import NeedsConfirm, StreamError
from vpstream.tests.fakes import FakeFfmpeg, FakeFirewall, FakeServer
from vpstream.tests.test_prepare import probe_json


class Rig:
    def __init__(self, root: Path, **probe: Any) -> None:
        self.media = root / "media"
        self.media.mkdir()
        self.film = self.media / "film.mkv"
        self.film.write_bytes(b"video")
        self.paths = config.Paths.under(root / "root")
        self.runner = FakeFfmpeg(root, probe_json(**probe))
        self.log: list[str] = []
        self.fw = FakeFirewall()
        self.preparer = prepare.Preparer(self.paths.cache_dir, self.runner, self.log.append)
        self.control = self.again()

    def again(self) -> Control:
        self.control = Control(
            config.Config(stream_host="192.0.2.9"),
            self.paths,
            self.fw,
            FakeServer(),
            self.log.append,
            boot="boot-1",
            runner=self.runner,
            preparer=self.preparer,
        )
        return self.control

    def start(self, **options: Any) -> dict[str, Any]:
        base = {"file": str(self.film), "alias": "aaa", "mode": "browser", "policy": "open"}
        return self.control.start({**base, **options})

    def recorded_stream(self) -> Any:
        from vpstream import state

        return state.load(self.paths.state_dir / state.FILE_NAME)[0].stream

    def entry(self, path: Path | None = None) -> Path:
        return prepared.entry_dir(self.paths.cache_dir, str(path or self.film))

    def tasks(self) -> list[str]:
        return [j["task"] for j in self.preparer.status()]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_a_browser_stream_makes_a_missing_copy_by_itself(rig: Rig) -> None:
    info = rig.start()
    assert info["stream"]["policy"] == "open" and rig.fw.calls == ["open"]
    assert rig.preparer.wait()
    assert sorted(rig.tasks()) == ["base version (copy)", "subtitle 1 (soft)"]  # no burn-in
    assert rig.control.status()["stream"]["videos"][0]["ready"] is True
    assert rig.control.visit("aaa", "192.0.2.1", "TV", True).kind == "player"


def test_while_it_is_made_a_browser_is_told_how_far_it_got(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.start()
    answer = rig.control.visit("aaa", "192.0.2.1", "TV", True)
    assert (answer.status, answer.kind) == (503, "preparing")
    assert answer.data["task"] == "base version (copy)"
    video = rig.control.status()["stream"]["videos"][0]
    assert video["job"]["kind"] == "base" and video["ready"] is False
    rig.preparer.cancel()
    rig.preparer.wait(15)
    assert rig.control.visit("aaa", "192.0.2.1", "TV", True).kind == "notready"


def test_a_transcode_is_asked_about_first(tmp_path: Path) -> None:
    rig = Rig(tmp_path, codec="h264", pix_fmt="yuv420p10le")
    with pytest.raises(NeedsConfirm, match=r"transcoded to H\.264") as refused:
        rig.start()
    assert rig.fw.calls == [] and rig.control.stream is None and rig.preparer.status() == []
    (plan,) = refused.value.details["plans"]
    assert plan["name"] == "film.mkv" and plan["tasks"][0]["task"].startswith("base version (t")
    rig.start(confirmed=True)
    assert "base version (transcode to H.264)" in rig.tasks()
    rig.preparer.wait()


def test_a_prepared_video_starts_without_any_prepare(rig: Rig) -> None:
    rig.start()
    rig.preparer.wait()
    rig.control.stop()
    before = len(rig.preparer.status())
    rig.start()
    assert len(rig.preparer.status()) == before


def test_the_starting_subtitles_must_be_prepared(rig: Rig) -> None:
    with pytest.raises(StreamError, match=r"subtitle 2 is not prepared.*--sub 2"):
        rig.start(sub="2")  # ASS: a burned-in version, which start does not make
    assert rig.control.stream is None
    rig.start(sub="1")  # soft: made with the base
    rig.preparer.wait()
    rig.control.stop()
    rig.start(sub="eng")  # base made now: known from its record
    rig.control.stop()
    rig.start(sub="off")
    rig.control.stop()
    with pytest.raises(StreamError, match="not prepared"):
        rig.start(sub="jpn")


def test_a_folder_stream_starts_with_what_is_ready(rig: Rig) -> None:
    for name in ("e1.mkv", "e2.mkv"):
        (rig.media / name).write_bytes(name.encode())
    rig.film.unlink()
    folder = {"dir": str(rig.media), "alias": "aaa", "mode": "browser"}
    info = rig.control.start({**folder, "sub": "1"})  # soft: made with each base
    assert [v["name"] for v in info["stream"]["videos"]] == ["e1.mkv", "e2.mkv"]
    rig.preparer.wait()
    assert rig.tasks().count("base version (copy)") == 2


def test_a_folder_start_needs_its_subtitles_prepared_for_some_episode(rig: Rig) -> None:
    # Found in review: a folder started with any --sub, prepared or not.
    for name in ("e1.mkv", "e2.mkv"):
        (rig.media / name).write_bytes(name.encode())
    rig.film.unlink()
    folder = {"dir": str(rig.media), "alias": "aaa", "mode": "browser"}
    with pytest.raises(StreamError, match=r"any video in the folder.*--dir .* --sub 2"):
        rig.control.start({**folder, "sub": "2"})  # ASS: burned in, which start does not make
    rig.control.prepare({"file": str(rig.media / "e2.mkv"), "sub": "2", "confirmed": True})
    rig.preparer.wait()
    rig.control.start({**folder, "sub": "2"})  # prepared for e2 only: e1 starts without
    rig.preparer.wait()


def test_prepare_asks_when_it_takes_longer_than_the_film(tmp_path: Path) -> None:
    rig = Rig(tmp_path, fps=60)  # a burn-in of 1080p at 60 fps: about 0.6x
    with pytest.raises(NeedsConfirm, match="longer than the film") as refused:
        rig.control.prepare({"file": str(rig.film), "sub": "2"})
    assert rig.preparer.status() == []
    assert refused.value.details["plans"][0]["longer_than_film"]
    answer = rig.control.prepare({"file": str(rig.film), "sub": "2", "confirmed": True})
    assert len(answer["jobs"]) == 2 and rig.preparer.wait()
    assert prepared.versions(rig.entry()).burn == [2]
    # Asked again, there is nothing left to make.
    again = rig.control.prepare({"file": str(rig.film), "sub": "2"})
    assert again["jobs"] == [] and again["plans"][0]["tasks"] == []


def test_a_quick_prepare_goes_ahead_without_asking(rig: Rig) -> None:
    answer = rig.control.prepare({"file": str(rig.film), "sub": "none"})
    assert [t["task"] for t in answer["plans"][0]["tasks"]] == ["base version (copy)"]
    rig.preparer.wait()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"sub": "1,"}, "sub: not a track number"),
        ({"sub": 3}, "sub: not a track choice"),
        ({"subs": "1"}, "unknown option: subs"),
        ({"file": None}, "exactly one"),
        ({"file": "/no/such.mkv"}, "not a file"),
    ],
)
def test_prepare_refuses_what_it_cannot_read(
    rig: Rig, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(StreamError, match=message):
        rig.control.prepare({"file": str(rig.film), **options})


def test_a_file_ffprobe_cannot_read_is_noted_not_fatal(rig: Rig) -> None:
    rig.runner.probe_json = "{}"  # no streams
    answer = rig.control.prepare({"file": str(rig.film)})
    assert answer["jobs"] == [] and "no video track" in answer["plans"][0]["notes"][0]


def test_without_a_preparer_browser_mode_cannot_start(rig: Rig) -> None:
    rig.control.preparer = None
    with pytest.raises(ControlError, match="cannot prepare"):
        rig.start()
    assert rig.fw.calls == []


def test_the_cache_list_says_what_is_in_use_and_remove_keeps_it(rig: Rig) -> None:
    rig.start()
    rig.preparer.wait()
    other = rig.media / "other.mkv"
    other.write_bytes(b"other")
    rig.control.prepare({"file": str(other), "sub": "none"})
    rig.preparer.wait()
    rows = {r["source"]: r for r in rig.control.prepared()}
    assert rows[str(rig.film)]["in_use"] and not rows[str(other)]["in_use"]
    assert rows[str(rig.film)]["base"] == "copy" and rows[str(rig.film)]["soft"] == [1]
    answer = rig.control.remove_prepared({"all": True})
    assert answer == {"removed": 1, "kept": [str(rig.film)]}
    assert not rig.entry(other).exists() and rig.entry().exists()


def test_remove_by_folder_and_the_stale_ones(rig: Rig) -> None:
    rig.control.prepare({"file": str(rig.film), "sub": "none"})
    rig.preparer.wait()
    old = rig.entry()
    st = os.stat(rig.film)
    os.utime(rig.film, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # a new film there
    rig.control.prepare({"file": str(rig.film), "sub": "none"})
    rig.preparer.wait()
    assert rig.control.remove_prepared({"stale": True}) == {"removed": 1, "kept": []}
    assert not old.exists() and rig.entry().exists()
    assert rig.control.remove_prepared({"path": str(rig.media) + "x"})["removed"] == 0
    assert rig.control.remove_prepared({"path": str(rig.media)})["removed"] == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root looks at everything")
def test_removing_the_stale_ones_keeps_what_cannot_be_looked_at(rig: Rig) -> None:
    # Found on the VPS: after its access was taken back, the service could not enter the
    # folder, and "remove the stale ones" would have removed good versions.
    rig.control.prepare({"file": str(rig.film), "sub": "none"})
    rig.preparer.wait()
    rig.media.chmod(0)
    try:
        (row,) = rig.control.prepared()
        answer = rig.control.remove_prepared({"stale": True})
    finally:
        rig.media.chmod(0o755)
    assert (row["stale"], row["unchecked"]) == (False, True)
    assert answer == {"removed": 0, "kept": []} and prepared.base_ready(rig.entry())


@pytest.mark.parametrize(
    "options",
    [{}, {"path": "relative"}, {"path": "/m", "all": True}, {"stale": True, "all": True}],
)
def test_remove_needs_exactly_one_full_choice(rig: Rig, options: dict[str, Any]) -> None:
    with pytest.raises(StreamError):
        rig.control.remove_prepared(options)


def test_a_video_being_prepared_is_not_removed_and_can_be_cancelled(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.control.prepare({"file": str(rig.film), "sub": "1"})
    deadline = time.monotonic() + 10
    while not rig.control.prepared() and time.monotonic() < deadline:
        time.sleep(0.05)  # the folder is made when the first job starts
    (row,) = rig.control.prepared()
    assert row["busy"] and not row["stale"] and row["source"] == str(rig.film)
    assert rig.control.remove_prepared({"all": True})["kept"] == [str(rig.film)]
    with pytest.raises(StreamError, match="full path"):
        rig.control.cancel_prepare({"path": "film.mkv"})
    assert rig.control.cancel_prepare({"path": str(rig.film)}) == {"cancelled": 2}
    assert rig.preparer.wait(15)


def test_a_browser_stream_taken_up_after_a_crash_makes_what_is_missing(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.start()
    rig.preparer.cancel()  # the daemon died: its jobs are gone
    rig.preparer.wait(15)
    rig.runner.mode = "ok"
    rig.again().reconcile()
    assert rig.control.stream is not None and rig.control.stream.mode == "browser"
    assert rig.preparer.wait()
    assert prepared.base_ready(rig.entry())


# --- found in the review of milestone 3


def test_a_restart_after_the_video_is_gone_leaves_the_daemon_running(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.start()
    rig.preparer.cancel()
    rig.preparer.wait(15)
    rig.film.unlink()
    rig.again().reconcile()  # raised StreamError, which stopped the daemon from starting
    assert rig.control.stream is None and rig.recorded_stream() is None
    assert rig.fw.calls[-1] == "close"
    assert any("could not be started again" in line for line in rig.log)


def test_a_restart_after_the_video_changed_serves_its_new_versions(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.start()
    rig.preparer.cancel()
    rig.preparer.wait(15)
    st = os.stat(rig.film)
    os.utime(rig.film, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # uploaded again
    rig.runner.mode = "ok"
    rig.again().reconcile()
    rig.preparer.wait()
    assert rig.control.stream is not None
    assert rig.control.stream.entries == [str(rig.entry())]
    assert rig.control.status()["stream"]["videos"][0]["ready"] is True
    assert (rig.paths.videos_dir / "1").resolve() == rig.entry().resolve()


def test_a_restart_does_not_queue_a_transcode_again(tmp_path: Path) -> None:
    rig = Rig(tmp_path, codec="h264", pix_fmt="yuv420p10le")
    rig.runner.mode = "slow"
    rig.start(confirmed=True)
    rig.preparer.cancel()  # cancelled on purpose: hours of work
    rig.preparer.wait(15)
    before = len(rig.preparer.status())
    rig.again().reconcile()
    assert rig.control.stream is not None
    assert len(rig.preparer.status()) == before and not rig.preparer.active()
    assert any("not transcoded yet" in line and "vpstream prepare" in line for line in rig.log)


def test_start_answers_with_the_jobs_it_queued(rig: Rig) -> None:
    rig.runner.mode = "slow"
    info = rig.start()
    assert sorted(info["jobs"]) == sorted(j["id"] for j in rig.preparer.status())
    assert len(info["jobs"]) == 2
    rig.preparer.cancel()
    rig.preparer.wait(15)


def test_a_remove_never_takes_what_a_job_writes(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.control.prepare({"file": str(rig.film), "sub": "none"})
    rig.preparer.wait()
    take = rig.preparer.take
    raced: list[Any] = []

    def take_after_a_prepare(folder: Path) -> Path | None:
        # A prepare of the same video comes in between the list and the move: the list
        # said nothing used the folder.
        rig.runner.mode = "slow"
        raced.append(rig.control.prepare({"file": str(rig.film), "sub": "2", "confirmed": True}))
        return take(folder)

    monkeypatch.setattr(rig.preparer, "take", take_after_a_prepare)
    answer = rig.control.remove_prepared({"all": True})
    assert raced and raced[0]["jobs"]
    assert answer == {"removed": 0, "kept": [str(rig.film)]}
    assert prepared.base_ready(rig.entry())  # the burn-in still has its base to work from
    rig.preparer.cancel()
    rig.preparer.wait(15)


def test_a_running_job_keeps_its_folder(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.control.prepare({"file": str(rig.film), "sub": "none"})
    assert rig.preparer.take(rig.entry()) is None  # queued or running: kept
    rig.preparer.cancel()
    rig.preparer.wait(15)
    moved = rig.preparer.take(rig.entry())
    assert moved is not None and not rig.entry().exists()


def test_a_plan_can_be_shown_without_queueing_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, fps=60)
    answer = rig.control.prepare({"file": str(rig.film), "sub": "2,9", "dry_run": True})
    assert answer["jobs"] == [] and rig.preparer.status() == []  # long, but not asked: shown
    (plan,) = answer["plans"]
    assert plan["longer_than_film"] and plan["notes"] == ["subtitle 9: not in this file"]
    kinds = [(t["kind"], t["number"], t["treatment"]) for t in plan["tracks"]]
    assert kinds[:2] == [("audio", 1, "AAC"), ("audio", 2, "AAC")]
    assert kinds[2:] == [("subtitle", 1, "soft"), ("subtitle", 2, "burned in"),
                         ("subtitle", 3, "burned in")]  # fmt: skip


def test_an_encode_of_unknown_length_or_size_is_asked_about(tmp_path: Path) -> None:
    rig = Rig(tmp_path, duration=0)
    with pytest.raises(NeedsConfirm) as refused:
        rig.control.prepare({"file": str(rig.film), "sub": "2"})
    assert refused.value.details["plans"][0]["unknown"]
    copy_only = rig.control.prepare({"file": str(rig.film), "sub": "none"})  # no encode
    assert copy_only["jobs"]
    rig.preparer.wait()


def test_shutdown_stops_the_prepares(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.control.prepare({"file": str(rig.film), "sub": "none"})
    rig.control.shutdown()
    assert not rig.preparer.active()


# --- the same through the API and the stream port


def test_a_long_prepare_is_refused_with_its_plan_over_the_api(tmp_path: Path) -> None:
    rig = Rig(tmp_path, fps=60)
    http = api.create_app(rig.control.status, rig.control).test_client()
    answer = http.post("/api/v1/prepare", json={"file": str(rig.film), "sub": "2"})
    data = answer.get_json()
    assert answer.status_code == 400 and data["code"] == api.CONFIRM
    assert data["plans"][0]["name"] == "film.mkv"
    done = http.post("/api/v1/prepare", json={"file": str(rig.film), "sub": "none"}).get_json()
    assert done["jobs"] and done["plans"] and "stream_port" in done  # with the status
    rig.preparer.wait()
    listed = http.get("/api/v1/prepared").get_json()["prepared"]
    assert listed[0]["source"] == str(rig.film) and listed[0]["base"] == "copy"
    assert http.post("/api/v1/prepared/rm", json={"all": True}).get_json()["removed"] == 1


def test_what_a_viewer_gets_while_a_video_is_made(rig: Rig) -> None:
    viewer = public.create_app(rig.control).test_client()
    ip = {"X-Vpstream-Ip": "192.0.2.1"}
    rig.runner.mode = "slow"
    rig.start()
    page = viewer.get("/aaa", headers={**ip, "Accept": "text/html"})
    assert page.status_code == 503 and "being prepared" in page.text
    assert 'http-equiv="refresh"' in page.text
    plain = viewer.get("/aaa", headers=ip)
    assert plain.status_code == 503 and plain.mimetype == "text/plain"
    assert plain.headers["Retry-After"] == "30"
    rig.preparer.cancel()
    rig.preparer.wait(15)
    rig.runner.mode = "ok"
    page = viewer.get("/aaa", headers={**ip, "Accept": "text/html"})
    assert page.status_code == 503 and "not prepared" in page.text
    rig.control.prepare({"file": str(rig.film), "sub": "none"})
    rig.preparer.wait()
    page = viewer.get("/aaa", headers={**ip, "Accept": "text/html"})
    assert page.status_code == 200 and '<script id="settings"' in page.text
    assert page.headers["Cache-Control"] == "no-store"
    vlc = viewer.get("/aaa", headers=ip)
    assert vlc.status_code == 302 and vlc.headers["Location"] == "/aaa/1/hls/base/master.m3u8"
    file = viewer.get("/aaa/1/hls/base/master.m3u8", headers=ip)
    assert file.headers["X-Vpstream-File"] == "1/base/master.m3u8"
    assert file.headers["X-Vpstream-Type"] == "application/vnd.apple.mpegurl" and file.data == b""
    script = viewer.get("/aaa/assets/player.js", headers=ip)
    assert script.status_code == 200 and script.mimetype == "text/javascript" and script.data


def test_the_index_marks_what_is_not_ready(rig: Rig) -> None:
    (rig.media / "e2.mkv").write_bytes(b"e2")
    rig.runner.mode = "slow"
    rig.control.start({"dir": str(rig.media), "alias": "aaa", "mode": "browser",
                       "policy": "open"})  # fmt: skip
    viewer = public.create_app(rig.control).test_client()
    index = viewer.get("/aaa", headers={"X-Vpstream-Ip": "192.0.2.1", "Accept": "text/html"})
    assert index.text.count("(not ready)") == 2
    rig.preparer.cancel()
    rig.preparer.wait(15)
