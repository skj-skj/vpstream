from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from vpstream import mkv, reindex
from vpstream.tests import mkvdata
from vpstream.tests.fakes import FakeMkvmerge, FakeRunner
from vpstream.tools import ToolMissing


@pytest.fixture
def film(tmp_path: Path) -> Path:
    return mkvdata.write(tmp_path / "film.mkv", ("video", "audio", "subtitles"), indexed=[1])


def test_the_copy_is_named_after_the_path_size_and_change_time(tmp_path: Path, film: Path) -> None:
    first = reindex.copy_path(tmp_path / "cache", str(film))
    assert first.parent == tmp_path / "cache" / "index" and first.suffix == ".mkv"
    assert reindex.copy_path(tmp_path / "cache", str(film)) == first
    os.utime(film, ns=(1, 1))
    assert reindex.copy_path(tmp_path / "cache", str(film)) != first
    assert reindex.ready(tmp_path / "cache", str(film)) is None
    assert reindex.ready(tmp_path / "cache", str(tmp_path / "gone.mkv")) is None


def test_mkvmerge_is_told_to_index_every_video_and_audio_track() -> None:
    identify = {
        "tracks": [
            {"id": 0, "type": "video"},
            {"id": 1, "type": "audio"},
            {"id": 2, "type": "subtitles"},
            {"id": 3, "type": "audio"},
            {"id": "x", "type": "audio"},
        ]
    }
    assert reindex.track_ids(identify) == [0, 1, 3]
    assert reindex.track_ids({}) == []
    args = reindex.mkvmerge_args("/srv/a b.mkv", Path("/c/x.mkv"), [0, 1])
    assert args == [
        "mkvmerge",
        "-q",
        "-o",
        "/c/x.mkv",
        "--cues",
        "0:iframes",
        "--cues",
        "1:iframes",
        "/srv/a b.mkv",
    ]


def test_make_writes_a_copy_with_a_full_index(tmp_path: Path, film: Path) -> None:
    tools = FakeMkvmerge(tmp_path)
    out = reindex.copy_path(tmp_path / "cache", str(film))
    reindex.make(tools, str(film), out, threading.Event())
    assert mkv.index_problem(film) and mkv.index_problem(out) is None
    assert reindex.ready(tmp_path / "cache", str(film)) == out
    assert not list(out.parent.glob("*.part"))
    assert tools.calls()[-1][-1] == str(film)
    assert tools.calls()[-1][3:7] == ["--cues", "0:iframes", "--cues", "1:iframes"]


@pytest.mark.parametrize(
    ("mode", "message"),
    [("fail", "mkvmerge failed: Error: the file could not be read"), ("same", "no better")],
)
def test_a_failed_copy_leaves_nothing(tmp_path: Path, film: Path, mode: str, message: str) -> None:
    out = tmp_path / "cache" / "index" / "x.mkv"
    with pytest.raises(reindex.ReindexError, match=message):
        reindex.make(FakeMkvmerge(tmp_path, mode), str(film), out, threading.Event())
    assert not out.exists() and list(out.parent.iterdir()) == []


def test_a_copy_can_be_stopped(tmp_path: Path, film: Path) -> None:
    out = tmp_path / "cache" / "index" / "x.mkv"
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(reindex.Cancelled):
        reindex.make(FakeMkvmerge(tmp_path, "slow"), str(film), out, cancel)
    assert time.monotonic() - started < 10
    assert not out.exists() and list(out.parent.iterdir()) == []


def test_no_copy_without_room_for_it(
    tmp_path: Path, film: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(reindex.shutil, "disk_usage", lambda p: usage._replace(free=1000))
    with pytest.raises(reindex.ReindexError, match="not enough free space"):
        reindex.make(FakeMkvmerge(tmp_path), str(film), tmp_path / "c" / "x.mkv", threading.Event())


def test_without_mkvmerge(tmp_path: Path, film: Path) -> None:
    with pytest.raises(ToolMissing, match="mkvtoolnix"):
        reindex.make(FakeRunner(()), str(film), tmp_path / "x.mkv", threading.Event())


def test_a_file_mkvmerge_cannot_read(tmp_path: Path, film: Path) -> None:
    tools = FakeRunner(("mkvmerge",))
    tools.on("mkvmerge", "-J", returncode=2, stdout='{"errors": ["unsupported"]}')
    with pytest.raises(reindex.ReindexError, match="mkvmerge cannot read it"):
        reindex.make(tools, str(film), tmp_path / "x.mkv", threading.Event())
