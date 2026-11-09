from __future__ import annotations

from pathlib import Path

import pytest

from vpstream import subtitles
from vpstream.probe import FFMPEG, MKVEXTRACT, SubtitleTrack
from vpstream.tests.fakes import FakeRunner
from vpstream.tools import ToolMissing


def _track(codec: str, reader: str = FFMPEG, index: int = 4) -> SubtitleTrack:
    return SubtitleTrack(
        number=2, index=index, codec=codec, language="spa", title="", default=False, reader=reader
    )


def test_ffmpeg_reads_ordinary_text_tracks() -> None:
    args = subtitles.webvtt_args("in.mkv", _track("subrip"), Path("/o/s.vtt"))
    assert args[0] == "ffmpeg" and args[-5:] == ["-map", "0:4", "-c:s", "webvtt", "/o/s.vtt"]


def test_mkvextract_reads_tracks_ffmpeg_cannot() -> None:
    args = subtitles.webvtt_args("in.mkv", _track("webvtt", MKVEXTRACT, 8), Path("/o/s.vtt"))
    assert args == ["mkvextract", "-q", "in.mkv", "tracks", "8:/o/s.vtt"]


def test_missing_mkvextract_names_the_package(tmp_path: Path) -> None:
    with pytest.raises(ToolMissing, match="apt install mkvtoolnix"):
        subtitles.extract_webvtt(
            FakeRunner(), "in.mkv", _track("webvtt", MKVEXTRACT), tmp_path / "s.vtt"
        )


def test_burned_in_tracks_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be soft"):
        subtitles.extract_webvtt(FakeRunner(), "in.mkv", _track("ass"), tmp_path / "s.vtt")


@pytest.mark.parametrize(("code", "ok"), [(0, True), (1, True), (2, False)])
def test_mkvextract_warnings_still_count_as_written(tmp_path: Path, code: int, ok: bool) -> None:
    out = tmp_path / "s.vtt"
    out.write_text("WEBVTT\n")
    r = FakeRunner(available=("mkvextract",))
    r.on("mkvextract", returncode=code, stderr="Error: No track with the ID 8")
    if ok:
        subtitles.extract_webvtt(r, "in.mkv", _track("webvtt", MKVEXTRACT), out)
    else:
        with pytest.raises(subtitles.SubtitleError, match="No track"):
            subtitles.extract_webvtt(r, "in.mkv", _track("webvtt", MKVEXTRACT), out)


def test_no_file_written_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(subtitles.SubtitleError, match="no file written"):
        subtitles.extract_webvtt(FakeRunner(), "in.mkv", _track("subrip"), tmp_path / "s.vtt")
