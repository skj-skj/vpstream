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


def test_font_warnings() -> None:
    err = "frame=1\n[Parsed_subtitles_0] fontselect: (Arial, 400, 0) -> DejaVuSans\nfps=3\n"
    assert bench.font_warnings(err) == [
        "[Parsed_subtitles_0] fontselect: (Arial, 400, 0) -> DejaVuSans"
    ]


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


def test_fmt_duration() -> None:
    assert bench.fmt_duration(7380) == "2h 03m"
    assert bench.fmt_duration(95) == "1m 35s"
    assert bench.fmt_duration(7) == "7s"
