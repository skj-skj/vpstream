"""Tiny media files made with the real ffmpeg, for the integration tests."""

from __future__ import annotations

from pathlib import Path

from vpstream.ffargs import base_args
from vpstream.tools import SystemRunner


def ffmpeg(*args: str | Path, timeout: float = 120) -> None:
    res = SystemRunner().run([*base_args(), *map(str, args)], timeout=timeout)
    assert res.returncode == 0, res.stderr


def make_sample(d: Path) -> Path:
    """A 12 s H.264 file with two audio tracks, an SRT and an ASS subtitle track, in `d`."""
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
    ffmpeg(
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
        out,
    )
    return out
