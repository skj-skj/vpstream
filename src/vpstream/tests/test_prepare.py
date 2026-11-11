from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vpstream import ffargs, prepare, prepared
from vpstream.prepare import BASE, BURN, SOFT, Task
from vpstream.probe import MediaInfo, VideoTrack, parse_probe
from vpstream.tests.fakes import FakeFfmpeg


def probe_json(codec: str = "h264", pix_fmt: str = "yuv420p", duration: float = 600.0,
               audio: int = 2, fps: int = 24) -> dict[str, Any]:  # fmt: skip
    streams: list[dict[str, Any]] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": codec,
            "width": 1920,
            "height": 1080,
            "pix_fmt": pix_fmt,
            "avg_frame_rate": f"{fps}/1",
        },
    ]
    for i in range(audio):
        streams.append({"index": len(streams), "codec_type": "audio", "codec_name": "aac",
                        "channels": 2, "tags": {"language": ["eng", "jpn"][i % 2]},
                        "disposition": {"default": int(i == 1)}})  # fmt: skip
    for codec_name in ("subrip", "ass", "hdmv_pgs_subtitle"):
        streams.append({"index": len(streams), "codec_type": "subtitle",
                        "codec_name": codec_name, "tags": {"language": "eng"}})  # fmt: skip
    return {
        "format": {"format_name": "matroska,webm", "duration": str(duration), "size": "1000"},
        "streams": streams,
    }


def info_of(path: str = "/m/film.mkv", **kw: Any) -> MediaInfo:
    return parse_probe(probe_json(**kw), path)


NOTHING = prepared.Versions()


# --- planning


def test_the_base_comes_first_and_only_the_chosen_subtitles_follow() -> None:
    todo, notes = prepare.plan(info_of(), "1,2,3", NOTHING)
    assert todo == [Task(BASE, copy=True), Task(SOFT, 1), Task(BURN, 2), Task(BURN, 3)]
    assert notes == []
    assert prepare.plan(info_of(), None, NOTHING)[0] == [Task(BASE, copy=True), Task(SOFT, 1)]


def test_what_is_made_already_is_not_made_again() -> None:
    have = prepared.Versions(base=True, soft=[1], burn=[3])
    assert prepare.plan(info_of(), "1,2,3", have)[0] == [Task(BURN, 2)]
    assert prepare.plan(info_of(), "none", have)[0] == []


@pytest.mark.parametrize(
    ("codec", "pix_fmt", "copy"),
    [("h264", "yuv420p", True), ("h264", "yuv420p10le", False), ("hevc", "yuv420p10le", True),
     ("mpeg2video", "yuv420p", False), ("vp9", "yuv420p", False)],
)  # fmt: skip
def test_the_base_is_a_copy_only_when_a_browser_plays_the_video(
    codec: str, pix_fmt: str, copy: bool
) -> None:
    (base, *_), _ = prepare.plan(info_of(codec=codec, pix_fmt=pix_fmt), "none", NOTHING)
    assert base == Task(BASE, copy=copy)


def test_a_file_without_video_cannot_be_prepared() -> None:
    with pytest.raises(prepare.PrepareError, match="no video track"):
        prepare.plan(MediaInfo("/m/a.mka", "matroska", 60.0, 1, None), None, NOTHING)


def test_task_names() -> None:
    assert Task(BASE, copy=True).name == "base version (copy)"
    assert Task(BASE).name == "base version (transcode to H.264)"
    assert (
        Task(SOFT, 3).name == "subtitle 3 (soft)" and Task(BURN, 4).name == "subtitle 4 (burned in)"
    )
    assert Task(BURN, 1).encodes and Task(BASE).encodes and not Task(BASE, copy=True).encodes


# --- estimates (measured on the VPS in milestone 0)


def video(width: int, height: int, fps: float, hdr: bool = False) -> MediaInfo:
    v = VideoTrack(1, 0, "hevc", "", width, height, fps, "yuv420p10le", 10, hdr, None)
    return MediaInfo("/m/x.mkv", "matroska", 6480.0, 4_000_000_000, None, video=[v])


def test_a_copy_of_a_film_takes_minutes() -> None:
    seconds = prepare.estimate(video(1920, 1080, 24), Task(BASE, copy=True))
    assert 6 * 60 <= seconds <= 10 * 60  # 1h48m: 6 to 10 minutes measured


def test_an_encode_is_slower_for_more_pixels_and_for_hdr() -> None:
    hd = prepare.estimate(video(1920, 1080, 24), Task(BASE))
    assert 6480 / 2.5 <= hd <= 6480 / 1.0  # 1.6x to 2.5x measured; the low end is used
    assert prepare.estimate(video(1920, 1080, 60), Task(BASE)) > 2 * hd
    uhd_hdr = prepare.estimate(video(3840, 2160, 60, hdr=True), Task(BURN, 1))
    assert uhd_hdr > 6480 / 0.1  # 0.07x measured: copy only, in practice


def test_an_unknown_frame_rate_counts_as_30() -> None:
    assert prepare.estimate(video(1920, 1080, 0), Task(BASE)) == prepare.estimate(
        video(1920, 1080, 30), Task(BASE)
    )


def test_soft_subtitles_take_seconds() -> None:
    assert prepare.estimate(video(1920, 1080, 24), Task(SOFT, 1)) < 60


def test_space_needed() -> None:
    info = video(1920, 1080, 24)
    s = ffargs.EncodeSettings(maxrate="8M")
    assert prepare.output_bytes(info, Task(BASE, copy=True), s) == info.size
    assert prepare.output_bytes(info, Task(BURN, 1), s) == 6480 * 1_000_000
    odd = ffargs.EncodeSettings(maxrate="9000k")
    assert prepare.output_bytes(info, Task(BASE), odd) == 6480 * 1_000_000  # unknown: 8M


def test_a_plan_says_when_it_takes_longer_than_the_film() -> None:
    entry = Path("/c/x")
    copy = prepare.VideoPlan("/m/x.mkv", entry, video(1920, 1080, 24), [Task(BASE, copy=True)])
    burn = prepare.VideoPlan("/m/x.mkv", entry, video(1920, 1080, 60), [Task(BURN, 1)])
    assert not copy.longer_than_film and not copy.transcodes
    assert burn.longer_than_film and not burn.transcodes
    assert prepare.VideoPlan("/m/x.mkv", entry, video(1920, 1080, 24), [Task(BASE)]).transcodes
    none = prepare.VideoPlan("/m/x.mkv", entry, None, notes=["cannot be prepared: x"])
    assert none.to_dict()["tasks"] == [] and not none.longer_than_film


def test_ffmpeg_errors_leave_out_progress_reports() -> None:
    output = "frame=10\nout_time_us=1000\nError opening input\nspeed=1.2x\nprogress=end\n"
    assert prepare.ffmpeg_errors(output) == "Error opening input"
    assert prepare.ffmpeg_errors("progress=continue\n") == "no error message"
    # A message with an = is kept, and so is anything else that is not a report.
    said = "frame=1\nUnrecognized option crf=x.\nstream_0_0_q=28.0\nbitrate=  12.3kbits/s"
    assert prepare.ffmpeg_errors(said) == "Unrecognized option crf=x."


# --- running the jobs


class Rig:
    def __init__(self, tmp_path: Path, mode: str = "ok", **probe: Any) -> None:
        self.film = tmp_path / "media" / "film.mkv"
        self.film.parent.mkdir(exist_ok=True)
        self.film.write_bytes(b"video")
        self.cache = tmp_path / "cache"
        self.runner = FakeFfmpeg(tmp_path, probe_json(**probe), mode)
        self.log: list[str] = []
        self.preparer = prepare.Preparer(self.cache, self.runner, self.log.append)
        self.entry = prepared.entry_dir(self.cache, str(self.film))

    def plan(self, spec: str | None = "1,2,3") -> prepare.VideoPlan:
        return prepare.plan_video(self.runner, self.cache, str(self.film), spec, set())

    def submit(self, spec: str | None = "1,2,3") -> list[prepare.Job]:
        p = self.plan(spec)
        assert p.info is not None
        return self.preparer.submit(p.source, p.entry, p.info, p.tasks)

    def states(self) -> dict[str, str]:
        return {j["task"]: j["state"] for j in self.preparer.status()}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_every_version_is_made_one_after_the_other(rig: Rig) -> None:
    jobs = rig.submit()
    assert len(jobs) == 4 and rig.preparer.wait()
    assert set(rig.states().values()) == {"done"}, rig.preparer.status()
    have = prepared.versions(rig.entry)
    assert have == prepared.Versions(True, [1], [2, 3])
    meta = prepared.read_meta(rig.entry)
    assert meta is not None and meta["base"] == "copy" and meta["source"] == str(rig.film)
    master = (rig.entry / "burn" / "2" / "master.m3u8").read_text()
    assert 'URI="../../base/a1.m3u8"' in master and 'URI="../../base/a2.m3u8"' in master
    assert not list(rig.entry.glob("*.part")) and not list(rig.entry.glob(".work*"))
    calls = [c for c in rig.runner.calls() if "-f" in c and "hls" in c]
    assert all(c[:4] == ["-progress", "pipe:1", "-nostats", "-nostdin"] for c in calls)
    assert all("progress" in m or "started" in m or "done" in m for m in rig.log)


def test_the_audio_players_start_with_is_the_files_default(rig: Rig) -> None:
    rig.submit("none")
    rig.preparer.wait()
    call = next(c for c in rig.runner.calls() if "-var_stream_map" in c)
    stream_map = call[call.index("-var_stream_map") + 1]
    assert "name:a2,default:yes" in stream_map and "a1,default" not in stream_map


def test_a_job_queued_or_running_is_not_queued_twice(rig: Rig) -> None:
    rig.runner.mode = "slow"
    assert len(rig.submit()) == 4
    assert rig.submit() == []
    assert rig.preparer.pending(rig.entry) == {
        Task(BASE, copy=True), Task(SOFT, 1), Task(BURN, 2), Task(BURN, 3)
    }  # fmt: skip
    assert rig.preparer.cancel() == 4 and rig.preparer.wait(15)


def test_cancel_stops_the_running_job_and_drops_the_queued(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.submit()
    deadline = time.monotonic() + 10
    while rig.states().get("base version (copy)") != "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    started = time.monotonic()
    assert rig.preparer.cancel(str(rig.film.parent)) == 4  # by its folder
    assert rig.preparer.wait(15) and time.monotonic() - started < 12
    assert set(rig.states().values()) == {"cancelled"}
    assert not (rig.entry / "base").exists() and not (rig.entry / "base.part").exists()


def test_cancel_of_another_file_changes_nothing(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.submit("none")
    assert rig.preparer.cancel("/m/other.mkv") == 0
    assert rig.preparer.cancel(str(rig.film) + "x") == 0  # a longer name is not inside it
    assert rig.preparer.active(rig.entry)
    rig.preparer.cancel()
    rig.preparer.wait(15)


def test_a_failed_encode_says_why_and_leaves_nothing(rig: Rig) -> None:
    rig.runner.mode = "fail"
    rig.submit("none")
    rig.preparer.wait()
    (job,) = rig.preparer.status()
    assert job["state"] == "failed" and "Invalid data found" in job["error"]
    assert "exit 1" in job["error"] and "out_time" not in job["error"]
    assert not rig.entry.exists() or not any(rig.entry.glob("base*"))


def test_a_burn_without_a_base_fails_and_the_next_job_still_runs(rig: Rig) -> None:
    p = rig.plan("2")
    assert p.info is not None
    rig.preparer.submit(p.source, p.entry, p.info, [Task(BURN, 2), Task(SOFT, 1)])
    rig.preparer.wait()
    states = rig.states()
    assert states == {"subtitle 2 (burned in)": "failed", "subtitle 1 (soft)": "done"}


def test_a_file_changed_after_it_was_planned_is_not_prepared(rig: Rig) -> None:
    plan = rig.plan("none")
    assert plan.info is not None
    st = os.stat(rig.film)
    os.utime(rig.film, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # replaced by another
    rig.preparer.submit(plan.source, plan.entry, plan.info, plan.tasks)
    rig.preparer.wait()
    (job,) = rig.preparer.status()
    assert job["state"] == "failed" and "changed after it was queued" in job["error"]
    assert not (plan.entry / "base").exists()


def test_no_room_on_the_disk(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prepare, "has_room", lambda folder, need: False)
    rig.submit("none")
    rig.preparer.wait()
    assert "not enough free space" in rig.preparer.status()[0]["error"]


def test_a_burn_of_styled_text_needs_the_subtitles_filter(rig: Rig) -> None:
    rig.runner.filters = "overlay"
    rig.submit("2,3")
    rig.preparer.wait()
    states = rig.states()
    assert states["subtitle 2 (burned in)"] == "failed"  # ASS: libass
    assert states["subtitle 3 (burned in)"] == "done"  # PGS: the overlay filter
    assert "libass" in rig.preparer.status()[1]["error"]


def test_hdr_is_tone_mapped_only_with_zscale(tmp_path: Path) -> None:
    rig = Rig(tmp_path, codec="h264", pix_fmt="yuv420p10le")  # Hi10P: a transcode
    rig.runner.filters = "subtitles"
    p = rig.plan("none")
    assert p.info is not None and p.tasks == [Task(BASE)]
    rig.preparer.submit(p.source, p.entry, p.info, p.tasks)
    rig.preparer.wait()
    call = next(c for c in rig.runner.calls() if "-var_stream_map" in c)
    assert "libx264" in call and "-vf" not in call  # SDR


def test_progress_and_time_left(rig: Rig) -> None:
    clock = [100.0]
    job = prepare.Job(1, "/m/f.mkv", Path("/c/k"), Task(BASE), info_of(duration=600), 300.0)
    job.state, job.started = prepare.RUNNING, 100.0
    assert job.to_dict(clock[0] + 10)["seconds_left"] == 290.0  # no progress yet: the estimate
    job.done_seconds = 150.0  # a quarter after 50 s: 150 s more
    assert job.to_dict(150.0)["seconds_left"] == 150.0 and job.to_dict(150.0)["progress"] == 0.25
    job.done_seconds = 9999.0
    assert job.to_dict(150.0)["progress"] == 1.0
    queued = prepare.Job(2, "/m/f.mkv", Path("/c/k"), Task(SOFT, 1), info_of(), 12.0)
    assert queued.to_dict(0)["seconds_left"] == 12.0
    empty = prepare.Job(3, "/m/f.mkv", Path("/c/k"), Task(BASE), MediaInfo("", "", 0, 0, None), 1)
    assert empty.progress == 0.0


def test_leftovers_are_swept_before_the_first_job(rig: Rig) -> None:
    (rig.entry / "base.part").mkdir(parents=True)
    rig.submit("none")
    rig.preparer.wait()
    assert not (rig.entry / "base.part").exists() and (rig.entry / "base").is_dir()


def test_shutdown_stops_the_worker(rig: Rig) -> None:
    rig.runner.mode = "slow"
    rig.submit("none")
    done = threading.Event()
    threading.Thread(target=lambda: (rig.preparer.shutdown(), done.set()), daemon=True).start()
    assert done.wait(15)


# --- found in the review of milestone 3


def test_a_size_ffprobe_did_not_tell_counts_as_1080p_not_as_nothing() -> None:
    blank = video(0, 0, 24)
    assert prepare.estimate(blank, Task(BURN, 1)) == prepare.estimate(
        video(1920, 1080, 24), Task(BURN, 1)
    )
    plan = prepare.VideoPlan("/m/x.mkv", Path("/c/x"), blank, [Task(BURN, 1)])
    assert plan.unknown and plan.needs_asking and not plan.longer_than_film


def test_a_length_ffprobe_did_not_tell_asks_before_an_encode_only() -> None:
    v = video(1920, 1080, 24)
    endless = MediaInfo(v.path, v.format_name, 0.0, v.size, None, video=v.video)
    assert prepare.VideoPlan("/m/x", Path("/c"), endless, [Task(BASE)]).needs_asking
    copy = prepare.VideoPlan("/m/x", Path("/c"), endless, [Task(BASE, copy=True), Task(SOFT, 1)])
    assert not copy.needs_asking


def test_estimates_use_the_speeds_this_host_was_measured_at() -> None:
    info = video(1920, 1080, 24)
    fast = prepare.Speeds(copy=22.0, encode=3.2)
    assert prepare.estimate(info, Task(BASE, copy=True), fast) == pytest.approx(6480 / 22)
    assert prepare.estimate(info, Task(BASE), fast) == pytest.approx(6480 / 3.2)
    plan = prepare.VideoPlan("/m/x", Path("/c"), info, [Task(BASE)], speeds=fast)
    assert plan.to_dict()["tasks"][0]["seconds"] == pytest.approx(6480 / 3.2, abs=0.1)


def test_the_load_of_a_video_against_1080p_at_24_fps() -> None:
    assert prepare.load_factor(video(1920, 1080, 24)) == 1.0
    assert prepare.load_factor(video(3840, 2160, 48)) == 8.0
    assert prepare.load_factor(video(1920, 1080, 24, hdr=True)) == pytest.approx(1 / 0.35)
    assert prepare.load_factor(MediaInfo("", "", 1, 1, None)) == 1.0


def test_a_version_whose_playlist_names_a_missing_file_is_not_installed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As before the fix: the burned-in version's playlist named "%v_init.mp4".
    real = ffargs.rendition_output_args
    monkeypatch.setattr(
        ffargs,
        "rendition_output_args",
        lambda out, stream_map: [
            "%v_init.mp4" if a == "video_init.mp4" else a for a in real(out, stream_map)
        ],
    )
    rig.submit("3")
    rig.preparer.wait()
    states = rig.states()
    assert states["base version (copy)"] == "done"
    assert states["subtitle 3 (burned in)"] == "failed"
    error = next(j["error"] for j in rig.preparer.status() if j["kind"] == BURN)
    assert "not whole" in error and "%v_init.mp4" in error
    assert prepared.versions(rig.entry).burn == []  # nothing a player would choose
