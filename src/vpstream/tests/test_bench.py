from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpstream import bench
from vpstream.ffargs import Window
from vpstream.probe import parse_probe
from vpstream.tests.fakes import FakeRunner
from vpstream.tests.test_probe import SAMPLE

ENCODERS = " V....D libx264  H.264\n A....D aac  AAC\n"
FILTERS = " .. subtitles V->V x\n TS overlay VV->V x\n .S zscale V->V x\n .S tonemap V->V x\n"


def test_plan_window() -> None:
    assert bench.plan_window(7200, 120) == Window(720.0, 120)
    assert bench.plan_window(100, 120) == Window(0.0, 100)
    assert bench.plan_window(130, 120) == Window(10.0, 120)
    assert bench.plan_window(7200, 0) == Window(0.0, None)


def test_pick_burn_track_prefers_first_burned_track() -> None:
    info = parse_probe(SAMPLE)
    t = bench.pick_burn_track(info, None)
    assert t is not None and t.codec == "ass"
    chosen = bench.pick_burn_track(info, 3)
    assert chosen is not None and chosen.codec == "hdmv_pgs_subtitle"
    with pytest.raises(ValueError):
        bench.pick_burn_track(info, 9)


def test_font_warnings_list_only_replaced_or_missing_fonts() -> None:
    replaced = "[sub] fontselect: (Arial, 400, 0) -> /f/DejaVuSans.ttf, 0, DejaVuSans"
    found = "[sub] fontselect: (DejaVu Sans, 400, 0) -> /f/DejaVuSans.ttf, 0, DejaVuSans"
    bold = "[sub] fontselect: (Arial, 700, 0) -> /f/arialbd.ttf, 0, Arial-BoldMT"
    glyph = "[sub] Glyph 0x4E00 not found, selecting one more font for (Arial, 400, 0)"
    err = "\n".join(["frame=1", replaced, found, bold, glyph, "fps=3"])
    assert bench.font_warnings(err) == [replaced, glyph]


def test_estimate_is_empty_without_a_duration() -> None:
    step = bench.StepResult("copy", True, media_seconds=10, wall_seconds=5)
    report = bench.BenchReport("f", 0.0, 0, "v", 0.0, 10, {}, [step])
    assert report.to_json()["steps"][0]["estimated_full_seconds"] is None


def test_step_speed_and_bitrate() -> None:
    s = bench.StepResult("copy", True, media_seconds=120, wall_seconds=60, output_bytes=15_000_000)
    assert s.speed == 2.0
    assert s.output_mbps == pytest.approx(1.0)


def _runner() -> FakeRunner:
    r = FakeRunner()
    r.on("ffprobe", stdout=json.dumps(SAMPLE))
    r.on("ffmpeg", "-hide_banner", "-encoders", stdout=ENCODERS)
    r.on("ffmpeg", "-hide_banner", "-filters", stdout=FILTERS)
    r.on("ffmpeg", "-hide_banner", "-version", stdout="ffmpeg version 6.1\n")
    return r


def test_benchmark_runs_each_step(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")
    r = _runner()
    report = bench.benchmark(
        r, src, sample=60, presets=("veryfast", "medium"), tmp_dir=str(tmp_path)
    )
    names = [s.name for s in report.steps]
    assert names == ["copy", "transcode veryfast", "transcode medium", "burn-in veryfast"]
    assert all(s.ok for s in report.steps)
    encodes = [c for c in r.calls if "-f" in c and "hls" in c]
    assert len(encodes) == 4
    # Low priority wrappers are used when the tools exist; this fake has none.
    assert all(c[0] == "ffmpeg" for c in encodes)
    # The window starts 10% into the 7380 s file.
    assert all(c[c.index("-ss") + 1] == "738.050" for c in encodes)
    burn = encodes[-1]
    assert "subtitles=filename=" in burn[burn.index("-filter_complex") + 1]
    assert report.steps[1].note == "HDR tone mapped"
    data = report.to_json()
    assert data["steps"][0]["speed"] > 0
    text = bench.format_report(report)
    assert "transcode medium" in text and "movie.mkv" in text


def test_benchmark_reports_failed_step(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")
    r = _runner()
    r.on("ffmpeg", "-nostdin", returncode=1, stderr="Unknown encoder\n")
    report = bench.benchmark(r, src, presets=("veryfast",), tmp_dir=str(tmp_path))
    assert not report.steps[0].ok
    assert "Unknown encoder" in report.steps[0].error
    assert "FAILED" in bench.format_report(report)


def test_low_priority_prefix_when_available(tmp_path: Path) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")
    r = _runner()
    r.available |= {"nice", "ionice"}
    bench.benchmark(r, src, presets=("veryfast",), tmp_dir=str(tmp_path))
    encodes = [c for c in r.calls if "hls" in c]
    assert encodes and all(c[:6] == ["nice", "-n", "19", "ionice", "-c", "3"] for c in encodes)


def test_burn_in_is_skipped_when_the_tmp_folder_is_unusable(tmp_path: Path) -> None:
    tmp = tmp_path / "my disk"
    tmp.mkdir()
    src = tmp / "movie.mkv"
    src.write_bytes(b"x")
    report = bench.benchmark(_runner(), src, presets=("veryfast",), tmp_dir=str(tmp))
    # The finished steps are kept; burn-in explains what to change.
    assert [s.ok for s in report.steps[:2]] == [True, True]
    burn = report.steps[-1]
    assert burn.skipped and "--tmp" in burn.note


def test_burn_in_is_skipped_when_links_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "movie.mkv"
    src.write_bytes(b"x")

    def no_links(*_a: object) -> None:
        raise PermissionError("links not allowed")

    monkeypatch.setattr(bench.os, "symlink", no_links)
    report = bench.benchmark(_runner(), src, presets=("veryfast",), tmp_dir=str(tmp_path))
    assert report.steps[-1].skipped and "links not allowed" in report.steps[-1].note


@pytest.mark.parametrize("name", ["-dash.mkv", "a:b.mkv"])
def test_ffmpeg_is_given_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # A relative -dash.mkv is taken for an option and a:b.mkv for a URL.
    (tmp_path / name).write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    r = _runner()
    report = bench.benchmark(r, Path(name), presets=("veryfast",), tmp_dir=str(tmp_path))
    inputs = [c[c.index("-i") + 1] for c in r.calls if "-i" in c]
    assert inputs and set(inputs) == {str(tmp_path.resolve() / name)}
    assert r.calls[0][-1] == str(tmp_path.resolve() / name)  # ffprobe
    assert report.file == name  # shown as it was given


def _report(steps: list[bench.StepResult], load: float = 1.0) -> bench.BenchReport:
    return bench.BenchReport("f.mkv", 600.0, 1, "v", 0.0, 60.0, {}, steps, load)


def step(name: str, speed: float, ok: bool = True) -> bench.StepResult:
    return bench.StepResult(name, ok, media_seconds=60.0 * speed, wall_seconds=60.0)


def test_bench_says_what_prepare_should_expect_of_this_host() -> None:
    # A 4K file (4x 1080p): its veryfast encode at 0.5x is 2x for 1080p at 24 fps.
    steps = [step("copy", 14.0), step("transcode veryfast", 0.5)]
    steps += [step("transcode medium", 0.2), step("burn-in veryfast", 0.4)]
    r = _report(steps, load=4.0)
    assert bench.speeds(r) == {"copy_speed": 14.0, "encode_speed": 2.0}
    text = bench.format_report(r)
    assert "[prepare]\n  copy_speed = 14\n  encode_speed = 2" in text


def test_bench_suggests_only_what_it_measured() -> None:
    r = _report([step("copy", 14.0, ok=False), step("transcode medium", 0.5)])
    assert bench.speeds(r) == {} and "[prepare]" not in bench.format_report(r)
