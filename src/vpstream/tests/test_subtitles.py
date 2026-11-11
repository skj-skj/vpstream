from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from vpstream import subtitles
from vpstream.probe import FFMPEG, MKVEXTRACT, SubtitleTrack
from vpstream.tests.fakes import FakeRunner
from vpstream.tools import Result, ToolMissing

MKVTOOLNIX = ("mkvextract", "mkvmerge")
MKV = subtitles.MKV_WEBVTT


def _track(
    codec: str, reader: str = FFMPEG, index: int = 4, number: int = 2, language: str = "spa"
) -> SubtitleTrack:
    return SubtitleTrack(
        number=number,
        index=index,
        codec=codec,
        language=language,
        title="",
        default=False,
        reader=reader,
    )


def _vtt(number: int = 2) -> SubtitleTrack:
    return _track("webvtt", MKVEXTRACT, number=number)


def _identify(*subs: tuple[int, str, str]) -> dict[str, Any]:
    """mkvmerge -J output: a video track, then subtitle tracks (id, codec ID, language)."""
    tracks: list[dict[str, Any]] = [{"id": 0, "type": "video", "properties": {}}]
    for track_id, codec_id, lang in subs:
        props = {"codec_id": codec_id, "language": lang}
        tracks.append({"id": track_id, "type": "subtitles", "properties": props})
    return {"tracks": tracks, "errors": []}


def _mkvtoolnix(identify: dict[str, Any], **extract: Any) -> FakeRunner:
    r = FakeRunner(available=MKVTOOLNIX)
    r.on("mkvmerge", "-J", stdout=json.dumps(identify))
    r.on("mkvextract", **extract)
    return r


class WritingRunner(FakeRunner):
    """Writes the file it is asked for, the way the real programs do (also when they fail)."""

    def __init__(self, out: Path, *available: str) -> None:
        super().__init__(available=available)
        self.out = out

    def run(self, args: Any, *, timeout: float | None = None) -> Result:
        res = super().run(args, timeout=timeout)
        if args[0] == "ffmpeg":
            Path(args[-1]).write_text("WEBVTT\n")
        elif args[0] == "mkvextract":
            Path(args[-1].partition(":")[2]).write_text("WEBVTT\n")
        return res


def test_ffmpeg_reads_ordinary_text_tracks() -> None:
    args = subtitles.ffmpeg_webvtt_args("in.mkv", _track("subrip"), Path("/o/s.vtt"))
    assert args[0] == "ffmpeg" and args[-5:] == ["-map", "0:4", "-c:s", "webvtt", "/o/s.vtt"]


def test_mkvextract_args() -> None:
    args = subtitles.mkvextract_args("in.mkv", 8, Path("/o/s.vtt"))
    assert args == ["mkvextract", "-q", "in.mkv", "tracks", "8:/o/s.vtt"]


def test_track_id_comes_from_mkvmerge_not_the_stream_index() -> None:
    # ffmpeg skipped mkvmerge track 1, so ffprobe's subtitle 2 (stream index 2) is track 3.
    identify = _identify((2, "S_TEXT/UTF8", "eng"), (3, MKV, "spa"))
    assert subtitles.mkvextract_track_id(identify, _track("webvtt", index=2)) == 3


def test_track_id_ignores_unknown_languages() -> None:
    identify = _identify((1, MKV, "und"))
    assert subtitles.mkvextract_track_id(identify, _vtt(number=1)) == 1
    und = _track("webvtt", number=1, language="und")
    assert subtitles.mkvextract_track_id(_identify((1, MKV, "fre")), und) == 1


@pytest.mark.parametrize(
    ("subs", "match"),
    [
        (((1, MKV, "spa"),), "lists 1 subtitle tracks"),
        (((1, "S_TEXT/UTF8", "eng"), (2, "S_TEXT/UTF8", "spa")), "do not agree"),
        (((1, "S_TEXT/UTF8", "eng"), (2, MKV, "ger")), "do not agree"),
    ],
)
def test_track_id_refuses_a_track_that_does_not_match(
    subs: tuple[tuple[int, str, str], ...], match: str
) -> None:
    with pytest.raises(subtitles.SubtitleError, match=match):
        subtitles.mkvextract_track_id(_identify(*subs), _vtt())


def test_mkvextract_is_given_the_mkvmerge_track_id(tmp_path: Path) -> None:
    out = tmp_path / "s.vtt"
    r = WritingRunner(out, *MKVTOOLNIX)
    identify = _identify((2, "S_TEXT/UTF8", "eng"), (5, MKV, "spa"))
    r.on("mkvmerge", "-J", stdout=json.dumps(identify))
    subtitles.extract_webvtt(r, "in.mkv", _vtt(), out)
    assert r.calls[-1] == ["mkvextract", "-q", "in.mkv", "tracks", f"5:{tmp_path / 's.part.vtt'}"]
    assert out.read_text() == "WEBVTT\n" and [p.name for p in tmp_path.iterdir()] == ["s.vtt"]


@pytest.mark.parametrize("missing", MKVTOOLNIX)
def test_missing_mkvtoolnix_names_the_package(tmp_path: Path, missing: str) -> None:
    r = FakeRunner(available=[t for t in MKVTOOLNIX if t != missing])
    with pytest.raises(ToolMissing, match=f"{missing} .*apt install mkvtoolnix"):
        subtitles.extract_webvtt(r, "in.mkv", _vtt(), tmp_path / "s.vtt")


def test_mkvmerge_errors_are_reported(tmp_path: Path) -> None:
    r = FakeRunner(available=MKVTOOLNIX)
    err = {"errors": ["The file 'in.mkv' could not be opened for reading.\n"]}
    r.on("mkvmerge", "-J", returncode=2, stdout=json.dumps(err))
    with pytest.raises(subtitles.SubtitleError, match="could not be opened"):
        subtitles.extract_webvtt(r, "in.mkv", _vtt(), tmp_path / "s.vtt")
    r.on("mkvmerge", "-J", returncode=0, stdout="not json")
    with pytest.raises(subtitles.SubtitleError, match="mkvmerge cannot read"):
        subtitles.extract_webvtt(r, "in.mkv", _vtt(), tmp_path / "s.vtt")


def test_burned_in_tracks_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot be soft"):
        subtitles.extract_webvtt(FakeRunner(), "in.mkv", _track("ass"), tmp_path / "s.vtt")


@pytest.mark.parametrize("code", [0, 1])
def test_mkvextract_warnings_still_count_as_written(tmp_path: Path, code: int) -> None:
    out = tmp_path / "s.vtt"
    r = WritingRunner(out, *MKVTOOLNIX)
    r.on("mkvmerge", "-J", stdout=json.dumps(_identify((1, "S_TEXT/UTF8", "eng"), (2, MKV, "spa"))))
    r.on("mkvextract", returncode=code, stdout="Warning: something odd")
    subtitles.extract_webvtt(r, "in.mkv", _vtt(), out)
    assert out.is_file()


def test_mkvextract_errors_are_read_from_stdout(tmp_path: Path) -> None:
    # mkvtoolnix prints its errors on stdout and leaves stderr empty.
    msg = "Error: No track with the ID 2 was found in the source file."
    r = _mkvtoolnix(_identify((1, "S_TEXT/UTF8", "eng"), (2, MKV, "spa")), returncode=2, stdout=msg)
    with pytest.raises(subtitles.SubtitleError, match="No track with the ID 2"):
        subtitles.extract_webvtt(r, "in.mkv", _vtt(), tmp_path / "s.vtt")


@pytest.mark.parametrize("code", [0, 1])
def test_mkvextract_without_a_file_is_an_error(tmp_path: Path, code: int) -> None:
    out = tmp_path / "s.vtt"
    out.write_text("WEBVTT\n\nleft by an earlier run\n")
    r = _mkvtoolnix(_identify((1, "S_TEXT/UTF8", "eng"), (2, MKV, "spa")), returncode=code)
    with pytest.raises(subtitles.SubtitleError, match="no file written"):
        subtitles.extract_webvtt(r, "in.mkv", _vtt(), out)
    assert not out.exists()


def test_ffmpeg_failure_is_an_error(tmp_path: Path) -> None:
    out = tmp_path / "s.vtt"
    r = WritingRunner(out, "ffmpeg")
    r.on("ffmpeg", returncode=1, stderr="Invalid data found")
    with pytest.raises(subtitles.SubtitleError, match="Invalid data found"):
        subtitles.extract_webvtt(r, "in.mkv", _track("subrip"), out)
    # What ffmpeg wrote before it failed is not left behind as if it were a result.
    assert list(tmp_path.iterdir()) == []


def test_no_file_written_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(subtitles.SubtitleError, match="no file written"):
        subtitles.extract_webvtt(FakeRunner(), "in.mkv", _track("subrip"), tmp_path / "s.vtt")


def test_filter_source_keeps_only_plain_suffixes(tmp_path: Path) -> None:
    for name, linked in (("a.MKV", "source.mkv"), ("a.mkvé", "source"), ("noext", "source")):
        src = tmp_path / name
        src.write_bytes(b"x")
        work = tmp_path / f"work-{linked}-{len(name)}"
        work.mkdir()
        link = subtitles.filter_source(work, src)
        assert link.name == linked and link.is_symlink() and link.resolve() == src.resolve()


def test_filter_source_replaces_a_link_left_by_an_earlier_run(tmp_path: Path) -> None:
    old, new = tmp_path / "old.mkv", tmp_path / "new.mkv"
    old.write_bytes(b"o")
    new.write_bytes(b"n")
    subtitles.filter_source(tmp_path, old)
    assert subtitles.filter_source(tmp_path, new).resolve() == new.resolve()


def test_cues_are_moved_earlier_and_nothing_else_changes() -> None:
    vtt = (
        "WEBVTT\n\n"
        "1\n00:00:10.500 --> 00:00:12.000 line:85% align:center\nA --> B\n\n"
        "01:00.000 --> 01:01.250\nshort form\n\n"
        "NOTE 00:00:01.000 --> 00:00:02.000 is not a cue\n\n"
        "00:00:00.200 --> 00:00:01.000\nbefore the start\n"
    )
    out = subtitles.shift_webvtt(vtt, 0.5).split("\n")
    assert out[3] == "00:00:10.000 --> 00:00:11.500 line:85% align:center"
    assert out[4] == "A --> B"
    assert out[6] == "00:00:59.500 --> 00:01:00.750"
    assert out[9] == "NOTE 00:00:01.000 --> 00:00:02.000 is not a cue"
    assert out[11] == "00:00:00.000 --> 00:00:00.500"  # cannot start before 0


def test_a_shift_of_hours() -> None:
    out = subtitles.shift_webvtt("WEBVTT\n\n10:00:00.000 --> 10:00:01.000\nx\n", 3600.25)
    assert "08:59:59.750 --> 09:00:00.750" in out
