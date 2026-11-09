"""Tests that run the real ffmpeg on tiny generated files. Run with --integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpstream import bench
from vpstream.caps import clips
from vpstream.probe import probe
from vpstream.tools import SystemRunner

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("need_ffmpeg")]


@pytest.fixture(scope="module")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 12 s H.264 file with two audio tracks, an SRT and an ASS subtitle track."""
    d = tmp_path_factory.mktemp("media")
    srt = d / "a.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:05,000\nHello\n\n")
    ass = d / "a.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, Alignment\n"
        "Style: Default,DejaVu Sans,24,&H00FFFFFF,2\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:11.00,Default,Styled line\n"
    )
    out = d / "sample.mkv"
    runner = SystemRunner()
    res = runner.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=12",
            "-i",
            str(srt),
            "-i",
            str(ass),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-map",
            "3:s",
            "-map",
            "4:s",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-c:s:0",
            "srt",
            "-c:s:1",
            "ass",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=jpn",
            str(out),
        ],
        timeout=120,
    )
    assert res.returncode == 0, res.stderr
    return out


def test_probe_real_file(sample: Path) -> None:
    info = probe(SystemRunner(), sample)
    assert info.video[0].codec == "h264"
    assert [a.language for a in info.audio] == ["eng", "jpn"]
    assert [s.treatment for s in info.subtitles] == ["soft", "burned in"]
    assert info.duration == pytest.approx(12, abs=0.5)


def test_bench_real_file(sample: Path, tmp_path: Path) -> None:
    report = bench.benchmark(
        SystemRunner(), sample, sample=6, presets=("ultrafast",), tmp_dir=str(tmp_path)
    )
    failed = [(s.name, s.error) for s in report.steps if not s.ok]
    assert not failed
    assert [s.name for s in report.steps] == ["copy", "transcode ultrafast", "burn-in ultrafast"]
    assert all(s.output_bytes > 0 and s.speed > 0 for s in report.steps)
    json.dumps(report.to_json())


def test_caps_clips(tmp_path: Path) -> None:
    manifest = clips.generate(SystemRunner(), tmp_path / "c")
    items = manifest["clips"]
    assert isinstance(items, list)
    h264 = next(c for c in items if c["id"] == "h264-8")
    assert h264["mp4"] and h264["hls"], h264
    assert (tmp_path / "c" / "h264-8" / "index.m3u8").is_file()
    master = (tmp_path / "c" / "multi-audio" / "master.m3u8").read_text()
    assert master.count("TYPE=AUDIO") == 2, master
    assert (tmp_path / "c" / "subs.vtt").read_text().startswith("WEBVTT")
    # Reused, not regenerated, on the next call.
    assert clips.generate(SystemRunner(), tmp_path / "c") == manifest
