from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from vpstream import mkv
from vpstream.tests import mkvdata


def test_a_full_index_is_proper(tmp_path: Path) -> None:
    assert mkv.index_problem(mkvdata.write(tmp_path / "a.mkv")) is None
    front = mkvdata.write(tmp_path / "b.mkv", where="front")
    assert mkv.index_problem(front) is None


def test_an_index_without_audio_is_not(tmp_path: Path) -> None:
    path = mkvdata.write(tmp_path / "a.mkv", ("video", "audio", "audio"), indexed=[1])
    assert mkv.index_problem(path) == "its seek index has no audio entries"
    one = mkvdata.write(tmp_path / "b.mkv", ("video", "audio", "audio"), indexed=[1, 2])
    assert mkv.index_problem(one) == "its seek index has no audio entries"  # every track


def test_subtitles_need_no_entries(tmp_path: Path) -> None:
    path = mkvdata.write(tmp_path / "a.mkv", ("video", "audio", "subtitles"), indexed=[1, 2])
    assert mkv.index_problem(path) is None


def test_a_file_without_an_index(tmp_path: Path) -> None:
    path = mkvdata.write(tmp_path / "a.mkv", where="none")
    assert mkv.index_problem(path) == "it has no seek index"
    novideo = mkvdata.write(tmp_path / "b.mkv", ("audio",), indexed=[])
    assert mkv.index_problem(novideo) == "its seek index has no audio entries"


def test_what_is_not_matroska_is_left_alone(tmp_path: Path) -> None:
    for name, data in (("a.mp4", b"\x00\x00\x00\x18ftypmp42"), ("b.mkv", b""), ("c.mkv", b"x")):
        (tmp_path / name).write_bytes(data)
        assert mkv.index_problem(tmp_path / name) is None
        assert not mkv.is_matroska(tmp_path / name)
    assert mkv.index_problem(tmp_path / "missing.mkv") is None


@pytest.mark.parametrize("cut", [10, 60, 200, -40])
def test_a_damaged_file_is_said_to_have_an_unreadable_index(tmp_path: Path, cut: int) -> None:
    whole = mkvdata.write(tmp_path / "a.mkv").read_bytes()
    damaged = tmp_path / "b.mkv"
    damaged.write_bytes(whole[:cut])
    problem = mkv.index_problem(damaged)
    assert problem is not None and problem.startswith(("its seek index cannot be read", "it "))


def test_garbage_after_the_header_does_not_loop_or_crash(tmp_path: Path) -> None:
    whole = mkvdata.write(tmp_path / "a.mkv").read_bytes()
    bad = tmp_path / "b.mkv"
    bad.write_bytes(whole[:60] + b"\x00" * 200 + whole[260:])
    assert mkv.index_problem(bad) is not None


def test_track_kinds_and_indexed_tracks() -> None:
    raw = mkvdata.tracks(["video", "audio", "subtitles", "audio"])
    assert mkv.track_kinds(raw[12:]) == {1: "video", 2: "audio", 4: "audio"}
    assert mkv.indexed_tracks(mkvdata.cues([1, 3])[12:]) == {1, 3}


SAMPLES = Path("/storage/emulated/0/Download/vpstream-samples")


@pytest.mark.integration
def test_real_files(tmp_path: Path, need_ffmpeg: None) -> None:
    """ffmpeg indexes only the video of a film with sound; mkvmerge, told to, all of it."""
    made = tmp_path / "ffmpeg.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine",
            "-t",
            "4",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(made),
        ],
        check=True,
    )
    assert mkv.index_problem(made) == "its seek index has no audio entries"
    if not shutil.which("mkvmerge"):
        pytest.skip("mkvmerge is not installed on this host")
    fixed = tmp_path / "fixed.mkv"
    subprocess.run(
        [
            "mkvmerge",
            "-q",
            "-o",
            str(fixed),
            "--cues",
            "0:iframes",
            "--cues",
            "1:iframes",
            str(made),
        ],
        check=True,
    )
    assert mkv.index_problem(fixed) is None
